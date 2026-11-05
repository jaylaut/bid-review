"""LLM 客户端测试。respx 拦截全部 HTTP——零网络。

覆盖：正常 / 429 重试 / 4xx 不重试 / 超时 / 结构化输出解析与重试 / JSON 提取兜底。
"""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import BaseModel

from common.errors import ContractViolation, UpstreamError
from models.llm import LLMClient, _extract_json
from models.types import Usage


class Out(BaseModel):
    verdict: str
    confidence: float


def _client() -> LLMClient:
    return LLMClient(base_url="http://fake-llm", api_key="sk-test", model="m1")


def _ok_payload(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


@respx.mock
@pytest.mark.asyncio
async def test_complete_returns_content_and_reports_usage():
    route = respx.post("http://fake-llm/chat/completions").respond(json=_ok_payload("你好"))
    usages: list[Usage] = []
    c = _client()
    async def _u(u): usages.append(u)
    c._on_usage = _u
    out = await c.complete("hi")
    assert out == "你好"
    assert route.called
    assert usages == [Usage(prompt_tokens=10, completion_tokens=5)]
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_429_retries_then_succeeds():
    """429 可重试：前两次 429，第三次成功——tenacity 指数退避真的在跑。"""
    route = respx.post("http://fake-llm/chat/completions")
    route.side_effect = [
        httpx.Response(429, json={"e": "rate"}),
        httpx.Response(429, json={"e": "rate"}),
        httpx.Response(200, json=_ok_payload("ok")),
    ]
    c = _client()
    out = await c.complete("hi")
    assert out == "ok"
    assert route.call_count == 3
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_400_does_not_retry():
    """【why】4xx 重试无意义（key 错/schema 不支持），重试只是放大故障。"""
    route = respx.post("http://fake-llm/chat/completions").respond(status_code=400, text="bad")
    c = _client()
    with pytest.raises(UpstreamError) as ei:
        await c.complete("hi")
    assert ei.value.retriable is False
    assert route.call_count == 1
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_timeout_is_retriable():
    respx.post("http://fake-llm/chat/completions").mock(side_effect=httpx.ConnectTimeout("t"))
    c = _client()
    with pytest.raises(UpstreamError) as ei:
        await c.complete("hi")
    assert ei.value.retriable is True
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_chat_json_valid_first_try():
    respx.post("http://fake-llm/chat/completions").respond(
        json=_ok_payload('{"verdict": "PASS", "confidence": 0.9}')
    )
    c = _client()
    out = await c.chat_json("判一下", Out)
    assert out.verdict == "PASS" and out.confidence == 0.9
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_chat_json_retries_with_validation_error_then_succeeds():
    """【why】第一次输出非法 → ValidationError 喂回去 → 第二次改对。
    这是 A6 证据校验重试同款策略的地基。"""
    route = respx.post("http://fake-llm/chat/completions")
    route.side_effect = [
        httpx.Response(200, json=_ok_payload('{"verdict": "MAYBE", "confidence": "高"}')),
        httpx.Response(200, json=_ok_payload('{"verdict": "PASS", "confidence": 0.9}')),
    ]
    c = _client()
    out = await c.chat_json("判一下", Out)
    assert out.verdict == "PASS"
    assert route.call_count == 2
    # 第二次请求的消息里应包含错误反馈（assistant + 修正指令）
    second_body = route.calls.last.request.read()
    assert "校验失败" in second_body.decode()
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_chat_json_exhausts_retries_raises_contract_violation():
    respx.post("http://fake-llm/chat/completions").respond(json=_ok_payload("完全不是JSON"))
    c = _client()
    with pytest.raises(ContractViolation):
        await c.chat_json("判一下", Out)
    await c.close()


# ── _extract_json 兜底链 ──────────────────────────────────────

def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_code_block():
    assert _extract_json('好的，结果如下：\n```json\n{"a": 2}\n```\n以上。') == {"a": 2}


def test_extract_json_embedded_braces():
    assert _extract_json('判定结果 {"a": 3} 请查收') == {"a": 3}


def test_extract_json_none_found():
    with pytest.raises(ValueError):
        _extract_json("没有任何结构化内容")
