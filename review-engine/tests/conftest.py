"""全局测试夹具。

关键：asyncpg 连接池绑定创建它的事件循环，而 pytest-asyncio 默认
每个测试新建 loop——不复位的话第二个真库测试会报
"Event loop is closed / another operation is in progress"。
autouse 夹具在每个测试后关闭池，保证下个测试拿到干净状态。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _reset_db_pool():
    yield
    from common import db

    await db.close_pool()
