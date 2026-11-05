"""主循环测试：五条终止路径 + 防打转 + 错误自愈（A2 验收）。全部 fake，零网络。"""

from __future__ import annotations

import time

import pytest

from agent.contracts.decision import Decision, make_fake_state, make_fake_verdict
from agent.contracts.fake_llm import FakeTools, ScriptedFakeLLM, ScriptedFakeLLMWithJudge
from agent.loop import run
from agent.state import MemoryCheckpointer


def _ctx():
    from common.context import AuthContext
    return AuthContext(user_id="u1", project_id="p1", acl=("proj:p1",))


def _search_impl(hits_text: str = "已完成达梦DM8的适配测试"):
    async def impl(args, ctx):
        return {
            "hits": [{"chunk_id": "c1", "text": hits_text, "page": 87}],
            "coverage": {"达梦适配": True, "人大金仓适配": False},
            "next_hint": "未覆盖: 人大金仓适配",
        }
    return impl


def _final(v=None, conf=0.8) -> Decision:
    return Decision(action="final", verdict=make_fake_verdict(confidence=conf) if v is None else v)


@pytest.mark.asyncio
async def test_answer_path():
    """正常两步闭环：search → final。"""
    llm = ScriptedFakeLLM([
        Decision(action="tool", tool_name="search_in_bid",
                 tool_args={"query": "达梦", "must_terms": ["达梦适配"]}),
        _final(),
    ])
    tools = FakeTools({"search_in_bid": _search_impl()})
    state = await run(make_fake_state(), _ctx(), llm, tools, MemoryCheckpointer())
    assert state.terminated_by == "answer"
    assert state.step == 2
    assert state.verdict.verdict == "PARTIAL"
    assert state.session_chunks        # 检索结果进了会话池


@pytest.mark.asyncio
async def test_max_steps_path():
    """步数超限硬停。脚本故意多给一个决策：验证的是"超了会被拦"。"""
    llm = ScriptedFakeLLM([
        Decision(action="tool", tool_name="search_in_bid", tool_args={"q": f"角度{i}"})
        for i in range(10)
    ])
    tools = FakeTools({"search_in_bid": _search_impl()})
    state = await run(make_fake_state(), _ctx(), llm, tools, MemoryCheckpointer())
    assert state.terminated_by == "max_steps"
    assert state.step == 6             # 默认 agent_max_steps=6


@pytest.mark.asyncio
async def test_budget_path():
    llm = ScriptedFakeLLM([Decision(action="tool", tool_name="search_in_bid", tool_args={})])
    tools = FakeTools({"search_in_bid": _search_impl()})
    st = make_fake_state()
    st.budget.used_tokens = 10**9      # 预算瞬间打爆
    state = await run(st, _ctx(), llm, tools, MemoryCheckpointer())
    assert state.terminated_by == "budget"


@pytest.mark.asyncio
async def test_timeout_path():
    llm = ScriptedFakeLLM([Decision(action="tool", tool_name="search_in_bid", tool_args={})])
    tools = FakeTools({"search_in_bid": _search_impl()})
    st = make_fake_state(budget={"deadline_ts": time.monotonic() - 1})
    state = await run(st, _ctx(), llm, tools, MemoryCheckpointer())
    assert state.terminated_by == "timeout"


@pytest.mark.asyncio
async def test_unknown_tool_recovers():
    """编造的工具名 → 错误喂回 → 模型换 final。任务失败率的关键防线。"""
    llm = ScriptedFakeLLM([
        Decision(action="tool", tool_name="nonexistent", tool_args={}),
        _final(),
    ])
    tools = FakeTools()
    state = await run(make_fake_state(), _ctx(), llm, tools, MemoryCheckpointer())
    assert state.terminated_by == "answer"
    assert any("不存在" in m.content for m in state.messages)


@pytest.mark.asyncio
async def test_tool_error_recovers():
    """工具抛 ToolError → 喂回模型 → 自纠。"""
    async def boom(args, ctx):
        # 用系统类异常（dispatch 包成 ToolError 喂回模型）；
        # PermissionDenied 走 error 终态（模型无法自纠权限），不是本测试场景
        raise RuntimeError("检索超时")

    llm = ScriptedFakeLLM([
        Decision(action="tool", tool_name="search_in_bid", tool_args={}),
        _final(),
    ])
    tools = FakeTools({"search_in_bid": boom})
    state = await run(make_fake_state(), _ctx(), llm, tools, MemoryCheckpointer())
    assert state.terminated_by == "answer"
    assert any("工具执行失败" in m.content for m in state.messages)


@pytest.mark.asyncio
async def test_repeat_detection_forces_conclusion():
    """连续两次同工具同参数 → 强制收口（answer 但置信 ≤0.85）。"""
    call = Decision(action="tool", tool_name="search_in_bid", tool_args={"query": "达梦"})
    judge_v = make_fake_verdict(confidence=0.9)
    llm = ScriptedFakeLLMWithJudge(
        [call, call, Decision(action="final", verdict=judge_v)],
        judge_outputs=[make_fake_verdict(confidence=0.7)],   # _force_conclude 的输出
    )
    tools = FakeTools({"search_in_bid": _search_impl()})
    state = await run(make_fake_state(), _ctx(), llm, tools, MemoryCheckpointer())
    assert state.terminated_by == "answer"
    assert state.verdict.confidence <= 0.85


@pytest.mark.asyncio
async def test_checkpointer_written_each_step():
    """每步写档 + 终态写档：崩溃恢复的地基。"""
    llm = ScriptedFakeLLM([
        Decision(action="tool", tool_name="search_in_bid", tool_args={}),
        _final(),
    ])
    ck = MemoryCheckpointer()
    tools = FakeTools({"search_in_bid": _search_impl()})
    st = make_fake_state()
    await run(st, _ctx(), llm, tools, ck)
    bucket = ck._data[(st.task_id, st.item_id)]
    assert len(bucket) >= 2            # 检索后至少一次 + 终态（无变化的步被哈希短路）
    assert bucket[-1].terminated_by == "answer"


@pytest.mark.asyncio
async def test_resume_skips_done_work():
    """对 done 的 state 再跑（start_step=终态步）不重算——幂等的循环级表达。"""
    llm = ScriptedFakeLLM([])
    tools = FakeTools()
    st = make_fake_state()
    st.step = 6                        # 已达上限
    state = await run(st, _ctx(), llm, tools, MemoryCheckpointer(), start_step=6)
    assert state.terminated_by == "max_steps"
    assert llm.calls == []             # 没有调过模型：已完成的绝不重算


@pytest.mark.asyncio
async def test_checkpoint_resume_continues():
    """存档恢复：崩溃点之后的步继续，前面的步不重跑。"""
    ck = MemoryCheckpointer()
    llm = ScriptedFakeLLM([
        Decision(action="tool", tool_name="search_in_bid", tool_args={"q": "达梦"}),
        _final(),
    ])
    tools = FakeTools({"search_in_bid": _search_impl()})
    st = make_fake_state()
    await run(st, _ctx(), llm, tools, ck)

    # 模拟崩溃恢复：读档，从下一步继续
    saved = await ck.load_latest(st.task_id, st.item_id)
    assert saved is not None
    llm2 = ScriptedFakeLLM([_final()])
    expected_first_decide = saved.step + 1   # ★ 先取值：run 会原地改 saved
    state = await run(saved, _ctx(), llm2, tools, ck, start_step=expected_first_decide)
    assert state.terminated_by == "answer"
    assert llm2.calls[0][0] == expected_first_decide   # 从崩溃点下一步开始
