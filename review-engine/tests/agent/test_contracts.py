"""契约测试：JSON 往返 + 非法值拦截（A0 核心验收）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.contracts.decision import Decision, make_fake_state, make_fake_verdict
from agent.contracts.state import AgentState
from agent.contracts.verdict import Verdict


@pytest.mark.parametrize("state", [
    make_fake_state(),
    make_fake_state(status="pending_human"),
    make_fake_state(terminated_by="max_steps"),
    make_fake_state(session_chunks=[{"chunk_id": "c1", "content": "已完成达梦适配"}]),
])
def test_state_json_roundtrip(state: AgentState):
    text = state.model_dump_json()
    again = AgentState.model_validate_json(text)
    assert again == state                       # Pydantic == 逐字段比较


def test_verdict_roundtrip():
    v = make_fake_verdict()
    assert Verdict.model_validate_json(v.model_dump_json()) == v


def test_verdict_rejects_unknown_type():
    """Literal 拦截非法判定：拼错立即报错而不是静默通过。"""
    with pytest.raises(ValidationError):
        Verdict(verdict="MAYBE", confidence=0.5, reason="x",
                engine=make_fake_verdict().engine)


def test_verdict_confidence_bounds():
    with pytest.raises(ValidationError):
        make_fake_verdict(confidence=1.5)


def test_decision_semantics():
    with pytest.raises(ValueError):
        Decision(action="final").validate_semantics()          # final 无 verdict
    with pytest.raises(ValueError):
        Decision(action="tool").validate_semantics()           # tool 无名字
    Decision(action="tool", tool_name="search_in_bid").validate_semantics()


def test_non_json_field_breaks_serialization():
    """约定探针：scratch 里塞不可序列化对象，model_dump_json 会炸——
    这就是"字段只允许 JSON 原生类型"的原因（恢复时炸在半路）。
    pydantic 的 Any 不做类型校验，约定靠本测试提醒 + 往返测试兜底。"""
    st = make_fake_state(scratch={"conn": object()})
    with pytest.raises(Exception) as ei:   # pydantic v2 抛 PydanticSerializationError
        st.model_dump_json()
    assert "erial" in type(ei.value).__name__ or "Serial" in type(ei.value).__name__
