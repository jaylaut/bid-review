"""嵌入与重排测试。respx 拦截 Ollama；零网络。"""

from __future__ import annotations

import json
import math

import httpx
import pytest
import respx

from common.errors import ContractViolation
from models.embedding import OllamaEmbeddings
from models.rerank import OllamaReranker, build_prompt, yes_no_probability


def _unit_vec(dim: int = 1024) -> list[float]:
    # 构造一个 L2=1 的假向量（全同值向量，每个分量 = 1/sqrt(dim)）
    v = 1.0 / math.sqrt(dim)
    return [v] * dim


# ── 嵌入 ──────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_embed_query_carries_instruction_and_num_ctx():
    """【why】查询侧必须带指令、必须传 num_ctx——这是 docs/03 §4.4 的铁律，
    少一个就是静默质量劣化，所以在这里用断言钉死。"""
    route = respx.post("http://ollama/api/embed").respond(status_code=200, json={"embeddings": [_unit_vec()]})
    e = OllamaEmbeddings(endpoint="http://ollama")
    vec = await e.embed_query("支持国产数据库适配")
    assert len(vec) == 1024
    body = json.loads(route.calls.last.request.read())
    assert body["options"]["num_ctx"] == 1024
    assert body["input"][0].startswith("Instruct:")
    assert "Query: 支持国产数据库适配" in body["input"][0]
    await e.close()


@respx.mock
@pytest.mark.asyncio
async def test_embed_documents_batched_by_32():
    """batch_size=32：65 条 → 3 次请求（32/32/1）。吞吐杠杆，不是随便定的。"""
    seen_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(__import__("json").loads(request.read())["input"])
        seen_sizes.append(n)
        return httpx.Response(200, json={"embeddings": [_unit_vec() for _ in range(n)]})

    respx.post("http://ollama/api/embed").mock(side_effect=handler)
    e = OllamaEmbeddings(endpoint="http://ollama")
    out = await e.embed_documents([f"doc{i}" for i in range(65)])
    assert len(out) == 65
    assert seen_sizes == [32, 32, 1]
    await e.close()


@respx.mock
@pytest.mark.asyncio
async def test_embed_wrong_dim_fails_fast():
    """维度不对当场炸（ContractViolation），绝不带病入库。"""
    respx.post("http://ollama/api/embed").respond(status_code=200, json={"embeddings": [[0.1] * 768]})
    e = OllamaEmbeddings(endpoint="http://ollama")
    with pytest.raises(ContractViolation, match="维度"):
        await e.embed_query("x")
    await e.close()


@respx.mock
@pytest.mark.asyncio
async def test_embed_unnormalized_fails():
    respx.post("http://ollama/api/embed").respond(status_code=200, json={"embeddings": [[0.5] * 1024]})
    e = OllamaEmbeddings(endpoint="http://ollama")
    with pytest.raises(ContractViolation, match="归一化"):
        await e.embed_query("x")
    await e.close()


# ── 重排 ──────────────────────────────────────────────────────

def _ollama_rerank_payload(logprob_yes: float) -> dict:
    return {
        "response": "yes",
        "logprobs": [{"token": "yes", "top_logprobs": [
            {"token": "yes", "logprob": logprob_yes},
            {"token": "no", "logprob": math.log(1 - math.exp(logprob_yes) + 1e-12)},
        ]}],
    }


def test_yes_no_probability():
    p = yes_no_probability(_ollama_rerank_payload(math.log(0.93))["logprobs"])
    assert 0.92 < p < 0.94


def test_yes_no_probability_missing_tokens_raises():
    """【why】yes/no 缺失必须炸而不是猜分数：猜的分污染拒答阈值。"""
    with pytest.raises(ValueError):
        yes_no_probability([{"token": "yes", "top_logprobs": [{"token": "yes", "logprob": -0.1}]}])


def test_build_prompt_template():
    p = build_prompt("支持达梦适配", "我方已完成DM8适配")
    assert "<Instruct>" in p and "<Query>支持达梦适配</Query>" in p
    assert "<Document>我方已完成DM8适配</Document>" in p


@respx.mock
@pytest.mark.asyncio
async def test_rerank_success_returns_scores():
    respx.post("http://ollama/api/generate").mock(
        side_effect=[httpx.Response(200, json=_ollama_rerank_payload(math.log(0.9))),
                     httpx.Response(200, json=_ollama_rerank_payload(math.log(0.2)))]
    )
    r = OllamaReranker(endpoint="http://ollama")
    scores, degraded = await r.rerank("达梦", ["相关内容", "无关内容"])
    assert degraded == "none"
    assert scores[0] > scores[1]
    await r.close()


@respx.mock
@pytest.mark.asyncio
async def test_rerank_degrades_to_no_rerank_when_ollama_down():
    """三级降级末端：本地挂 + 云未配 → 原序 0.5 分，degraded=no_rerank。
    【why】降级必须可观测——0.5 分不带偏好，排序退回 RRF 序。"""
    respx.post("http://ollama/api/generate").mock(side_effect=httpx.ConnectTimeout("down"))
    r = OllamaReranker(endpoint="http://ollama")
    scores, degraded = await r.rerank("达梦", ["a", "b", "c"])
    assert degraded == "no_rerank"
    assert scores == [0.5, 0.5, 0.5]
    await r.close()


@respx.mock
@pytest.mark.asyncio
async def test_rerank_timeout_is_retriable_upstream_error():
    """单项超时抛 UpstreamError(retriable)——rerank() 捕获后走降级，
    但错误语义保留给上层日志。"""
    respx.post("http://ollama/api/generate").mock(side_effect=httpx.ReadTimeout("slow"))
    r = OllamaReranker(endpoint="http://ollama")
    scores, degraded = await r.rerank("达梦", ["a"])
    assert degraded == "no_rerank" and scores == [0.5]
    await r.close()
