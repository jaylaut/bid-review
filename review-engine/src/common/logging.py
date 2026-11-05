"""结构化日志模块：全项目打日志只走这里。

给 Java 同学的定位：相当于 SLF4J + Logback，但是"结构化"的——
每条日志是一个字典（机器能按字段检索），而不是一整行拼好的字符串。
开发环境把字典渲染成一行人读的文本，生产环境渲染成 JSON 交给采集器（Loki/Alloy）。

对外提供（tests/common/test_logging.py 按这份清单测）：
    setup()              进程入口调一次，初始化日志。重复调用安全（不会叠加 handler）
    build_processors()   日志处理链（加时间戳、脱敏、合并上下文等），测试里单独取用
    get_logger(name)     取 logger，用法 log.info("事件名", 字段=值)
    bind_run_context()   绑定进程级 / 请求级上下文（run_id、trace_id 等）
    clear_run_context()  清掉上下文（请求结束时调用，防止串到下一个请求）

为什么这里自己读环境变量，不走 common.config？
日志必须比配置更早可用：.env 写坏、配置类导入失败时，恰恰最需要一行日志说清原因。
如果本模块依赖 config，配置一崩，连"为什么崩"都打不出来。

关于"为什么不用后台线程异步写日志"（历史实现用过 QueueListener）：
它带来两个问题——① 业务代码打完日志立刻检查输出（测试就是如此）会拿不到内容，
因为写操作还排在另一个线程的队列里；② 进程退出时队列里没来得及写的日志会丢。
stdout 写入本身是内存缓冲、速度极快，为它引入一条线程和一套队列不划算，所以现在直接写。
"""
# 这行的作用见 config.py 顶部的说明，照抄即可。
from __future__ import annotations

# logging：Python 标准库的日志模块，负责最终把日志写到 stdout / 文件。
import logging

# logging.handlers 里有按天切分的文件 handler（TimedRotatingFileHandler）。
import logging.handlers

# os：读环境变量、拿进程号。
import os

# socket：拿主机名（os 模块没有 hostname，别写成 os.hostname()）。
import socket

# sys：拿标准输出、判断是否接在终端上（决定控制台日志要不要上色）。
import sys

# uuid：给每次运行生成一个短 run_id，把同一次运行的多条日志串起来。
import uuid

# datetime：生成带时区的时间戳。
from datetime import datetime

# Path：拼日志目录和 .env 的路径。
from pathlib import Path

# Any：类型标注，表示随便什么类型。
from typing import Any

# orjson：比标准库 json 快几倍的序列化库（pyproject 已声明依赖）。
import orjson

# structlog：结构化日志库本体，提供"日志是字典"这套能力。
import structlog

# 三个上下文相关的函数：
#   merge_contextvars  把上下文里的字段（task_id、item_id 等）合并进每条日志
#   bind_contextvars   往上下文里写字段
#   clear_contextvars  清空上下文
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars

# 脱敏的敏感字段名清单。日志里出现这些键名时，值被替换成 ***。
# 标书系统最容易踩的合规线：把投标人原文、密钥、身份证写进日志。
SENSITIVE_KEYS = frozenset({
    "password", "passwd", "pwd", "secret", "token", "access_token", "refresh_token",
    "api_key", "apikey", "authorization", "cookie", "llm_api_key",
    "minio_secret_key", "minio_access_key", "id_card", "phone",
})
# 单个字符串字段的截断长度默认值（字符数）。防止把整段标书原文、整个 prompt 灌进日志。
_MAX_TEXT_DEFAULT = 2000

# 进程级固定字段（服务名、环境、版本、主机、进程号），每条日志都带。
_STATIC: dict[str, Any] = {}
# 是否已经初始化过。用来让 get_logger 在忘记调 setup 时自动兜底。
_setup_done = False
# .env 是否已经加载过，保证只加载一次。
_dotenv_loaded = False


def _max_text() -> int:
    """读单个字段的截断长度上限。

    为什么每次都从环境变量读，而不是在模块加载时读成常量？
    因为 setup() 会先把 .env 的内容灌进环境变量；如果在导入时就固化成常量，
    那时 .env 还没读，拿到的永远是默认值，配了也不生效。
    """
    # 读环境变量并转成整数；配得不合法（比如写了字母）就用默认值，不让日志模块因此崩掉。
    try:
        return max(200, int(_env("LOG_MAX_TEXT_LEN", str(_MAX_TEXT_DEFAULT))))
    except ValueError:
        # 配置不合法时不报错，退回默认值——日志模块本身不能成为故障源。
        return _MAX_TEXT_DEFAULT


def _load_dotenv_once() -> None:
    """把 review-engine/.env 的内容读进环境变量，只做一次。

    override=False 的意思是：已经存在的环境变量不覆盖。
    这样容器里 docker -e 注入的值优先于 .env 文件，和配置模块的优先级保持一致。
    """
    # 声明修改模块级变量（原因见 config.py 里 global 的注释）。
    global _dotenv_loaded
    # 已经加载过：直接返回，避免重复读文件。
    if _dotenv_loaded:
        return
    # 先标记，防止重复加载。
    _dotenv_loaded = True
    # dotenv 没装、文件不可读都不能影响日志本身，所以整段包在 try 里。
    try:
        # 延迟导入：这个模块要在"依赖可能不全"的环境里也能工作。
        from dotenv import load_dotenv

        # 本文件在 src/common/ 下，往上两级是 review-engine/，.env 就在那里。
        env_file = Path(__file__).resolve().parents[2] / ".env"
        # 文件存在才加载；不存在就全部走环境变量和默认值。
        if env_file.is_file():
            load_dotenv(env_file, override=False)
    # 任何异常都吞掉。noqa 是给 lint 工具的说明：这里"什么都不做"是故意的。
    except Exception:  # noqa: S110 —— 日志模块不能因为读配置失败而起不来
        pass


def _env(key: str, default: str = "") -> str:
    """读环境变量的小工具：去掉首尾空格，空字符串也按"没配"处理，返回默认值。"""
    # os.getenv 拿不到时返回 None，用 or "" 兜成空串；strip 去空格；最后还是空就用默认值。
    return (os.getenv(key) or "").strip() or default


# ── 日志处理链上的各个环节 ────────────────────────────────────
# 一个 processor（处理器）就是一个函数：接收当前日志字典，加工后返回。
# 它们按顺序串成一条链，上一步的输出就是下一步的输入。

def _add_logger_name(logger, _method_name, event_dict: dict) -> dict:
    """把 logger 的名字写进日志的 logger 字段。

    为什么不用 structlog 自带的 add_logger_name？
    那个函数只认标准库的 logger（假定有 .name 属性）；而测试里为了把输出写进
    内存缓冲区，用的是 PrintLogger，它没有 .name，直接用会抛 AttributeError。
    这里用 getattr 取，取不到就跳过，两种 logger 都能跑。
    """
    # getattr(对象, 属性名, 默认值)：取不到时返回 None，不会抛异常。
    name = getattr(logger, "name", None)
    # 取到了才写。setdefault 保证调用方自己传了 logger 字段时不被覆盖。
    if name is not None:
        event_dict.setdefault("logger", name)
    # 交给下一个 processor。
    return event_dict


def _inject_trace_context(_logger, _name, event_dict: dict) -> dict:
    """把链路追踪的 trace_id / span_id 写进每条日志。

    这样在日志系统里按 trace_id 一搜，就能把一次请求跨多个服务的日志串起来，
    再跳到 Langfuse 看这次判定每一步问了模型什么。
    没配链路追踪时拿不到有效的 trace，此时不写字段——绝不写全 0 的假 ID，
    否则日志里全是无法关联的噪音。
    """
    # 没装链路追踪库、没初始化，都不能影响打日志，所以整段包在 try 里。
    try:
        # 延迟导入，理由同 _load_dotenv_once。
        from opentelemetry import trace

        # 取当前 span（一次操作的时间片段）的上下文信息。
        ctx = trace.get_current_span().get_span_context()
        # is_valid 为 True 表示真的有追踪上下文，此时才写。
        if ctx.is_valid:
            # 格式化成 32 位十六进制字符串。
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            # span_id 是 16 位。
            event_dict["span_id"] = format(ctx.span_id, "016x")
    # 观测失败绝不能打断业务；这里也不能记日志，否则会自己调自己、无限递归。
    except Exception:  # noqa: S110
        pass
    # 返回加工后的字典，交给链上的下一个 processor。
    return event_dict


def _inject_static(_logger, _name, event_dict: dict) -> dict:
    """把进程级固定字段（服务名、环境、版本、主机、进程号）写进每条日志。"""
    # 逐个写入。用 setdefault 而不是直接赋值：调用方自己传了同名字段时以调用方为准。
    for k, v in _STATIC.items():
        event_dict.setdefault(k, v)
    # 交给下一个 processor。
    return event_dict


def _sanitize(_logger, _name, event_dict: dict, _depth: int = 0) -> dict:
    """脱敏 + 截断：敏感键的值换成 ***，超长字符串截断。

    _depth 是递归深度，只往下钻两层：日志里的字典一般不会更深，
    再深说明有人往日志里塞业务大对象，那本身就该改。
    """
    # 读一次截断长度，避免循环里反复读环境变量。
    limit = _max_text()
    # list(...) 复制一份键列表再遍历：循环里要改字典的值，直接遍历会有隐患。
    for k, v in list(event_dict.items()):
        # 键名转小写再匹配，避免 Password / PASSWORD 这种大小写绕过。
        lk = str(k).lower()
        # 命中敏感键名：值直接打码。
        if lk in SENSITIVE_KEYS:
            event_dict[k] = "***"
        # 字符串超长：截断并写明原长度，方便判断"是不是传错了东西"。
        elif isinstance(v, str):
            if len(v) > limit:
                event_dict[k] = f"{v[:limit]}...(truncated,{len(v)} chars)"
        # 嵌套字典且还没到两层：递归脱敏里面的内容。
        elif isinstance(v, dict) and _depth < 2:
            event_dict[k] = _sanitize(_logger, _name, v, _depth + 1)
    # 交给下一个 processor。
    return event_dict


def _orjson_serializer(data: Any, **_kw) -> str:
    """给 structlog 用的序列化函数：把日志字典转成 JSON 字符串。

    default=str 的作用：遇到 datetime、Path 这类 JSON 不认识的类型时转成字符串，
    而不是抛异常——日志不能因为某个字段类型特殊就整条丢掉。
    """
    # orjson.dumps 出来是 bytes，decode 成字符串才能被日志系统按行处理。
    return orjson.dumps(data, default=str).decode("utf-8")


def _timestamper(_logger, _name, event_dict: dict) -> dict:
    """给每条日志加时间戳，键名固定为 timestamp。

    格式带时区偏移，例如 2026-11-05T22:20:11.337+08:00。
    不带偏移的话，跨时区排查时"这条日志到底是几点"会变成猜谜；
    带偏移的字符串人眼和日志系统都能直接用。
    """
    # setdefault：调用方自己传了 timestamp 就不覆盖。
    event_dict.setdefault("timestamp", datetime.now().astimezone().isoformat(timespec="milliseconds"))
    # 交给下一个 processor。
    return event_dict


def build_processors() -> list:
    """返回日志处理链（不含最后"渲染成文本 / JSON"那一步）。

    为什么单独暴露这个函数？渲染方式在不同场合不一样：
    进程里用 ProcessorFormatter 渲染到 stdout 和文件；测试里直接接一个 JSONRenderer
    写进内存缓冲区。但"加时间戳、脱敏、合并上下文"这些环节必须完全一致——
    测试要验证的就是生产用的那条链，不是另造一条。
    """
    # 按顺序返回各个环节。顺序有讲究：先合并上下文，再加 level / logger 名，
    # 然后时间戳、固定字段、trace，最后脱敏和异常信息。
    # 脱敏必须靠后，否则前面环节加进来的字段会漏掉脱敏。
    return [
        # 把上下文里的 task_id / item_id / step 合并进每条日志。
        merge_contextvars,
        # 加 level 字段（info / warning ...）。
        structlog.stdlib.add_log_level,
        # 加 logger 字段（logger 的名字，一般传 __name__）。用自己的容错版本，原因见函数注释。
        _add_logger_name,
        # 加 timestamp 字段。
        _timestamper,
        # 加 service / env / version / host / pid。
        _inject_static,
        # 加 trace_id / span_id。
        _inject_trace_context,
        # 脱敏 + 截断。
        _sanitize,
        # 有异常时把 traceback 渲染进 exception 字段。
        structlog.processors.format_exc_info,
        # 处理 stack_info（手动调用 log.info(..., stack_info=True) 时的调用栈）。
        structlog.processors.StackInfoRenderer(),
    ]


# ── 初始化 ────────────────────────────────────────────────────
def setup(*, force: bool = False) -> None:
    """初始化日志。进程入口调一次；重复调用安全。

    force 是保留的历史参数：早期实现靠它强制重建，现在每次调用都会全量重建
    （先清掉旧 handler 再装新的），传不传结果一样，保留只是为了不破坏调用方写法。
    """
    # 声明修改模块级标记（global 的说明见 config.py）。
    global _setup_done
    # 先加载 .env，否则下面读到的环境变量全是默认值。
    _load_dotenv_once()

    # 运行环境：dev 开发 / prod 生产。
    app_env = _env("APP_ENV", "dev")
    # 日志级别，默认 INFO；转大写是因为 logging 库的常量名是大写（INFO / DEBUG）。
    level_name = _env("LOG_LEVEL", "INFO").upper()
    # 输出格式：生产默认 JSON（给采集器解析），其他环境默认 console（给人看）。
    fmt = _env("LOG_FORMAT", "json" if app_env in {"prod", "production"} else "console").lower()
    # 写入进程级固定字段。update 会覆盖旧值，重复调用 setup 时能跟上环境变化。
    _STATIC.update({
        "service": _env("SERVICE_NAME", "review-engine"),
        "env": app_env,
        "version": _env("APP_VERSION", "0.1.0"),
        "host": socket.gethostname(),
        "pid": os.getpid(),
    })

    # 取统一处理链；"渲染"那一步在下面按场合分别接。
    shared = build_processors()
    # JSON 渲染器，用 orjson 加速。
    renderer_json = structlog.processors.JSONRenderer(serializer=_orjson_serializer)
    # ProcessorFormatter 负责把"标准库的日志记录"和"处理链"接起来：
    #   remove_processors_meta 去掉 structlog 内部用的辅助字段（_record、_from_structlog 等）
    #   foreign_pre_chain      第三方库（uvicorn、asyncpg）打的普通日志也走同一套加工，
    #                          这样日志格式全项目统一，不会一半 JSON 一半纯文本
    remove_meta = structlog.stdlib.ProcessorFormatter.remove_processors_meta
    json_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[remove_meta, renderer_json],
    )
    # 控制台格式：dev 环境给人看。isatty() 判断是否接在终端上，不是终端就不上色，
    # 否则输出重定向到文件时会塞满颜色控制符。
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[remove_meta, structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())],
    )
    # stdout 用哪种格式由 LOG_FORMAT 决定。
    stdout_formatter = json_formatter if fmt == "json" else console_formatter

    # 收集本次要装的 handler（handler = 日志最终写到哪）。
    handlers: list[logging.Handler] = []
    # 标准输出 handler：直接写，不经过后台线程（原因见模块顶部说明）。
    out = logging.StreamHandler(sys.stdout)
    # 装上刚才选定的格式。
    out.setFormatter(stdout_formatter)
    # 加入列表。
    handlers.append(out)

    # 文件日志：内容恒为 JSON。采集器（Alloy）按字段解析，dev 环境也需要能被检索，
    # 所以文件格式不受 LOG_FORMAT 影响。
    if _env("LOG_FILE_ENABLED", "true").lower() in {"1", "true", "yes"}:
        # 建目录、建 handler 都可能因为权限不足 / 只读文件系统失败，失败就降级为只写 stdout。
        try:
            # 日志目录，默认工程根下的 logs/。
            log_dir = Path(_env("LOG_DIR", "logs"))
            # 不存在就建。parents=True 连父目录一起建，exist_ok=True 表示已存在也不报错。
            log_dir.mkdir(parents=True, exist_ok=True)
            # 按天切分：when="midnight" 每天零点换新文件，backupCount 控制保留天数。
            fh = logging.handlers.TimedRotatingFileHandler(
                log_dir / "review-engine.log",
                when="midnight",
                backupCount=int(_env("LOG_BACKUP_DAYS", "14")),
                # 必须显式指定 utf-8：Windows 默认 GBK，中文日志会乱码。
                encoding="utf-8",
            )
            # 文件固定用 JSON 格式。
            fh.setFormatter(json_formatter)
            # 加入列表。
            handlers.append(fh)
        # 权限不足、磁盘只读等：提示一句，然后继续只用 stdout。
        except OSError as e:
            print(f"[logging] file handler disabled: {e}", file=sys.stderr)

    # 拿到根 logger（所有日志的最终出口）。
    root = logging.getLogger()
    # 先摘掉并关闭旧 handler：这样重复调 setup 不会出现"一条日志打两遍"。
    for old in list(root.handlers):
        root.removeHandler(old)
        # close 释放文件句柄。不关的话 Windows 上文件可能被占用、删不掉。
        old.close()
    # 设置全局日志级别（低于该级别的不处理，省性能）。
    root.setLevel(getattr(logging, level_name, logging.INFO))
    # 逐个装上新 handler。
    for h in handlers:
        root.addHandler(h)

    # 让 warnings 模块的告警也走日志管道，不要另开一条输出。
    logging.captureWarnings(True)
    # 这些第三方库默认自带 handler，清掉并让它们向上冒泡到根 logger，
    # 保证格式统一（否则会出现一半咱们的 JSON、一半它们的纯文本）。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "asyncpg", "sqlalchemy"):
        lg = logging.getLogger(name)
        # 清空它们自己的 handler。
        lg.handlers.clear()
        # propagate=True 表示交给根 logger 处理。
        lg.propagate = True

    # 配置 structlog：以后所有 structlog 调用都走这条链，输出交给标准库 logging 的 handler。
    structlog.configure(
        # 业务处理链 + 最后一步 wrap_for_formatter：
        # 它把日志字典打包成标准库的日志记录，交给上面的 ProcessorFormatter 渲染。
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        # 用标准库 logger 工厂，好处是能正确记录日志来自哪个模块。
        logger_factory=structlog.stdlib.LoggerFactory(),
        # 返回的对象类型，支持 .bind() 绑定固定字段。
        wrapper_class=structlog.stdlib.BoundLogger,
        # 缓存 logger 实例，减少重复创建开销。
        cache_logger_on_first_use=True,
    )
    # 标记已初始化。
    _setup_done = True


def get_logger(name: str | None = None) -> Any:
    """取 logger。用法：

        log = get_logger(__name__)
        log.info("review.item.judged", verdict="PASS", cost_ms=120)

    第一个参数是事件名（英文点号分隔，方便统计），后面跟键值对形式的字段。
    """
    # 忘了在入口调 setup 的兜底：宁可多初始化一次，也不要静默丢日志。
    if not _setup_done:
        setup()
    # name 为空时用 "app"，避免 logger 名字是 None。
    return structlog.stdlib.get_logger(name or "app")


def bind_run_context(**extra: Any) -> None:
    """绑定运行上下文：进程级（service/env/run_id）+ 请求级（request_id、trace_id）。

    **extra 表示可以传任意多个键值对，比如 bind_run_context(request_id="abc")。
    """
    # 生成一个 12 位短 run_id，同一次运行的所有日志都能靠它串起来。
    # {**extra} 是"把字典展开"的写法：先放 run_id，再把调用方传的字段并进去。
    ctx = {"run_id": uuid.uuid4().hex[:12], **extra}
    # 网关（Java 那边）生成的 trace_id，入站时必须沿用，否则链路断成两截。
    server_trace = extra.get("trace_id") or os.getenv("TRACE_ID")
    # 有就写进上下文。
    if server_trace:
        ctx["trace_id"] = server_trace
    # 写进 structlog 的上下文，之后每条日志自动带上。
    bind_contextvars(**ctx)


def clear_run_context() -> None:
    """清空上下文。请求结束时调用，防止上下文串到下一个请求。"""
    # 清空 structlog 的上下文变量。
    clear_contextvars()
