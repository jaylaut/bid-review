from __future__ import annotations

import pytest

from common.errors import (
    AppError,
    ConfigError,
    ContractViolation,
    LockHeld,
    PermissionDenied,
    StorageError,
    ToolError,
    UpstreamError,
)
from common.ids import digest, new_id, sha256_text

# ── 异常体系 ──────────────────────────────────────────────────

@pytest.mark.parametrize("exc_type", [
    ToolError, PermissionDenied, LockHeld, UpstreamError, StorageError,
    ContractViolation, ConfigError,
])
def test_all_errors_inherit_app_error(exc_type):
    """【why】上层捕获统一写 except AppError——子类必须挂在这棵树上，
    谁在树外建异常类，循环里的兜底捕获就漏它。"""
    assert issubclass(exc_type, AppError)


def test_retriable_defaults_false():
    """retriable 默认 False：【why】宁可少重试（任务进人工），
    不可多重试（429 风暴）。需要重试的场景显式声明。"""
    assert UpstreamError("timeout").retriable is False
    assert UpstreamError("429 too many", retriable=True).retriable is True


def test_to_dict_carries_code_and_details():
    e = ToolError("section_id 不存在", details={"section_id": "9.9.9"})
    d = e.to_dict()
    assert d == {"code": "tool_error", "message": "section_id 不存在",
                 "details": {"section_id": "9.9.9"}}


def test_code_distinct_per_class():
    """code 撞了 = trace 里分不清错误来源。"""
    codes = {c.code for c in (ToolError, PermissionDenied, LockHeld,
                              UpstreamError, StorageError, ContractViolation)}
    assert len(codes) == 6


# ── ID 与哈希 ─────────────────────────────────────────────────

def test_digest_stable_across_key_order():
    """【why】digest 必须对键序不敏感：Pydantic dump 顺序稳定，
    但手工构造的 dict 不保证——防打转检测靠它比对参数，
    键序不同就算不同参数，检测就失效了。"""
    a = digest({"b": 1, "a": 2})
    b = digest({"a": 2, "b": 1})
    assert a == b


def test_digest_differs_for_different_args():
    assert digest({"q": "达梦"}) != digest({"q": "金仓"})


def test_digest_length_default_8():
    assert len(digest({"x": 1})) == 8
    assert len(digest({"x": 1}, length=12)) == 12


def test_sha256_text_full_length():
    h = sha256_text("abc")
    assert len(h) == 64
    assert h == sha256_text("abc")          # 稳定
    assert h != sha256_text("abd")


def test_new_id_prefix_and_uniqueness():
    ids = {new_id("task") for _ in range(100)}
    assert all(i.startswith("task_") for i in ids)
    assert len(ids) == 100                  # 100 个无碰撞（uuid4 前 12 hex 足够）


def test_digest_non_serializable_falls_back():
    """不可 JSON 序列化的对象降级 repr 而不是炸——
    【why】工具入参理论上是 JSON 安全类型，但模型偶尔会传出意外结构，
    digest 炸掉会把一个"参数记录"问题放大成"工具调用失败"。"""
    class Weird:
        def __repr__(self):
            return "<weird>"
    assert len(digest(Weird())) == 8
