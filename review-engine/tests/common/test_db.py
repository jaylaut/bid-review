"""db 测试。PG 容器没起时全部 skip（环境准备是前置，但不阻塞单元测试）。

skip 的探测方式用 socket 而不是 try-connect：【why】连接超时要等 5-30s，
socket connect_ex 对未监听端口立即返回，测试套件不因环境缺失而变慢。
"""

from __future__ import annotations

import socket

import pytest

from common.config import get_settings

_pg_up = False
try:
    host, port = "localhost", 5432
    _cfg = get_settings()
    _dsn_host = _cfg.pg_dsn_asyncpg.split("@")[-1].split("/")[0]
    if ":" in _dsn_host:
        host, port = _dsn_host.rsplit(":", 1)
        port = int(port)
    with socket.create_connection((host, port), timeout=0.5):
        _pg_up = True
except OSError:
    _pg_up = False

pytestmark = pytest.mark.skipif(not _pg_up, reason="PG 未启动（docker compose up -d）")


@pytest.mark.asyncio()
async def test_pool_ping():
    from common import db

    assert await db.ping() is True


@pytest.mark.asyncio()
async def test_pool_reused():
    """【why】池是单例：两次 fetch 用的是同一个池（连接复用），
    如果每次新建，40 并发下 PG 连接数会爆。"""
    from common import db

    await db.fetch_val("SELECT 1")
    pool1 = await db.get_pool()
    await db.fetch_val("SELECT 1")
    pool2 = await db.get_pool()
    assert pool1 is pool2


@pytest.mark.asyncio()
async def test_transaction_rollback():
    """事务内抛异常 → 回滚。【why】A4 的软删+重写依赖这个语义，
    先在基础层证明它。"""
    from common import db

    # 回滚语义：异常向上传播（调用方感知失败）+ 事务内全部变更撤销
    with pytest.raises(_Boom):
        async with db.transaction() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _tx_test(id int)"
            )
            await conn.execute("INSERT INTO _tx_test VALUES (1)")
            raise _Boom()

    # 回滚后表不存在（CREATE TABLE 也在事务里）
    val = await db.fetch_val(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name='_tx_test')"
    )
    assert val is False


class _Boom(Exception):
    pass
