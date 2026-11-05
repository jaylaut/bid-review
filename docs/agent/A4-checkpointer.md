# A4 · 自研 checkpointer

上一份：[A3](A3-工具层.md) · 工期：4 天 · 前置：A2 主循环已完成（本阶段不依赖 Java 侧）

## 解决什么

80 项、每项 4 步、跑 6 分钟。第 3 分钟进程被 OOM 杀了 / 部署重启了 / Ollama 挂了——前面烧掉的钱和 40 个已完成的判定全没了。

方案：每步把状态写进 PG，崩溃后从最新存档继续。已完成的步只读状态、绝不重算。

> 单个任务才 ¥1–2，看起来不值得做？生产环境一周崩几次、每次重烧 6 分钟算力，加上用户等了 3 分钟进度归零的体验成本。而且这个机制是 A5（挂起等人）的前提——挂起本身就是"存档后返回"，没有存档就没有挂起。

## 表结构

```sql
CREATE TABLE agent_state (
    task_id      VARCHAR(64) NOT NULL,
    item_id      VARCHAR(64) NOT NULL,
    step         SMALLINT    NOT NULL,
    status       VARCHAR(16) NOT NULL,   -- running | done | pending_human | failed
    state_json   JSONB       NOT NULL,
    state_hash   CHAR(64)    NOT NULL,   -- 内容哈希：没变就不重复写
    schema_ver   SMALLINT    NOT NULL,   -- 契约版本号，从第一天就要有
    created_at   TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (task_id, item_id, step)
);

CREATE INDEX idx_agent_state_resume ON agent_state (task_id, status, step DESC);
-- 【why】恢复查询的模式固定是"按 task_id 找 status 非 done 的最大 step"，
-- 索引按这个查询模式建，别按 created_at 建

CREATE INDEX idx_agent_state_gc ON agent_state (created_at);
-- 清理任务用。GC 策略见文末
```

`schema_ver` 的必要性：A6 几乎必然会往 `AgentState` 加字段（比如 `confidence_breakdown`）。老存档没有新字段，`model_validate_json` 直接炸，**所有历史任务无法恢复**。应对：新字段全部给默认值 + `schema_ver` 记版本。两条都要做，缺一条都有存量数据坑。

## state.py 落地

```python
# 本阶段开始前，基础层（src/common/）已就位，存档代码直接用它的 API：
#   from common import db
#   db.execute(sql, *args) / db.fetch_one(sql, *args) / db.fetch_all(...)
#   conn = await db.acquire() ... await db.release(conn)   # 需长持有时用（如持锁跨多步）
#   async with db.transaction() as conn: ...   # 短事务
# 注意：db.acquire() 的连接绝不在持有时 await LLM 调用（A7 坑2）
# 下文伪代码统一用这套 API。

async def save(state: AgentState, last_hash: str | None = None) -> str | None:
    """写档。返回新内容哈希供调用方缓存；内容没变返回 None（不写库）。

    【why】last_hash 是参数、不是 state 字段：AgentState（A0 契约）里
    没有它，挂在实例上 Pydantic 直接报错。调用方（loop）每次 save 后
    记下返回值、下次传入——重试场景下状态经常没变（比如同一决策重放），
    跳过写省 IO。
    """
    state.state_version += 1
    payload = state.model_dump_json()
    h = sha256(payload).hexdigest()

    if h == last_hash:
        return None

    # 内容没变不写；变了就 UPSERT。同 (task,item,step) 重复执行时覆盖旧档
    await db.execute("""
        INSERT INTO agent_state
            (task_id, item_id, step, status, state_json, state_hash, schema_ver)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        ON CONFLICT (task_id, item_id, step) DO UPDATE
            SET state_json = EXCLUDED.state_json,
                status     = EXCLUDED.status,
                state_hash = EXCLUDED.state_hash
    """, state.task_id, state.item_id, state.step, state.status,
         payload, h, settings.schema_ver)
    return h
```

**写档时机与顺序**，这是本阶段最容易写错的地方：

```
执行一步 → 成功/失败都更新 state → save(state) → 进入下一轮
```

- 顺序是"先执行、后写档"。反过来写，崩溃时会留下"标着完成但实际没做"的档，恢复时直接跳过，结论悄悄错。这种错误的特征是极隐蔽：99% 的时候没事，只在崩溃那一次错。
- 更严格的生产做法：步骤开始前先写一条 `status=running` 的档，结束后更新为 done；恢复时把 `running` 的那一步当"未完成"重做。A2 的 `step` 在"决策后、执行前"加一，就是为这个方案铺的路——恢复时 `start_step = state.step`，从当前步重做。
- 两种做法选哪个：先按简单的（执行后写档）跑通，崩溃测试过了再升级到写前置。**别一开始就上复杂的**，写前置的恢复逻辑多两个分支，先让简单版跑对。

## load_latest() 与 resume()

```python
async def load_latest(task_id: str, item_id: str) -> AgentState | None:
    """读某个评分项的最新一档，没有返回 None。

    【why】单独一个函数：A5 的人工恢复也走它——挂起恢复和崩溃恢复
    必须是同一条读档路径，分两条必然一条新一条旧。
    """
    row = await db.fetch_one("""
        SELECT state_json, step, status FROM agent_state
        WHERE task_id=$1 AND item_id=$2
        ORDER BY step DESC LIMIT 1
    """, task_id, item_id)
    if row is None:
        return None
    return AgentState.model_validate_json(row["state_json"])


async def resume(task_id: str, item_id: str, ctx: AuthContext,
                 llm, tools) -> AgentState:
    state = await load_latest(task_id, item_id)
    if state is None:
        return new_state(task_id, item_id)      # 从没跑过，正常新建

    if state.status == "done":
        return state
        # 【why】已完成的直接返回，一行代码都不执行。
        # 幂等在这里兑现：重复投递的消息、并发触发、人工重试，全走这条路径

    # 关键：从下一步继续进入 run()，不是"重放"
    # 【坑】把 messages 重新喂给模型跑一遍——这不叫恢复，叫重放，钱照烧。
    # 恢复的唯一含义：读档，从 state.step 继续
    async with item_lock(task_id, item_id):
        # 【why】llm/tools 必须传进去：恢复是"重新进入循环"，
        # 不是"重新执行函数"——run() 的依赖一个都不能少
        return await run(state, ctx, llm, tools, start_step=state.step + 1)
```

## 锁：一个评分项同时只允许一个 worker

```python
@asynccontextmanager
async def item_lock(task_id: str, item_id: str):
    # PG advisory lock，不引入新依赖。
    # 【why】锁键是 (task_id, item_id) 组合，不是 task_id：
    # 锁整个任务的话，一个评分项卡住，其余 79 项全跟着等。
    # 粒度到单项，并发度和隔离性都对
    key = int.from_bytes(hashlib.sha256(f"{task_id}:{item_id}".encode()).digest()[:8], "big")
    conn = await db.acquire()
    got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", key)
    # 【why】try_ 而不是阻塞版：拿不到说明有 worker 在跑同一个项，
    # 正确动作是放弃这个项（稍后重投），不是排队等——排队会叠加出雪崩
    try:
        if not got:
            raise LockHeld(task_id, item_id)
        yield
    finally:
        if got:
            await conn.execute("SELECT pg_advisory_unlock($1)", key)
            await db.release(conn)
```

为什么不用 Redis 锁：功能等价，但 PG advisory lock 和状态在同一个库，**天然原子**——不存在"Redis 锁拿到了但 PG 写失败"的中间态。少一个组件，少一类故障。

## GC：存档会膨胀

80 项 × 6 步 = 480 行/任务，每行 5–15KB，约 5MB/任务。1000 个任务就是 5GB。策略：

```sql
-- 每日任务（放在 common/scheduler.py 里，APScheduler 即可）
-- 1. 终态任务：保留最后一行完整状态（纠纷回放用），中间步只留摘要
-- 2. 超过 30 天：连最后一行也删（审计系统里已有结果 JSON，存档可以走）
DELETE FROM agent_state
WHERE created_at < now() - interval '30 days'
  AND status IN ('done', 'failed');
```

## 崩溃测试

这是本阶段的核心验收，别用 mock 模拟崩溃——真杀：

```python
# tests/agent/test_crash_recovery.py
# 思路：起子进程跑 80 项任务 → 等它完成 40 项（轮询 agent_state 表）→ kill -9
# → 主进程调 resume 全部恢复 → 对比两件事

def test_kill9_recovery():
    proc = subprocess.Popen([...run_task_cmd...])
    wait_until_done_items(40)                # 轮询 DB，等 40 项变 done
    proc.kill()                              # SIGKILL，不给清理机会

    recovered = resume_all(task_id)
    assert recovered.status in ("done", "pending_human")

    # 断言一：重烧比例 <5%
    # 统计恢复阶段的 LLM 调用次数 / 全量正常跑的调用次数
    # 理想值 ≈ 1/80 ≈ 1.25%（只有崩溃那一个项重做）
    assert llm_calls_after_recovery < total_calls * 0.05

    # 断言二：结论一致性
    # 恢复跑完的每个 verdict，和不崩溃跑完的逐字段相同
    # 【why】这条最能暴露"恢复悄悄丢了 scratch"的 bug——
    # 步数对了、钱没多花，但草稿纸缺了，结论就是另一个。只有全字段比对能抓住
    assert recovered_verdicts == clean_run_verdicts
```

测试环境把 `agent_max_steps` 调小、模型换 ScriptedFakeLLM + 真 DB，让整个流程秒级跑完——崩溃测试要跑很多遍，不能每次等 6 分钟。

## 本阶段术语

> 【白话】**UPSERT**：INSERT 遇到主键冲突就改成 UPDATE。一句 SQL 完成"有则改、无则插"。
> 【白话】**advisory lock（咨询锁）**：PG 提供的应用层锁，锁的是一个你自己定的数字，和表数据无关。适合"同一个任务只有一个执行者"这类协调。
> 【白话】**SIGKILL（kill -9）**：进程连清理代码都来不及执行就被系统终止。测试用它是为了保证"任何时刻死掉都能恢复"，而不是只在优雅退出时能恢复。
> 【白话】**重放 vs 续跑**：重放是把做过的事再做一遍（花钱），续跑是读档接着干（不花钱）。恢复逻辑写错最容易变成重放。

## 完成标准

- 崩溃测试：kill -9 后恢复，重烧 <5%，结论逐字段一致
- 并发保护：两个 worker 同时 resume 同一项，一个拿到锁一个拿到 `LockHeld`
- 幂等：对 done 的项反复 resume，LLM 调用数为 0
- GC 手动触发一次，30 天前终态数据被清，最后一行保留逻辑正确

下一份：[A5 interrupt 与 HITL](A5-interrupt与HITL.md)
