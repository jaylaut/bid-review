"""telemetry（A8 观测埋点）测试。

验证三个 span（agent.step / tool.call / llm.call）能正确创建、字段齐全、
归属正确、异常时正确标记失败。用 OpenTelemetry 的 InMemorySpanExporter
把 span 收进内存，再逐字段断言——不依赖任何外部 collector。

关键理解：
- trace.get_tracer 返回的 tracer 由全局 TracerProvider 决定。测试里通过
  set_tracer_provider 装一个带 InMemorySpanExporter 的 provider，
  业务代码（telemetry 模块）就会把 span 写进这个 exporter 而不是丢弃。
- 测试结束后要 shutdown + 复位 provider，避免污染其他测试。
"""

from __future__ import annotations

import pytest
from opentelemetry import trace

# OpenTelemetry 测试侧依赖：
#   - InMemorySpanExporter：把 span 收进内存 list，测试结束时读出来断言
#   - SimpleSpanProcessor：span 结束立刻同步导出（不像 Batch 有延迟，测试不用等 flush）
#   - TracerProvider / trace.set_tracer_provider：替换全局 provider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# OpenTelemetry 用一把"只允许设置一次"的 Once 锁防止 provider 被误覆盖
# （_TRACER_PROVIDER_SET_ONCE）。测试里每个用例都要换一个带内存 exporter 的
# provider，所以必须重置这把锁，否则第二个用例的 set_tracer_provider 会被
# 静默拒绝（日志里打 WARNING "Overriding ... not allowed"，span 收不进 exporter）。
from opentelemetry.util._once import Once

from agent.telemetry import (
    _str_or_empty,
    llm_call_span,
    record_usage,
    step_span,
    tool_call_span,
)
from common.context import set_run_context


@pytest.fixture
def spans():
    """装一个内存 exporter 的 provider，yield exporter，测试完复位。

    【why】用 fixture 而不是每个测试手写：装/卸 provider 有固定顺序
    （重置 Once 锁 → set → 跑 → shutdown → 复位），漏复位会让后续测试
    读到脏 provider 或 span 收不到。
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # 重置"只设一次"锁，让本次 set_tracer_provider 生效
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    trace.set_tracer_provider(provider)
    try:
        yield exporter
    finally:
        # 优雅 shutdown（flush 剩余 span）
        provider.shutdown()
        # 复位到干净的 NoOp provider，并重置锁，避免污染其他测试文件
        trace._TRACER_PROVIDER_SET_ONCE = Once()
        trace.set_tracer_provider(TracerProvider())


def _attrs(spans, name: str) -> dict:
    """从 exporter 里取指定 span 名的属性 dict（取最后一个，覆盖重名场景）。

    属性在 span 里存为 {key: value}，InMemorySpanExporter 的 finished spans
    每个是 ReadableSpan，.attributes 是 dict[str, Any]。
    """
    matched = [s for s in spans.get_finished_spans() if s.name == name]
    assert matched, f"没找到 span {name}，现有: {[s.name for s in spans.get_finished_spans()]}"
    return dict(matched[-1].attributes)


# ── 工具函数 ──────────────────────────────────────────────────

def test_str_or_empty():
    """None 要转空串：OTel set_attribute 不接受 None（有删除语义）。"""
    assert _str_or_empty(None) == ""
    assert _str_or_empty("t1") == "t1"


# ── agent.step span ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_step_span_records_identity_and_outcome(spans):
    """step span 归属字段齐全，结束回填 verdict / terminated_by。"""
    from agent.contracts.decision import make_fake_state, make_fake_verdict

    set_run_context(task_id="task-1", item_id="S3.2.4", step=3)
    state = make_fake_state(task_id="task-1", item_id="S3.2.4")
    state.step = 3
    state.verdict = make_fake_verdict(verdict="PASS", confidence=0.95)
    state.terminated_by = "answer"

    async with step_span(state):
        pass

    a = _attrs(spans, "agent.step")
    assert a["task_id"] == "task-1"
    assert a["item_id"] == "S3.2.4"
    assert a["step"] == 3
    assert a["verdict"] == "PASS"
    assert a["confidence"] == 0.95
    assert a["terminated_by"] == "answer"
    assert a["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_step_span_marks_error_on_exception(spans):
    """step 抛异常：span 标记失败，异常原样上抛（不吞）。"""
    set_run_context(task_id="t", item_id="S", step=1)
    from agent.contracts.decision import make_fake_state

    with pytest.raises(RuntimeError):
        async with step_span(make_fake_state()):
            raise RuntimeError("boom")

    a = _attrs(spans, "agent.step")
    assert a["latency_ms"] >= 0  # 即使异常也记录了耗时（finally 保证）


@pytest.mark.asyncio
async def test_step_span_no_verdict_when_none(spans):
    """还没出结论就被打断时，只写 terminated_by，不写 verdict。"""
    set_run_context(task_id="t", item_id="S", step=5)
    from agent.contracts.decision import make_fake_state

    state = make_fake_state()
    state.terminated_by = "max_steps"     # 步数耗尽，没有 verdict

    async with step_span(state):
        pass

    a = _attrs(spans, "agent.step")
    assert "verdict" not in a            # 无结论不写 verdict 属性
    assert a["terminated_by"] == "max_steps"


# ── tool.call span ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_call_span_records_tool_name_and_hash(spans):
    """tool span 记录工具名 + 参数摘要（防打转排查用）。"""
    set_run_context(task_id="t", item_id="S", step=2)

    async with tool_call_span("search_in_bid", {"q": "达梦"}, args_digest="abc123"):
        pass

    a = _attrs(spans, "tool.call")
    assert a["tool_name"] == "search_in_bid"
    assert a["tool_args_hash"] == "abc123"
    assert a["task_id"] == "t"
    assert a["step"] == 2


@pytest.mark.asyncio
async def test_tool_call_span_marks_error(spans):
    """工具抛 ToolError：span 标记失败并上抛，loop 会捕获喂回模型。"""
    set_run_context(task_id="t", item_id="S", step=1)

    with pytest.raises(ValueError):
        async with tool_call_span("get_section", {"doc_id": "d1"}):
            raise ValueError("不存在")

    assert _attrs(spans, "tool.call")["latency_ms"] >= 0


# ── llm.call span ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_call_span_records_model_and_usage(spans):
    """llm span 记录模型名 + 回填 token 消耗。"""
    set_run_context(task_id="t", item_id="S", step=1)

    async with llm_call_span(model="qwen-plus", prompt_version="v1", index_version="idx1") as span:
        record_usage(span, prompt_tokens=120, completion_tokens=30, cost_cny=0.002)

    a = _attrs(spans, "llm.call")
    assert a["model"] == "qwen-plus"
    assert a["prompt_version"] == "v1"
    assert a["index_version"] == "idx1"
    assert a["prompt_tokens"] == 120
    assert a["completion_tokens"] == 30
    assert a["cost_cny"] == 0.002


@pytest.mark.asyncio
async def test_llm_call_span_optional_cost(spans):
    """cost 可空：成本换算函数未接时只记 token，不炸。"""
    set_run_context(task_id="t", item_id="S", step=1)

    async with llm_call_span(model="deepseek-chat") as span:
        record_usage(span, prompt_tokens=10, completion_tokens=5)

    a = _attrs(spans, "llm.call")
    assert a["prompt_tokens"] == 10
    assert "cost_cny" not in a          # 未传 cost 不写属性


@pytest.mark.asyncio
async def test_llm_call_span_marks_error(spans):
    """LLM 超时（UpstreamError）会被 span 标记失败。"""
    set_run_context(task_id="t", item_id="S", step=1)

    with pytest.raises(RuntimeError):
        async with llm_call_span(model="m"):
            raise RuntimeError("timeout")

    assert _attrs(spans, "llm.call")["latency_ms"] >= 0


# ── 无 provider（NoOp）也能安全跑 ─────────────────────────────

@pytest.mark.asyncio
async def test_spans_noop_without_provider():
    """没 setup provider 时，span 全部 NoOp，不炸不报错。

    【why】本地开发 / 未接 collector 的环境是常态，埋点必须静默降级，
    不能因为"没配观测后端"就把评审主流程搞挂。
    """
    # 复位到一个干净的 NoOp provider（默认 TracerProvider 就是 NoOp exporter）
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    trace.set_tracer_provider(TracerProvider())
    try:
        set_run_context(task_id="t", item_id="S", step=1)
        from agent.contracts.decision import make_fake_state

        async with step_span(make_fake_state()):
            pass
        async with tool_call_span("search_in_bid", {}):
            pass
        async with llm_call_span(model="m"):
            pass
        # 不抛异常即通过
    finally:
        trace._TRACER_PROVIDER_SET_ONCE = Once()
        trace.set_tracer_provider(TracerProvider())
