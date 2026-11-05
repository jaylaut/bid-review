# A5 · interrupt 与 HITL

上一份：[A4](A4-checkpointer.md) · 工期：5 天 · 前置：A4 存档已可用 + Java 侧审批队列就绪（至少接口定稿）

## 解决什么

A6 会算出置信度。低置信的项不能自动采纳，要给人看。但专家可能第二天才处理——进程不能等，也不能丢。

**interrupt ≠ 抛异常**。抛异常是"这事我不干了"；interrupt 是"这事干到一半，冻结进冰箱，你什么时候想继续都行，解冻后和刚才一模一样"。关键差别：时间可以无限长，进程可以换一个。

实现上没有任何黑魔法：**挂起 = 存档（A4 的 save）+ 发事件 + 函数返回**。没有 `await asyncio.sleep()`，没有占住的连接。恢复 = A4 的 resume，只是入口多了一种触发（Java 回调）。

## PendingHuman 载荷

```python
class PendingHuman(BaseModel):
    reason: Literal["low_confidence", "partial_verdict", "ambiguous_requirement",
                    "tool_conflict", "evidence_unverified"]
    # reason 用枚举不用自由文本：【why】审批队列要按原因分类排序——
    # ambiguous（要求本身有歧义）应该先反馈给招标文件解析端，
    # low_confidence 才是给评审专家的。自由文本没法路由
    ai_suggestion: Verdict
    # 【why】永远带建议，不带空着挂起：专家处理一条 AI 建议的平均耗时，
    # 是从零判一条的 1/3。挂起不带建议 = 把工作甩给人工 = 系统白做
    open_questions: list[str] = []    # "金仓适配是否在附件3？"
    evidence_for_review: list[Evidence] = []
```

> **与 02 §三 流 4 的术语对齐**：02 里人工动作写的是"采纳 / 修改 / 驳回"，本文落成
> `accept` / `modify` / `request_more`——"驳回"对应 `request_more`（不采纳 AI 结论、
> 带 hint 打回补充证据）；要直接给出最终结论走 `modify`。Java 侧按这三个枚举实现、
> 按 02 的三个汉语词设计界面，一一对应，**别再造第四种叫法**。

## 挂起

```python
async def maybe_interrupt(state: AgentState, verdict: Verdict) -> AgentState:
    # 高置信 + 明确结论 → 放行。不打扰人
    if verdict.confidence >= 0.9 and verdict.verdict in ("PASS", "FAIL"):
        return state

    state.status = "pending_human"
    state.verdict = verdict                 # AI 建议保留，作为人工的起点

    await checkpointer.save(state)          # 冻结：状态落库，本进程不再持有任何东西
    await emit_event("review.pending_human", state)
    # emit_event 发给 Java（REST 或 MQ 均可，见下），Java 侧进审批队列
    # 【why】不在这里等 Java 确认：发完就走。通知丢了有对账兜底（见文末）

    return state
    # 【核心】返回的 state 带 pending_human 标记，调用方（scheduler）看到它
    # 就把这个评分项放下、去跑下一个。worker 全程不阻塞
```

调用方在 loop 结束后检查：

```python
state = await run(state, ctx, llm, tools)
if state.status == "pending_human":
    return state          # 已落库 + 已通知，直接返回，资源全部释放
```

## 恢复

```python
class HumanDecision(BaseModel):
    action: Literal["accept", "modify", "request_more"]
    final_verdict: Verdict | None = None
    hint: str | None = None              # request_more 时给 Agent 的提示
    # 审计五件套，一个都不能少（docs/01 §Phase4 的 audit_log 要求）
    before: Verdict | None = None        # AI 原结论
    after: Verdict | None = None         # 人工最终结论
    actor: str                           # 谁改的
    reason: str                          # 为什么改。评标被质疑时靠它自证
    ts: datetime


async def resume_with_human(task_id: str, item_id: str,
                            decision: HumanDecision, ctx: AuthContext) -> AgentState:
    state = await checkpointer.load_latest(task_id, item_id)
    # 【坑】这里必须读档，绝不允许"重新跑一遍重建现场"——那就是重放，钱照烧

    if decision.action == "accept":
        state.verdict = decision.final_verdict or state.verdict
        state.terminated_by = "answer"
        state.status = "done"

    elif decision.action == "modify":
        state.verdict = decision.final_verdict
        state.terminated_by = "answer"
        state.status = "done"

    elif decision.action == "request_more":
        # 【关键】人可以说"附件3你还没看，去看一下"。
        # Agent 带着这条提示继续跑，前面几步的工作全部保留——
        # 这才是人机协同，而不是"人推翻 AI 从头来"。
        # 实现：消息追加一条 role=human 的提示，循环从当前步继续
        state.status = "running"
        state.messages.append({"role": "human", "content": decision.hint})
        state = await run(state, ctx, start_step=state.step + 1)

    await checkpointer.save(state)
    await audit_log.write(decision)      # before/after/actor/reason/ts 全部落审计
    return state
```

三种动作的语义必须严格区分，特别是 `request_more`：验收时要证明"前面的步骤没有被重做"——对比恢复前后的 LLM 调用次数，只应新增 request_more 之后那几步的调用。

## 与 Java 的接口

两个 HTTP 端点（Java 侧实现调用方，engine 侧实现被调方）：

```
POST /internal/review/{task_id}/{item_id}/resume     # Java 审批完成后回调 engine
     body: HumanDecision
     幂等：同 item 已 done → 直接返回当前结果，不报错

GET  /internal/review/{task_id}/pending              # 对账用：列出应挂起但 Java 没收到的事件
```

事件通知（engine → Java）建议走 REST 而不是 MQ：挂起是低频事件（一批任务几十条），不需要 MQ 的吞吐，REST 带来的"Java 确认收到"语义简单直接。MQ 版本等有吞吐需求再说。

## 超时兜底

挂起 24 小时没人处理，任务不能永远挂着：

```python
# scheduler 定时扫：pending_human 超过 24h 的项
# 动作：标 degraded=true，继续往下走（进偏离表），报告上打醒目标记
# 【why】宁可出一份带"未经人工确认"标记的报告，
# 也不要让任务永远卡在 99%。挂起机制不能变成任务黑洞
```

"AI 建议 + 强制人工终判"这个降级产物在偏离表里单独一栏，评标时人工补判即可——流程不断，责任清晰。

## 测试

```python
# 1. 长时间挂起：挂起 → 模拟等 2 小时（测试里把超时配置缩到 2 秒）→ 恢复
#    断言：结论与不挂起时逐字段一致
# 2. 资源占用：同时挂起 80 项，观察 worker 内存、DB 连接数
#    【why】挂起不该占用任何持续资源。连接数不降 = 哪里还留着句柄，找出来
# 3. 三种动作全覆盖：accept / modify / request_more
#    request_more 额外断言：恢复后 LLM 调用次数 = 原次数 + 新增步数的调用
# 4. 审计完整性：每条人工记录能查到 before/after/actor/reason/ts 五项
# 5. 幂等回调：同 item 已 done，再 POST resume → 返回现有结果，不报错、不重算
```

## 本阶段术语

> 【白话】**挂起（suspend）**：把状态存盘然后函数返回。和"暂停线程"的本质区别：不占任何资源，等多久都行，可以在另一个进程里继续。
> 【白话】**HITL**：Human-In-The-Loop，人在环里。关键判断有人把关，在受监管行业不是选项是要求。
> 【白话】**幂等回调**：Java 重复调 resume（网络重试很常见），第一次和第 N 次结果相同、只算一次。
> 【白话】**对账**：两边各记一笔账，定时比对找差异。事件通知丢了（网络抖动、Java 重启），对账能补回来。

## 完成标准

- 挂起 80 项，worker 内存和连接数恢复到挂起前水平
- 2 小时（模拟）后恢复，结论与不挂起一致
- `request_more` 不重做已有步骤（LLM 调用数验证）
- 审计五件套完整；幂等回调测试通过
- 超时兜底演示一次：挂起项 24h（配置缩到秒级测试）后自动降级出报告

下一份：[A6 护栏与置信度](A6-护栏与置信度.md)
