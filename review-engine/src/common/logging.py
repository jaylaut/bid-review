"""结构化日志：structlog 门面 + stdlib 桥接 + 异步落盘（唯一入口）。

对外契约（对齐 common/__init__.py 与 api/workers 的既有调用）：
    setup()               进程入口调一次；幂等，未调用时首次 get_logger 自动兜底
    get_logger(name)      取 logger，用法 log.info("事件名", 字段=值)
    bind_run_context()    绑定进程级 + 请求级上下文（service/env/run_id/request_id）
    clear_run_context()   请求结束清理（FastAPI 中间件用）

【why 自己读 environ 而不走 get_settings】
日志必须比配置更早可用：.env 写错/配置类 import 失败时，恰恰最需要一行日志说明原因。
"""
from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import queue
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson
import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars

# ── 常量：脱敏键与截断阈值 ─────────────────────────────────────
# 标书系统最容易踩的合规线：把投标人原文 / 密钥 / 身份证写进日志
SENSITIVE_KEYS = frozenset({
    "password", "passwd", "pwd", "secret", "token", "access_token", "refresh_token",
    "api_key", "apikey", "authorization", "cookie", "llm_api_key",
    "minio_secret_key", "minio_access_key", "id_card", "phone",
})
_MAX_TEXT_DEFAULT = 2000


def _max_text() -> int:
    """长文本截断阈值。运行时读而不是导入时读成常量：
    setup() 会先加载 .env，导入时读只能拿到 shell 里的环境变量（.env 的值读不到）。"""
    try:
        return max(200, int(_env("LOG_MAX_TEXT_LEN", str(_MAX_TEXT_DEFAULT))))
    except ValueError:
        return _MAX_TEXT_DEFAULT

_STATIC: dict[str, Any] = {}
_setup_done = False
_dotenv_loaded = False
_listener: logging.handlers.QueueListener | None = None


def _load_dotenv_once() -> None:
    """把 review-engine/.env 灌进 os.environ（override=False）。

    【why 必须做】本模块刻意不依赖 common.config（要能在配置类崩塌时工作），
    但 .env 里的值不会自动进 os.environ —— 不加载的话 LOG_LEVEL / LOG_FORMAT /
    LOG_FILE_ENABLED / LOG_DIR 全是摆设，实际生效的永远是默认值。
    override=False：容器/K8s 里显式注入的环境变量优先于 .env 文件。
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        from dotenv import load_dotenv

        env_file = Path(__file__).resolve().parents[2] / ".env"   # src/common/ → review-engine/
        if env_file.is_file():
            load_dotenv(env_file, override=False)
    except Exception:  # noqa: S110 —— dotenv 未装/文件不可读时，日志本身仍要能用
        pass


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or "").strip() or default


# ── processors ────────────────────────────────────────────────
def _inject_trace_context(_logger, _name, event_dict: dict) -> dict:
    """把 OTel 当前 span 的 trace_id/span_id 写进每条日志。

    【why】这是 log 与 trace 的唯一粘合点：Loki 里按 trace_id 一搜，
    能直接跳到 Langfuse 看这次判定每一步问了什么。未配 OTLP 时是 NoOp span
    （is_valid=False），此时不写字段——绝不写全 0 的假 ID，否则日志里全是噪音。
    """
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
    except Exception:  # noqa: S110 —— 观测量失败绝不能打断主链路；此处记日志会自递归
        pass
    return event_dict


def _inject_static(_logger, _name, event_dict: dict) -> dict:
    for k, v in _STATIC.items():
        event_dict.setdefault(k, v)
    return event_dict


def _sanitize(_logger, _name, event_dict: dict, _depth: int = 0) -> dict:
    """脱敏 + 长文本截断（递归两层）。event 本身也截断——事件名拼长文本是常见误用。"""
    limit = _max_text()
    for k, v in list(event_dict.items()):
        lk = str(k).lower()
        if lk in SENSITIVE_KEYS:
            event_dict[k] = "***"
        elif isinstance(v, str):
            if len(v) > limit:
                event_dict[k] = f"{v[:limit]}...(truncated,{len(v)} chars)"
        elif isinstance(v, dict) and _depth < 2:
            event_dict[k] = _sanitize(_logger, _name, v, _depth + 1)
    return event_dict


def _orjson(data: Any, **_kw) -> str:
    """orjson 序列化（项目已有依赖，比标准库 json 快 3-5 倍）。"""
    return orjson.dumps(data, default=str).decode("utf-8")


def _timestamper(_logger, _name, event_dict: dict) -> dict:
    """ISO-8601 时间戳，带本地时区偏移（如 2026-10-27T22:34:24.565+08:00）。

    【why 不用 structlog 的 TimeStamper】它用 datetime.now() 生成 naive 时间，
    isoformat() 不带偏移，跨时区排查时"这条日志到底是几点"就成了猜谜；
    带偏移的串 Loki/Grafana 与人眼都能直接用。
    """
    event_dict.setdefault("ts", datetime.now().astimezone().isoformat(timespec="milliseconds"))
    return event_dict


class _DictQueueHandler(logging.handlers.QueueHandler):
    """把日志 record 原样入队的 QueueHandler。

    【坑】stdlib 的 QueueHandler.prepare() 会先调 record.getMessage() 把 msg 拍平成
    字符串；而 structlog 的 wrap_for_formatter 是把整个 event_dict 塞进 record.msg 的
    （ProcessorFormatter 靠 `_from_structlog` 标记去取 dict）。一旦被拍平，监听线程里
    ProcessorFormatter.format 就会抛 'str' object has no attribute 'copy'，
    表现为"日志全丢、只在 stderr 看到一堆 AttributeError"。
    覆盖 prepare 保留原始 record：本进程内的队列传对象不需要序列化，
    格式化反而推到监听线程做更省事（业务线程零格式化开销）。
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record


# ── 初始化 ────────────────────────────────────────────────────
def setup(*, force: bool = False) -> None:
    """初始化日志。进程入口调一次；重复调用安全（测试用 force=True 重建）。"""
    global _setup_done, _listener
    if _setup_done and not force:
        return
    if _listener is not None:            # force 时先停旧监听线程，避免 handler 叠加翻倍
        _listener.stop()
        _listener = None

    # 必须先加载 .env 再读配置，否则下面的 _env() 拿到的全是默认值
    _load_dotenv_once()

    app_env = _env("APP_ENV", "dev")
    level_name = _env("LOG_LEVEL", "INFO").upper()
    fmt = _env("LOG_FORMAT", "json" if app_env in {"prod", "production"} else "console").lower()
    _STATIC.update({
        "service": _env("SERVICE_NAME", "review-engine"),
        "env": app_env,
        "version": _env("APP_VERSION", "0.1.0"),
        "host": socket.gethostname(),
        "pid": os.getpid(),
    })

    shared = [
        merge_contextvars,                       # contextvars 里的 task_id/item_id/step
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _timestamper,                            # ts（ISO-8601 + 时区偏移）
        _inject_static,                          # service/env/version/host/pid
        _inject_trace_context,                   # trace_id/span_id
        _sanitize,                               # 脱敏 + 截断
        structlog.processors.format_exc_info,    # exc_info → "exception" 字段
        structlog.processors.StackInfoRenderer(),
    ]
    renderer_json = structlog.processors.JSONRenderer(serializer=_orjson)
    remove_meta = structlog.stdlib.ProcessorFormatter.remove_processors_meta
    json_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,            # 非 structlog 的日志（uvicorn/asyncpg）也走同一套
        processors=[remove_meta, renderer_json],
    )
    # stdout 按 LOG_FORMAT（dev 给人读），文件恒为 JSON（采集栈要按字段解析）。
    # 两者必须分开：dev 环境若也用 console 格式，Alloy/Loki 侧就解析不出 level/event。
    stdout_formatter = (
        json_formatter
        if fmt == "json"
        else structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[remove_meta, structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())],
        )
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handlers: list[logging.Handler] = []
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(stdout_formatter)
    handlers.append(out)

    if _env("LOG_FILE_ENABLED", "true").lower() in {"1", "true", "yes"}:
        try:
            log_dir = Path(_env("LOG_DIR", "logs"))
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.TimedRotatingFileHandler(
                log_dir / "review-engine.log",
                when="midnight", backupCount=int(_env("LOG_BACKUP_DAYS", "14")),
                encoding="utf-8",          # 【坑】不写死 utf-8，Windows 上中文事件名会乱码
            )
            fh.setFormatter(json_formatter)
            handlers.append(fh)
        except OSError as e:               # 只读文件系统/权限不足：降级为仅 stdout
            print(f"[logging] file handler disabled: {e}", file=sys.stderr)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level_name, logging.INFO))
    q: queue.SimpleQueue = queue.SimpleQueue()
    root.addHandler(_DictQueueHandler(q))   # 保留 dict msg，见 _DictQueueHandler 的说明
    _listener = logging.handlers.QueueListener(q, *handlers, respect_handler_level=True)
    _listener.start()
    atexit.register(_listener.stop)

    logging.captureWarnings(True)          # warnings 也进同一管道，别分叉
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "asyncpg", "sqlalchemy"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    _setup_done = True


def get_logger(name: str | None = None) -> Any:
    """取 logger。用法：log = get_logger(__name__)；log.info("review.item.judged", verdict="PASS")"""
    if not _setup_done:                    # 忘了在入口调 setup 的兜底：宁可多配一次，也不要静默丢日志
        setup()
    return structlog.stdlib.get_logger(name or "app")


def bind_run_context(**extra: Any) -> None:
    """绑定运行上下文。进程级：service/env/run_id；请求级：request_id/trace 透传值。"""
    ctx = {"run_id": uuid.uuid4().hex[:12], **extra}
    server_trace = extra.get("trace_id") or os.getenv("TRACE_ID")
    if server_trace:                       # Java 网关生成的 trace_id，入站必须沿用
        ctx["trace_id"] = server_trace
    bind_contextvars(**ctx)


def clear_run_context() -> None:
    """清空上下文（请求结束调用，防止上下文串到下一个请求）。"""
    clear_contextvars()
