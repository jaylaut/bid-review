"""logging 测试：dev 人读 / prod JSON / contextvars 字段带出。

不 mock structlog 内部——直接 configure 后看真实输出，
因为配置错（processor 顺序、renderer）只体现在最终产物上。
"""

from __future__ import annotations

import io
import json

import orjson
import pytest
import structlog

from common import logsetup
from common.config import get_settings


@pytest.fixture()
def captured_json_logger(monkeypatch):
    """prod 模式 + stdout 重定向到内存。返回 (logger, buffer)。
    processor 链直接用 logsetup.build_processors()——测的就是生产链。"""
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "prod")

    buf = io.StringIO()
    structlog.configure(
        processors=[*logsetup.build_processors(),
                    structlog.processors.JSONRenderer(serializer=_dumps)],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        logger_factory=structlog.PrintLoggerFactory(buf),
        cache_logger_on_first_use=False,   # 测试里必须关缓存，否则拿到旧配置的 logger
    )
    log = structlog.get_logger("test")
    return log, buf


def _dumps(obj: object, default=None, **_: object) -> str:
    return orjson.dumps(obj, default=default).decode()


def test_json_output_has_level_and_message(captured_json_logger):
    log, buf = captured_json_logger
    log.info("review.done", item_id="S3.2.4", cost_cny=0.02)
    line = json.loads(buf.getvalue())     # 能被 json.loads = 每行是合法 JSON
    assert line["event"] == "review.done"
    assert line["level"] == "info"
    assert line["item_id"] == "S3.2.4"
    assert "timestamp" in line            # TimeStamper 在链上


def test_contextvars_merged_into_output(captured_json_logger):
    """【why】A8 排查靠"按 item_id 搜日志"：contextvars 的字段必须
    自动出现在每条日志，业务代码不需要每次手动带。"""
    from common.context import set_run_context

    log, buf = captured_json_logger
    set_run_context("task-9", "item-S3.2.4", step=3)
    log.info("agent.step")
    line = json.loads(buf.getvalue())
    assert line["task_id"] == "task-9"
    assert line["item_id"] == "item-S3.2.4"
    assert line["step"] == 3


def test_filtering_bound_logger_respects_level(captured_json_logger):
    log, buf = captured_json_logger
    log.debug("should not appear")        # wrapper 级别 INFO=20，debug 被拦
    log.warning("should appear")
    assert "should not appear" not in buf.getvalue()
    assert "should appear" in buf.getvalue()


def test_setup_is_idempotent(monkeypatch):
    """setup() 连跑两次不炸（进程入口可能重复 import 触发）。"""
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "dev")
    logsetup.setup()
    logsetup.setup()  # 不抛异常即通过


def test_real_setup_dev_output(capsys, monkeypatch):
    """走一遍真实 setup()（dev 渲染），确认能打出行。"""
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "dev")
    logsetup.setup()
    logsetup.bind_run_context()
    logsetup.get_logger("smoke").info("env.ready", modules="common")
    out = capsys.readouterr().out
    assert "env.ready" in out
