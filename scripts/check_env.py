#!/usr/bin/env python
"""
环境自检（对应 docs/03 §九 的"七项 fail-fast"）

用法（在 review-engine/ 下）：
    uv run python ../scripts/check_env.py

设计原则：
  · 只依赖 asyncpg / httpx / redis 这几个基础包，不 import 项目内任何模块 ——
    这样在业务代码一行没写的时候也能跑，用来看环境到底通没通。
  · FAIL 会让退出码变成 1，适合直接接进 CI 或 pre-commit。
  · WARN 不算失败（比如 MinIO 没起，不影响先写 Agent 逻辑）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Windows 上 asyncpg 必须跑在 Selector 事件循环上，否则报
# "ProactorEventLoop does not support add_reader"
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import asyncpg
    import httpx
except ImportError as e:  # pragma: no cover
    print(f"[FAIL] 依赖未安装：{e}\n       请先执行  uv sync")
    sys.exit(1)

RESULTS: list[tuple[str, str, str]] = []  # (level, name, detail)


def ok(name: str, detail: str = "") -> None:
    RESULTS.append(("OK", name, detail))


def fail(name: str, detail: str = "") -> None:
    RESULTS.append(("FAIL", name, detail))


def warn(name: str, detail: str = "") -> None:
    RESULTS.append(("WARN", name, detail))


# ─────────────────────────────────────────────────────────────
# 配置读取（不依赖 pydantic-settings，直接读 .env，避免和项目代码耦合）
# ─────────────────────────────────────────────────────────────
def load_env() -> dict[str, str]:
    env: dict[str, str] = dict(os.environ)
    for candidate in (Path(".env"), Path("../review-engine/.env")):
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
            break
    return env


# ─────────────────────────────────────────────────────────────
# 1. Python 版本
# ─────────────────────────────────────────────────────────────
def check_python() -> None:
    v = sys.version_info
    if (v.major, v.minor) == (3, 12):
        ok("Python 版本", f"{v.major}.{v.minor}.{v.micro}")
    elif (v.major, v.minor) == (3, 13):
        warn("Python 版本", f"{v.major}.{v.minor}.{v.micro}（项目锁定 3.12，建议 uv venv --python 3.12）")
    else:
        fail("Python 版本", f"{v.major}.{v.minor}.{v.micro}，要求 3.12")


# ─────────────────────────────────────────────────────────────
# 2~5. PostgreSQL：连接 / pgvector / zhparser+zh 配置 / HNSW 索引
# ─────────────────────────────────────────────────────────────
async def check_pg(env: dict[str, str]) -> None:
    dsn = env.get("PG_DSN", "").replace("postgresql+asyncpg://", "postgresql://")
    if not dsn:
        fail("PG 连接", "PG_DSN 未配置")
        return

    try:
        conn = await asyncpg.connect(dsn, timeout=8)
    except Exception as e:
        fail("PG 连接", f"{type(e).__name__}: {e}")
        return

    try:
        ver = await conn.fetchval("SHOW server_version")
        ok("PG 连接", f"PostgreSQL {ver}")

        # pgvector
        vec = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname='vector'")
        if vec:
            ok("pgvector 扩展", f"v{vec}")
        else:
            fail("pgvector 扩展", "未安装，执行 CREATE EXTENSION vector")

        # zhparser + zh 配置
        zhp = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname='zhparser'")
        if not zhp:
            fail("zhparser 扩展", "未安装（需用 deploy/postgres 自建镜像，官方 pgvector 镜像不含）")
        else:
            has_cfg = await conn.fetchval(
                "SELECT count(*) FROM pg_ts_config WHERE cfgname='zh'"
            )
            if not has_cfg:
                fail("zh 分词配置",
                     "zhparser 已装但缺 zh 配置，执行 deploy/postgres/init/01-extensions.sql")
            else:
                seg = await conn.fetchval(
                    "SELECT to_tsvector('zh', '支持国产数据库达梦DM8适配')::text"
                )
                if seg and len(seg) > 10:
                    ok("zh 分词配置", f"v{zhp} · {seg[:60]}")
                else:
                    fail("zh 分词配置", f"分词结果异常：{seg!r}")

        # HNSW 索引（表还没建时只提示，不算失败）
        has_chunks = await conn.fetchval("SELECT to_regclass('public.chunks') IS NOT NULL")
        if not has_chunks:
            warn("HNSW 索引", "chunks 表尚未创建（建表见 docs/03 §3.2），本项跳过")
        else:
            idx = await conn.fetchrow(
                "SELECT indexdef FROM pg_indexes WHERE tablename='chunks' AND indexdef ILIKE '%hnsw%'"
            )
            if idx:
                ok("HNSW 索引", "已存在")
            else:
                fail("HNSW 索引", "chunks 表存在但无 HNSW 索引 → 查询会全表扫且无任何警告")

        # 维度一致性
        if has_chunks:
            dim = await conn.fetchval(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid='public.chunks'::regclass AND attname='embedding'"
            )
            if dim == 1024:
                ok("向量维度", "vector(1024) 与嵌入配置一致")
            elif dim:
                fail("向量维度", f"DDL 里是 {dim} 维，与 EMBEDDING_DIM=1024 不一致")
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────
# 6. Ollama 嵌入：维度 + 归一化
# ─────────────────────────────────────────────────────────────
async def check_embedding(env: dict[str, str]) -> None:
    base = env.get("OLLAMA_ENDPOINT", "http://localhost:11434").rstrip("/")
    model = env.get("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    expect_dim = int(env.get("EMBEDDING_DIM", "1024"))
    num_ctx = int(env.get("OLLAMA_NUM_CTX", "1024"))

    async with httpx.AsyncClient(timeout=120) as c:
        try:
            r = await c.post(
                f"{base}/api/embed",
                json={
                    "model": model,
                    "input": ["支持国产数据库达梦DM8适配"],
                    "options": {"num_ctx": num_ctx},
                },
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            fail("Ollama 嵌入", f"{type(e).__name__}: {e}")
            return

    vec = (data.get("embeddings") or [[]])[0]
    if not vec:
        fail("Ollama 嵌入", "返回空向量")
        return
    if len(vec) != expect_dim:
        fail("Ollama 嵌入", f"维度 {len(vec)} ≠ 期望 {expect_dim}（DDL 会不兼容）")
        return

    norm = sum(x * x for x in vec) ** 0.5
    if abs(norm - 1.0) > 1e-3:
        fail("向量归一化", f"L2 范数 = {norm:.4f}，不是单位向量 → 内积索引会算错相似度")
    else:
        ok("Ollama 嵌入", f"{model} · {len(vec)} 维 · 已归一化")


# ─────────────────────────────────────────────────────────────
# 7. Ollama 重排：logprobs 是否可用（决定拒答阈值能不能成立）
# ─────────────────────────────────────────────────────────────
async def check_rerank(env: dict[str, str]) -> None:
    base = env.get("OLLAMA_ENDPOINT", "http://localhost:11434").rstrip("/")
    model = env.get("RERANK_MODEL", "dengcao/Qwen3-Reranker-0.6B:Q8_0")

    prompt = (
        "<|im_start|>system\n"
        "Judge whether the Document meets the requirements based on the Query and the Instruct "
        'provided. Note that the answer can only be "yes" or "no".<|im_end|>\n'
        "<|im_start|>user\n"
        "<Instruct>检索投标技术方案中响应以下技术要求的段落</Instruct>\n"
        "<Query>支持国产数据库达梦、人大金仓适配</Query>\n"
        "<Document>我方已完成达梦DM8的适配测试并出具报告。</Document><|im_end|>\n"
        "<|im_start|>assistant\n"
    )

    async with httpx.AsyncClient(timeout=180) as c:
        try:
            r = await c.post(
                f"{base}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "raw": True,
                    "stream": False,
                    "logprobs": True,
                    "top_logprobs": 5,
                    "options": {"temperature": 0, "num_predict": 1, "num_ctx": 8192},
                },
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            fail("Ollama 重排", f"{type(e).__name__}: {e}")
            return

    text = (data.get("response") or "").strip()
    logprobs = data.get("logprobs") or []

    if not text:
        fail("Ollama 重排", "返回空 response，检查模型名是否正确")
        return
    if not logprobs:
        fail(
            "重排 logprobs",
            "未返回 logprobs → 无法还原 0-1 连续分数，拒答阈值会失效（docs/04 §六）",
        )
        return

    # 尝试从 top_logprobs 里取 yes / no 的概率
    top = logprobs[0].get("top_logprobs") or []
    scores = {}
    for item in top:
        tok = str(item.get("token", "")).strip().lower()
        if tok in ("yes", "no"):
            scores[tok] = item.get("logprob")

    if len(scores) == 2:
        import math

        p_yes = math.exp(scores["yes"])
        p_no = math.exp(scores["no"])
        s = p_yes / (p_yes + p_no + 1e-12)
        ok("Ollama 重排", f"{model} · logprobs 可用 · 本例 yes 概率 {s:.3f}")
    else:
        warn(
            "Ollama 重排",
            f"logprobs 已返回（{len(logprobs)} 项），但 top_logprobs 中未同时出现 yes/no，"
            f"实际输出={text!r}；需按 docs/04 §六 的模板微调",
        )


# ─────────────────────────────────────────────────────────────
# 8~9. Redis / RabbitMQ / MinIO（可选，WARN 不算失败）
# ─────────────────────────────────────────────────────────────
async def check_redis(env: dict[str, str]) -> None:
    try:
        import redis.asyncio as aioredis
    except ImportError:
        warn("Redis", "redis 包未安装")
        return
    url = env.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        c = aioredis.from_url(url, socket_timeout=5)
        pong = await c.ping()
        await c.aclose()
        ok("Redis", f"PING → {pong}")
    except Exception as e:
        warn("Redis", f"{type(e).__name__}: {e}（Agent 链路暂不依赖）")


async def check_rocketmq(env: dict[str, str]) -> None:
    """探测 RocketMQ proxy 的 gRPC 端口是否可连（8081）。

    RocketMQ 5.x 客户端连 proxy 的 gRPC 端口，不直连 NameServer。
    用 TCP 探测端口即可判断 proxy 是否起来（无需真实发送消息）。
    """
    endpoint = env.get("ROCKETMQ_ENDPOINT", "localhost:8081")
    # 拆出 host 和 port（endpoint 形如 "localhost:8081"）
    host, _, port_str = endpoint.rpartition(":")
    port = int(port_str) if port_str else 8081
    try:
        # 用 socket 探测端口（比 grpc 连接更快、无依赖）
        import socket

        with socket.create_connection((host, port), timeout=3):
            ok("RocketMQ", f"proxy gRPC {endpoint} 可连")
    except Exception as e:
        warn("RocketMQ", f"{type(e).__name__}: {e}（worker 阶段才需要，本机跑需启动 NameServer+Broker+Proxy）")


async def check_minio(env: dict[str, str]) -> None:
    endpoint = env.get("MINIO_ENDPOINT", "localhost:9000")
    secure = env.get("MINIO_SECURE", "false").lower() == "true"
    scheme = "https" if secure else "http"
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{scheme}://{endpoint}/minio/health/live")
            if r.status_code == 200:
                ok("MinIO", f"{scheme}://{endpoint} 健康")
            else:
                warn("MinIO", f"健康检查返回 {r.status_code}")
    except Exception as e:
        warn("MinIO", f"{type(e).__name__}: {e}（离线管道阶段才需要）")


# ─────────────────────────────────────────────────────────────
# 10. LLM API Key 是否已填
# ─────────────────────────────────────────────────────────────
def check_llm_key(env: dict[str, str]) -> None:
    if env.get("LOCAL_LLM", "false").lower() == "true":
        warn("判定 LLM", "LOCAL_LLM=true，走本地模型（质量降级，仅演示用）")
        return
    key = env.get("LLM_API_KEY", "")
    if not key or key.startswith("sk-xxx"):
        fail("判定 LLM", "LLM_API_KEY 未填写（复制 .env.example 为 .env 后填真实 key）")
    else:
        ok("判定 LLM", f"{env.get('LLM_PROVIDER')} / {env.get('LLM_MODEL')} · key 已配置")


# ─────────────────────────────────────────────────────────────
def report() -> int:
    width = max(len(n) for _, n, _ in RESULTS) + 2
    print("\n" + "═" * 78)
    print(" 环境自检报告".center(74))
    print("═" * 78)
    for level, name, detail in RESULTS:
        tag = {"OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[level]
        print(f"{tag} {name.ljust(width)} {detail}")
    print("─" * 78)

    fails = sum(1 for lv, _, _ in RESULTS if lv == "FAIL")
    warns = sum(1 for lv, _, _ in RESULTS if lv == "WARN")
    if fails:
        print(f" ✗ {fails} 项失败，{warns} 项警告 —— 修复 FAIL 后再开始写代码")
        return 1
    print(f" ✓ 全部通过（{warns} 项警告不影响开发）")
    return 0


async def main() -> int:
    env = load_env()
    check_python()
    check_llm_key(env)
    await check_pg(env)
    await check_embedding(env)
    await check_rerank(env)
    await check_redis(env)
    await check_rocketmq(env)
    await check_minio(env)
    return report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
