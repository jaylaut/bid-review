"""config 测试。注意：.env 可能不存在（环境准备未完成时全部走默认值），
本文件不能依赖 .env 的存在——依赖它的键值测试放在 test_db 的 skipif 后面。"""

from __future__ import annotations

import pytest

from common.config import Settings, get_settings, require_llm_key


def test_default_values_match_docs():
    """默认值必须和 docs/10 §A6/A7 的决策一致——改了文档忘了改代码（或反过来）在这暴露。"""
    s = Settings(_env_file=None)  # 显式忽略 .env，纯默认值
    assert s.agent_max_steps == 6
    assert s.agent_llm_concurrency == 8
    assert s.agent_rerank_concurrency == 2
    assert s.pg_pool_max == 40
    assert s.embedding_dim == 1024
    assert s.rerank_refusal_threshold == 0.15
    assert s.embedding_model == "qwen3-embedding:0.6b"


def test_pg_dsn_dialect_stripped():
    """SQLAlchemy 方言前缀必须剥掉。坑点见 config.pg_dsn_asyncprop 的注释。"""
    s = Settings(_env_file=None, pg_dsn="postgresql+asyncpg://u:p@h:5432/db")
    assert s.pg_dsn_asyncpg == "postgresql://u:p@h:5432/db"
    # 原字段不动：其他地方（如果真用 SQLAlchemy）还能拿到带方言的
    assert s.pg_dsn == "postgresql+asyncpg://u:p@h:5432/db"


def test_env_override(tmp_path, monkeypatch):
    """env 变量覆盖默认值——pydantic-settings 的基本行为要验证一次。
    【why】字段名拼写错（agent_max_step vs agent_max_steps）不会报错，
    只会静默用默认值，这里用一条真实覆盖测试兜住命名映射。"""
    monkeypatch.setenv("AGENT_MAX_STEPS", "9")
    monkeypatch.setenv("PG_POOL_MAX", "55")
    s = Settings(_env_file=None)
    assert s.agent_max_steps == 9
    assert s.pg_pool_max == 55


def test_extra_env_ignored(tmp_path, monkeypatch):
    """无关 env 不炸（.env 里可能有注释残留/其他项目的键）。"""
    monkeypatch.setenv("SOME_OTHER_PROJECT_VAR", "x")
    Settings(_env_file=None)  # 不抛 ValidationError 即通过


def test_get_settings_is_singleton():
    assert get_settings() is get_settings()


def test_require_llm_key_raises_when_missing(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    monkeypatch.setattr(get_settings(), "local_llm", False)
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        require_llm_key()


def test_require_llm_key_returns_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "sk-test-123")
    assert require_llm_key() == "sk-test-123"
