"""RocketMQ 客户端测试。

验证点：
1. 配置字段从 RabbitMQ 正确迁移到 RocketMQ（config 层）
2. 客户端构造、endpoint 归一化、client_id 生成
3. send/receive/ack/to_dead_letter 四个方法的请求组装逻辑（用 fake stub 拦截，
   不真实连接 RocketMQ——真连验证等 NameServer/Broker/Proxy 启动后做）

设计说明：RocketMQ 是外部中间件，单测不依赖它（和 test_db 的 skip 策略不同，
这里用 fake stub 替换 gRPC 调用，验证"我们发的请求对不对"而不是"RocketMQ 收不收"）。
真实联调放在脚本层（scripts/），需要三进程起来后手动跑。
"""

from __future__ import annotations

import pytest

from apache.rocketmq.v2 import definition_pb2 as pb
from rocketmq.client import (
    RocketMQClient,
    _normalize_endpoint,
    _seconds_duration,
)

# ── endpoint 归一化 ──────────────────────────────────────────

def test_normalize_endpoint_adds_default_port():
    """没写端口时补上默认 gRPC 端口 8081。"""
    assert _normalize_endpoint("localhost") == "localhost:8081"


def test_normalize_endpoint_keeps_explicit_port():
    """写了端口就原样保留。"""
    assert _normalize_endpoint("localhost:9090") == "localhost:9090"


def test_seconds_duration():
    """秒数转 protobuf Duration：seconds 正确、nanos 为 0。"""
    d = _seconds_duration(60)
    assert d.seconds == 60
    assert d.nanos == 0


# ── 配置字段迁移 ─────────────────────────────────────────────

def test_config_has_rocketmq_fields():
    """config 里 RocketMQ 字段齐全，RabbitMQ 字段已移除。"""
    from common.config import Settings

    # 直接构造 Settings，不读 .env，测默认值
    s = Settings(_env_file=None)
    # RocketMQ 新字段存在
    assert s.rocketmq_endpoint == "localhost:8081"
    assert s.rocketmq_topic_review == "bid-review"
    assert s.rocketmq_group_review == "bid-review-consumer"
    # RabbitMQ 旧字段已删除（访问会抛 AttributeError）
    assert not hasattr(s, "rabbitmq_url")
    assert not hasattr(s, "queue_review")


# ── 客户端构造 ───────────────────────────────────────────────

def test_client_constructs():
    """客户端能构造：endpoint 归一化 + client_id 生成。"""
    c = RocketMQClient("localhost")
    assert c._endpoint == "localhost:8081"
    assert c._client_id.startswith("py-")
    # 显式传 client_id 则用传的
    c2 = RocketMQClient("localhost:8081", client_id="my-worker")
    assert c2._client_id == "my-worker"


# ── send 请求组装（fake stub 拦截验证）────────────────────────

class _FakeStub:
    """拦截 gRPC stub 调用，记录收到的 request 并返回预设 response。

    【why】不真连 RocketMQ，只验证"我们构造的请求字段对不对"。
    """

    def __init__(self) -> None:
        self.sent_requests = []          # 记录所有 SendMessage 收到的 request

    async def SendMessage(self, request, metadata=None):
        self.sent_requests.append(request)
        # 构造一个"成功"响应（status.code = OK）
        resp = _ok_send_response()
        return resp

    async def close(self):
        pass


def _ok_send_response():
    from apache.rocketmq.v2 import service_pb2 as svc

    resp = svc.SendMessageResponse()
    resp.status.code = pb.Code.OK
    entry = resp.entries.add()
    entry.message_id = "msg-123"
    return resp


@pytest.mark.asyncio
async def test_send_builds_correct_request():
    """send 构造的请求：topic/body/system_properties 都正确。"""
    c = RocketMQClient("localhost:8081")
    fake = _FakeStub()
    c._stub = fake            # 替换成 fake stub

    msg_id = await c.send("bid-review", b'{"a":1}', keys=["task-1"], tag="t")

    # 断言返回的 message_id
    assert msg_id == "msg-123"
    # 断言请求字段
    req = fake.sent_requests[0]
    assert len(req.messages) == 1
    m = req.messages[0]
    assert m.topic.name == "bid-review"
    assert m.body == b'{"a":1}'
    assert m.system_properties.message_id != ""       # 客户端生成的非空 ID
    assert list(m.system_properties.keys) == ["task-1"]
    assert m.system_properties.tag == "t"
    await c.close()


@pytest.mark.asyncio
async def test_send_raises_on_non_ok():
    """服务端返回非 OK → 抛 UpstreamError（retriable）。"""
    from common.errors import UpstreamError

    class _FailStub:
        async def SendMessage(self, request, metadata=None):
            from apache.rocketmq.v2 import service_pb2 as svc

            resp = svc.SendMessageResponse()
            resp.status.code = pb.Code.BAD_REQUEST     # 40000 非 OK
            resp.status.message = "bad"
            return resp

    c = RocketMQClient("localhost:8081")
    c._stub = _FailStub()
    with pytest.raises(UpstreamError):
        await c.send("t", b"x")
    await c.close()


# ── receive / ack / to_dead_letter 请求组装 ───────────────────

class _FakeRecvStub:
    """模拟 ReceiveMessage 的流式返回：返回一条消息。"""

    def __init__(self) -> None:
        self.ack_requests = []
        self.dlq_requests = []

    def ReceiveMessage(self, request, metadata=None):
        # 记录 request 供断言（batch_size 等）
        self.recv_request = request
        # 【关键】grpc.aio 的 unary-stream RPC 调用是「同步返回 async iterable」，
        # 不是 awaitable。所以这里必须是普通 def（不是 async def），
        # 直接返回 async generator 对象供调用方的 `async for` 迭代。
        return _fake_recv_stream()

    async def AckMessage(self, request, metadata=None):
        self.ack_requests.append(request)
        from apache.rocketmq.v2 import service_pb2 as svc

        resp = svc.AckMessageResponse()
        resp.status.code = pb.Code.OK
        return resp

    async def ForwardMessageToDeadLetterQueue(self, request, metadata=None):
        self.dlq_requests.append(request)
        from apache.rocketmq.v2 import service_pb2 as svc

        resp = svc.ForwardMessageToDeadLetterQueueResponse()
        resp.status.code = pb.Code.OK
        return resp

    async def close(self):
        pass


def _fake_recv_stream():
    """模拟 ReceiveMessage 的 unary-stream：yield 一条消息响应。"""
    from apache.rocketmq.v2 import service_pb2 as svc

    async def gen():
        resp = svc.ReceiveMessageResponse()
        msg = resp.message
        msg.system_properties.message_id = "m1"
        msg.system_properties.receipt_handle = "handle-1"
        msg.system_properties.keys.append("task-9")
        msg.body = b'{"task_id":"t9"}'
        yield resp

    return gen()


@pytest.mark.asyncio
async def test_receive_parses_message():
    """receive 正确解析流式响应，解包出 body/凭据。"""
    c = RocketMQClient("localhost:8081")
    fake = _FakeRecvStub()
    c._stub = fake

    msgs = await c.receive("bid-review", "g1", batch_size=2)

    assert len(msgs) == 1
    m = msgs[0]
    assert m.message_id == "m1"
    assert m.receipt_handle == "handle-1"
    assert m.keys == ["task-9"]
    assert m.body == b'{"task_id":"t9"}'
    await c.close()


@pytest.mark.asyncio
async def test_ack_builds_correct_request():
    """ack 构造的请求：message_id + receipt_handle 都正确。"""
    c = RocketMQClient("localhost:8081")
    fake = _FakeRecvStub()
    c._stub = fake

    msgs = await c.receive("bid-review", "g1")
    await c.ack(msgs[0])

    req = fake.ack_requests[0]
    assert req.group.name == "g1"
    assert req.topic.name == "bid-review"
    assert req.entries[0].message_id == "m1"
    assert req.entries[0].receipt_handle == "handle-1"
    await c.close()


@pytest.mark.asyncio
async def test_to_dead_letter_builds_correct_request():
    """转死信请求：凭据 + 投递次数都正确。"""
    c = RocketMQClient("localhost:8081")
    fake = _FakeRecvStub()
    c._stub = fake

    msgs = await c.receive("bid-review", "g1")
    await c.to_dead_letter(msgs[0], delivery_attempt=16, max_delivery_attempts=16)

    req = fake.dlq_requests[0]
    assert req.receipt_handle == "handle-1"
    assert req.message_id == "m1"
    assert req.delivery_attempt == 16
    assert req.max_delivery_attempts == 16
    await c.close()


# ── workers 消费循环的 handler 编排（不真连 MQ）────────────────

@pytest.mark.asyncio
async def test_process_review_task_still_works():
    """重写 consume 后，process_review_task 纯函数不受影响（回归锚点）。"""
    from agent.contracts.decision import Decision, make_fake_verdict
    from agent.contracts.fake_llm import FakeTools
    from agent.interrupt import CollectingSink
    from agent.state import MemoryCheckpointer
    from workers import process_review_task

    items = [{"item_code": "S0", "item_text": "t", "requirement": {"must": []}}]

    class LLM:
        async def decide(self, state, specs):
            return Decision(action="final", verdict=make_fake_verdict())

    deps = {"llm": LLM(), "tools": FakeTools(), "checkpointer": MemoryCheckpointer(),
            "sink": CollectingSink()}
    out = await process_review_task(
        {"task_id": "tk", "project_id": "p1", "scoring_items": items,
         "user_id": "u", "acl": ["proj:p1"]}, deps=deps)
    assert out["status"] == "DONE"
    assert out["done"] == 1
