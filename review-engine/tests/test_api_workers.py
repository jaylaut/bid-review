"""api + workers 测试：TestClient 打 health / 422 / 幂等回调；process_task 编排。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import app
from workers import process_review_task

# ── API ───────────────────────────────────────────────────────

@pytest.fixture()
def client():
    return TestClient(app)


def test_health(client, monkeypatch):
    """PG ping 失败 → degraded 而不是 5xx：【why】健康检查的语义是
    '实例活着吗'，依赖坏了是降级不是死亡。"""
    from common import db

    async def fail():
        return False

    monkeypatch.setattr(db, "ping", fail)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"


def test_retrieve_422_on_empty_query(client):
    r = client.post("/retrieve", json={"query": "  ", "project_id": "p1"})
    assert r.status_code == 422


def test_resume_404_without_checkpoint(client):
    r = client.post("/internal/review/nope/nope/resume", json={
        "task_id": "nope", "item_id": "nope", "action": "accept", "actor": "x"})
    assert r.status_code == 404


def test_resume_idempotent_on_done(client):
    """已 done 的项重复回调 → already_done，不报错不重算（A5 幂等验收）。"""
    import asyncio

    from agent.contracts.decision import make_fake_state, make_fake_verdict
    from agent.state import MemoryCheckpointer

    ck = MemoryCheckpointer()
    st = make_fake_state(task_id="tapi", item_id="S1")
    st.status = "done"
    st.verdict = make_fake_verdict()
    asyncio.run(ck.save(st))

    import api as api_mod
    monkey_patch = MemoryCheckpointer()
    monkey_patch._data = ck._data
    monkeypatch_setattr = pytest.MonkeyPatch()
    monkeypatch_setattr.setattr(api_mod, "get_checkpointer", lambda: monkey_patch)

    r = client.post("/internal/review/tapi/S1/resume", json={
        "task_id": "tapi", "item_id": "S1", "action": "accept",
        "actor": "expert", "final_verdict": None})
    body = r.json()
    assert body["status"] == "already_done"
    monkeypatch_setattr.undo()


# ── workers.process_review_task ───────────────────────────────

@pytest.mark.asyncio
async def test_process_review_task_summarizes():
    """编排函数：正常项出 done，无 verdict 项不算 done。"""
    from agent.contracts.decision import Decision, make_fake_verdict
    from agent.contracts.fake_llm import FakeTools
    from agent.interrupt import CollectingSink
    from agent.state import MemoryCheckpointer

    items = [{"item_code": f"S{i}", "item_text": "t", "requirement": {"must": []}}
             for i in range(3)]

    class LLM:
        async def decide(self, state, specs):
            return Decision(action="final", verdict=make_fake_verdict())

    deps = {"llm": LLM(), "tools": FakeTools(), "checkpointer": MemoryCheckpointer(),
            "sink": CollectingSink()}
    out = await process_review_task(
        {"task_id": "tk", "project_id": "p1", "scoring_items": items,
         "user_id": "u", "acl": ["proj:p1"]}, deps=deps)
    assert out["status"] == "DONE"
    assert out["done"] == 3 and out["total"] == 3
    assert all(v["verdict"] == "PARTIAL" for v in out["verdicts"])


def test_lock_held_is_importable_from_agent_state():
    """LockHeld 从 common.errors 来、agent.state 转出口——协议一致性的小锚点。"""
    from agent.state import LockHeld as FromState
    from common.errors import LockHeld as FromCommon

    assert FromState is FromCommon
