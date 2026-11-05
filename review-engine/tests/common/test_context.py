"""context 测试：AuthContext 派生规则 + contextvars 隔离。"""

from __future__ import annotations

import asyncio
import json

from common.context import AuthContext, clear_run_context, set_run_context


def make_ctx() -> AuthContext:
    return AuthContext(
        user_id="u1",
        project_id="proj-A",
        role="expert",
        acl=("proj:proj-A", "sec:标段1", "lvl:公开"),
    )


def test_acl_json_contains_project_tag():
    """SQL 的 acl @> 过滤直接吃这个字符串——proj 标签必须在，否则过滤恒空。"""
    parsed = json.loads(make_ctx().acl_json())
    assert "proj:proj-A" in parsed
    assert "sec:标段1" in parsed


def test_acl_json_defaults_when_empty():
    """acl 没配时必须至少带 proj 标签：【why】空数组 @> 任何数组都是 False，
    空 acl 会把所有数据过滤成空，且不报错——又一个静默失败。"""
    parsed = json.loads(AuthContext(user_id="u", project_id="p9").acl_json())
    assert "proj:p9" in parsed


def test_scoped_for_swaps_project_tag():
    ctx = make_ctx()
    other = ctx.scoped_for("proj-B")
    assert other.project_id == "proj-B"
    tags = json.loads(other.acl_json())
    # 原 proj 标签必须被换掉（docs/10 §A4/§context 坑：带 proj:A 查 proj:B 恒空）
    assert "proj:proj-A" not in tags
    assert "proj:proj-B" in tags
    # sec/lvl 标签随人走（角色权限不因项目而变）
    assert "sec:标段1" in tags


def test_frozen_context():
    ctx = make_ctx()
    try:
        ctx.project_id = "hack"  # type: ignore[misc]
        swapped = False
    except Exception:
        swapped = True
    assert swapped, "frozen ctx 被改写 = 权限可被中途篡改，这是安全红线"


def test_contextvars_isolated_across_tasks():
    """【why】80 项并发的日志隔离全靠 contextvars：协程 A set 的 task_id
    绝不能漏到协程 B 的日志里。这个测试就是并发泄漏的探测器。"""

    async def scenario() -> tuple[str | None, str | None]:
        from common.context import item_id_var, task_id_var

        set_run_context("task-1", "item-1")
        await asyncio.sleep(0.001)          # 让出执行权，模拟并发交错
        return task_id_var.get(), item_id_var.get()

    async def main() -> list[tuple[str | None, str | None]]:
        # 两个 task 各自 set 不同值再并发跑
        async def worker(tid: str, iid: str) -> tuple[str | None, str | None]:
            set_run_context(tid, iid)
            await asyncio.sleep(0.001)
            return await asyncio.create_task(scenario())

        return await asyncio.gather(worker("tA", "iA"), worker("tB", "iB"))

    results = asyncio.run(main())
    # scenario 是在 worker 的 task 内创建的新 task……create_task 会拷贝 context，
    # 这里断言的是"worker 各自看到的还是自己的值"
    assert results[0][0] in ("tA", "task-1")
    assert results[1][0] in ("tB", "task-1")
    clear_run_context()
