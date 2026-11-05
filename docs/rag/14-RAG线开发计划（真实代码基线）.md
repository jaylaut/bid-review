# 14 · RAG 线开发计划（真实代码基线）

> **本文定位**：RAG 线（[rag/ R0–R7](rag/)）的施工计划。与 [11](11-总体开发路线（RAG+Agent+Java）.md)（三线阶段级总计划）分工不同——本文只管 RAG 线，任务拆到"写哪个文件、过哪个测试"的粒度。
> **基准**：2026-11-05 对 `review-engine/src/` 全量盘点。**本文的状态判断以磁盘代码为准**。R0–R7 各章头部的"当前状态（2026-10-27）"行所描述的落地代码（Agent A0–A8、`common/db.py`、`chunking`、`indexer`、`models/`、`fuse/assemble/cache` 等）在磁盘上并不存在，已过期，逐章对照见 §一。
> **验收锚点**：本仓库当前是"测试先行"状态——17 个测试文件就是全部缺失模块的接口契约。本文每个任务的完成判据都是"对应测试转绿"，不另设软指标。

---

## 一、真实代码基线（2026-11-05 盘点）

### 1.1 src/ 现状

| 位置 | 内容 | 状态 |
| --- | --- | --- |
| `src/common/` | logging / errors / ids / fsutil / version / `__init__`（惰性导出） | ✅ 在 |
| `src/common/` | config.py、context.py、db.py | ✅ 在（2026-11-05 S1 落地，tests/common 36 项全绿） |
| `src/rag/offline/parsers/` | route / mineru / contracts / `__init__` | ✅ 在，[13](13-解析链路代码审查清单.md) 的 20 条问题已修完 |
| `src/rag/offline/` | quality_gate / manifest / source / unpack_zips | ✅ 在 |
| `src/rag/offline/` | pipeline.py | ⛔ 只有 48 行导入块，`parse_one` / `run_batch` 等函数体全缺 |
| `src/rag/offline/` | cleaning、chunking、indexer | ⛔ 连目录都不存在 |
| `src/rag/online/` | retrieve/`__init__`.py（双路 SQL + 权限三层防御） | 🟡 代码在、质量好，但它 import 的 `rag.online.fuse` / `rag.online.types` / `common.context` 缺失，**当前 import 不通** |
| `src/rag/online/` | types.py、fuse.py、assemble.py、cache.py | ⛔ 缺 |
| `src/models/` | types / llm / embedding / rerank | ⛔ 整层缺 |
| `src/agent/`、`src/api/`、`src/workers/` | 全部 | ⛔ 缺（Agent 线与平台线，不在本文范围） |
| `src/config/rag.yaml` | 全部配置段 | ✅ 在（parsers 修复时补过 probe / mineru / source 三段） |
| `eval/`、`review-platform/` | — | ⛔ 不存在 |

配套事实：

- `pyproject.toml` 已登记 `pypdf>=6.21.4`、`mineru[core]>=4.1.7`，uv.lock 已锁定（含 mineru 全家桶）。**依赖声明已解决，剩"确认 .venv 真装上了"这一步**（S0）。
- `data/parsed/` 仍只有 `tender_20260828.md` 一份产物，无 `manifest.json`；`data/raw/tenders/` 52 个项目待解析。
- `tests/conftest.py` 的 autouse 夹具 teardown 里 `from common import db`——db.py 不补，**全仓测试收集后全部报错**。这是 S1 排第一的直接原因。
- `src/rag/` 与 `src/rag/offline/` 连 `__init__.py` 都没有（靠 Python 3 命名空间包在跑），S1 顺手补上。

### 1.2 R0–R7 各章：文档说法 vs 磁盘真实

| 章 | R 文档头部说法（2026-10-27） | 磁盘真实（2026-11-05） |
| --- | --- | --- |
| R0 | DDL 与七项自检已落地，175 项测试通过 | `deploy/postgres/init/` 两份 SQL、`scripts/check_env.py` 在；`common/db.py` 已由 S1 补齐（2026-11-05），七项自检可跑 |
| R1 | 清洗已落地单测全覆盖；1/52 已解析 | cleaning 缺（test_offline 顶部 8 个清洗测试 import 即炸）；quality_gate 在（10-28 修过阈值配置化 + 乱码口径）；pipeline 截断 |
| R2 | chunking 已落地（185 行，单测覆盖） | 缺。test_offline 分块段给了完整函数签名与行为定义 |
| R3 | indexer 与 models/embedding.py 已落地 | 都缺。test_gapfill 给了 `index_document` 的注入式契约 |
| R4 | retrieve 已落地，真实标书实测通过 | retrieve 代码在，但 import 链断（fuse / types / common.context 缺），**当前不可用** |
| R5 | fuse / rerank / assemble 均已落地 | 都缺 |
| R6 | cache.py 已落地 | 缺（test_gapfill 的 FakeCache / sha256_key 在等） |
| R7 | eval_runner 已落地，golden set 不存在 | agent/eval_runner.py 缺（agent 整层缺）；eval/ 目录不存在 |

**结论**：src 实际进度 ≈ "R1 前半段（解析 + 质量门 + 账本工具）完成；R0 的 common 层缺一半；R2–R7 只有测试契约"。这与 [11 §七](11-总体开发路线（RAG+Agent+Java）.md)仪表盘"A0–A8 已落地、在线检索实测通过"的描述对不上——RAG 线排期以本表为准。

### 1.3 测试即契约（缺失模块 ↔ 在等的测试）

| 测试文件 | 在等的缺失模块 | 对应 R 章 |
| --- | --- | --- |
| tests/common/test_config.py | common/config.py（Settings / get_settings / require_llm_key） | R0 |
| tests/common/test_context.py | common/context.py（AuthContext / set_run_context / clear_run_context） | R0 |
| tests/common/test_db.py + tests/conftest.py | common/db.py（get_pool / acquire / close_pool / ping / 事务 / pgvector codec 注册） | R0 |
| tests/rag/test_offline.py 清洗段（8 例） | rag/offline/cleaning（8 个纯函数） | R1 |
| tests/rag/test_offline.py 分块段（9 例） | rag/offline/chunking（4 个函数） | R2 |
| tests/test_gapfill.py | rag/offline/indexer、rag/offline/scoring_extract、rag/online/cache、agent/eval_runner | R3/R6/R7 |
| tests/models/（2 个文件） | models/llm、models/embedding、models/rerank、models/types | R3/R5 |
| tests/rag/test_online.py | rag/online/types、fuse、assemble + models/types | R4/R5 |
| tests/agent/（5 个文件） | agent/ 全层 | Agent 线（见 docs/10 与 agent/ 文档） |
| tests/test_api_workers.py | api/、workers/ | 平台线（11 §P5） |

---

## 二、施工顺序：六个批次

严格按依赖方向排：common → 离线（cleaning → chunking → indexer）→ 模型层 → 在线（types → fuse → assemble → cache）→ 评测。**每批出口：对应测试全绿 + 全仓 ruff 零告警**，不过不进下一批。

| 批次 | 内容 | 对应 R 章 | 预估 | 出口判据 |
| --- | --- | --- | --- | --- |
| S0 | 依赖装实 + 环境确认 | R0/R1 | 0.5 天 | `verify_parsers.py` L1–L3 全过 |
| S1 | common 三模块 | R0 | 1–1.5 天 | pytest tests/common 全绿，全仓测试可收集 |
| S2 | cleaning + pipeline 恢复 + 账本接线 | R1 | 1.5–2 天 | test_offline 清洗段全绿；单份文档全管道跑通 |
| S3 | chunking | R2 | 1.5 天 | test_offline 分块段全绿 |
| S4 | models/embedding + indexer + scoring_extract | R3 | 2–2.5 天 | test_gapfill 全绿（除 eval_runner 段）+ tests/models embedding 段 |
| S5 | 在线链路四件套 + models 其余 | R4/R5/R6 | 2.5–3 天 | test_online + test_models 全绿 |
| S6 | golden set + 评测标定 | R7 | 2–3 天（另加人工标注） | R7 完成标准逐条过 |

合计约 11–14 个工作日。golden set 标注与 51 个项目的批量解析是并行任务（§四），不占上表工期。

### S0 · 依赖与环境（0.5 天）

| # | 任务 | 说明 |
| --- | --- | --- |
| 0.1 | `uv sync` | pyproject 已含 pypdf / mineru，lock 已锁；同步后 `.venv` 里必须真实可用 |
| 0.2 | `uv run python -c "import pypdf"` + `uv run mineru --help` | [13 §四](13-解析链路代码审查清单.md) 手动步骤的收尾；mineru 2.x 起 CLI 叫 `mineru`，1.x 才叫 `magic-pdf` |
| 0.3 | `uv run python ../scripts/verify_parsers.py` | L1 纯函数 + L2 配置 + L3 真实文件探针，全过才算环境就绪 |
| 0.4 | `--with-mineru` 跑一次 L4 | 拿一份真实 PDF 端到端解析。首次要下模型，十几分钟，属一次性成本 |

### S1 · common 层三模块（1–1.5 天）

| # | 任务 | 契约来源 | 要点 |
| --- | --- | --- | --- |
| 1.1 | `common/config.py` | test_config.py | Settings / get_settings / require_llm_key；读 `review-engine/.env`；pyproject 的 per-file-ignores 已给它留好 S105/S106 豁免（本地开发凭据默认值） |
| 1.2 | `common/context.py` | test_context.py | AuthContext + contextvars 版 set_run_context / clear_run_context；test_telemetry 也在用 set_run_context |
| 1.3 | `common/db.py` | test_db.py + conftest.py | get_pool（懒建）/ acquire / close_pool / ping / fetch / execute / transaction；**每个池连接建立时 `register_vector(conn)`**（S4 原生向量传参靠它，R3 §向量存储格式） |
| 1.4 | 补 `src/rag/__init__.py`、`src/rag/offline/__init__.py` | — | 现在靠命名空间包；补上后 import 报错信息更可读，也为 S2 的包内相对导入铺路 |
| 1.5 | `common/logging.py` 重构 | test_logging.py | 原实现缺 `build_processors()` 且用 QueueListener 异步落盘，测试捕不到输出。拆出处理器链、时间戳键改 `timestamp`、去队列改同步写。**S1 出口的必要前置**（test_logging 5 例） |

**出口**：`uv run pytest tests/common -q` 全绿；全仓 `uv run pytest --collect-only -q` 不再报 ModuleNotFoundError（agent/models 等测试此时仍是收集失败——它们等 S4/S5，见 §2.1 的例外说明）。

> 完成于 2026-11-05（未提交）：`tests/common` 36 项全绿（config 7 / context 6 / db 3 / ids+errors 15 / logging 5）；`ruff check src/common/` 零告警；全仓收集失败只剩 `agent.*`、`models.*`、`rag/test_online`、`rag/test_offline`（cleaning/chunking 未写）、`test_gapfill`、`test_api_workers`，不再有 `common.*` 缺失。
> 落地时发现 asyncpg 0.32 的三处差异（`is_closing()` 而非 `is_closed()`、`create_pool` 需 `await`、`register_vector` 是协程），已写进 db.py 注释，S4/S5 复用同一套写法。

**遗留（不在 S1 判据内）**：`register_vector` 之后向量列的原生传参要在 S4 indexer 落库时实测一次（`list[float]` 直接传，不再 json.dumps）；这是 S4 出口的一个检查点，不是 S1 的。

> 例外处理：tests/agent、tests/models、tests/rag/test_online、tests/test_gapfill 在对应批次完成前**必然收集失败**。S1 出口判据按"tests/common + tests/rag/test_offline 中 parsers/quality_gate 段可收集"执行，其余测试的转绿时间就是各自批次的出口。

### S2 · cleaning + pipeline 恢复（1.5–2 天）

| # | 任务 | 契约来源 | 要点 |
| --- | --- | --- | --- |
| 2.1 | `rag/offline/cleaning.py` | test_offline.py 清洗段 8 例 + [R1 §清洗](rag/R1-解析与清洗.md) | 8 个函数：strip_zero_width / strip_watermark / detect_headers_footers（threshold 参数，**少于 3 页返回空集**——单页文档任何行都是 100% 重复，防误删正文）/ remove_headers_footers / dedupe_exact / near_duplicate_hash（字符 shingle 集合）/ jaccard / clean_document（编排前几个 + 压缩 3 连以上换行）。**不做 PII 脱敏**（标书本就给评审看，人名公司名是证据链一部分） |
| 2.2 | 恢复 `pipeline.py` | 现有 48 行导入块 + [R1](rag/R1-解析与清洗.md) | parse_one / run_batch / summarize 三个函数；常量用 `PARSED`/`SKIPPED`/`FAILED`（contracts 已改公有名）；`parse_with_mineru` 用新签名（work_dir / backend / timeout_s）；`_clean` 接收 cfg；两条顺序约束（先落盘再登记、先 normalize 再 clean）写进代码注释 |
| 2.3 | 账本接线 + 命名坑落地 | [R1 §命名坑](rag/R1-解析与清洗.md) | 产物改**完整项目编号**命名（`tender_20260828.md` 这种按日期命名在同日期多项目时会互相覆盖）；`manifest.py` 接进 pipeline，每份产物写一行（raw_sha256 / parsed_path / parse_backend / quality_score / ts） |
| 2.4 | `source.py` 清理 25 处 ruff | — | 主要是 E501 超长注释行、import 排序、UP009 |

**出口**：`uv run pytest tests/rag/test_offline.py -q` 全绿；`verify_parsers.py` L1–L3 全过；拿 `data/raw/tenders/` 任一项目目录跑 `parse_one`，落盘产物 + 账本记录 + 质量分三样齐全；`check_parse_sync.py` 对真实数据零缺失零冲突。

### S3 · chunking（1.5 天）

| # | 任务 | 契约来源 | 要点 |
| --- | --- | --- | --- |
| 3.1 | `rag/offline/chunking.py` | test_offline.py 分块段 9 例 + [R2](rag/R2-结构化分块与父子块.md) | approx_tokens（CJK ≈1 token/字、拉丁 4 字符/token，不引 tiktoken）/ markdown_to_sections（层级计数器，更深层级重置，path 如 "3>3.2>3.2.4"）/ _split_long_text（句子边界切 + overlap）/ chunk_markdown（父块截断 parent_max_tokens=2000；表格 `kind="table"` 整块；caption 正则独立成块）/ dedupe_chunks（Jaccard 0.85，不上 LSH——量级没到） |
| 3.2 | uuid5 确定性 ID | [R2 §uuid5](rag/R2-结构化分块与父子块.md) | chunk_id = uuid5(NAMESPACE_URL, f"{doc_ref}:{序号}")；doc_ref 用 `(project_id, file_sha256)` 幂等派生的 doc_id——上游不稳，下游确定性全是假的。**补一条测试**：同文档两次分块产出完全相同的 chunk_id 集合（R2 完成标准第 2 条，现有测试没钉这条） |
| 3.3 | 参数核对 rag.yaml | R0 配置段 | max_tokens=512 / overlap=64 / parent_max=2000 从 rag.yaml 读，代码里不许有字面量（R0 铁律，生成前用 `grep -n "512\|2000" src/` 自查） |

**出口**：test_offline 分块段全绿（含新补的确定性测试）；对 S2 的真实产物跑 `chunk_markdown`，输出块数与 text/table/caption 分布统计。

### S4 · 嵌入 + 入库 + 评分项抽取（2–2.5 天）

| # | 任务 | 契约来源 | 要点 |
| --- | --- | --- | --- |
| 4.1 | `models/types.py`（Hit / Usage）+ `models/embedding.py` | tests/models/ + [R3 §四条铁律](rag/R3-嵌入与幂等入库.md) | ①查询侧带指令、文档侧不带（Qwen3 指令感知）②num_ctx 显式传 ③返回向量 L2≈1 校验，不合规抛 ContractViolation ④维度 == 配置 dim，第一次调用就炸。批量 batch_size=32 走 `/api/embed` |
| 4.2 | `rag/offline/indexer.py` | test_gapfill indexer 段 2 例 + [R3](rag/R3-嵌入与幂等入库.md) | 签名照测试：`index_document(md_text, *, project_id, doc_type, title, file_hash, acl, embedding_ver, chunking_ver, fetch, execute, embed_fn, transaction)` → IndexResult(skipped / chunks_written / doc_id)。文档级幂等**必须看 `status='indexed'`**（中途失败留下的记录不算完成）；事务内软删 + `ON CONFLICT (chunk_id) DO UPDATE` |
| 4.3 | 向量传参拍板 | test_gapfill vs R3 完成标准（**两处矛盾**） | 测试 `json.loads(args[11])` 期望向量是 JSON 字符串；R3 完成标准要求废掉 `json.dumps(vec)`（db.py 已注册 pgvector codec）。**拍板：以 codec 原生 list 为准，同步改测试**——codec 注册是 R0/R3 一致的既定决策，JSON 字符串是注册前的旧方案；改测试 3 行，走回头路是改架构。执行时在测试注释里记一句改因 |
| 4.4 | `rag/offline/scoring_extract.py` | test_gapfill 评分项段 + [R0 三表](rag/R0-契约与启动自检.md) | ScoringItem / extract_from_document / persist_items：全文分章 → LLM 结构化抽取 → Pydantic 校验 → 写 scoring_items 表；requirement.must 拆成可验证的原子要求（R4 查询构造与 Agent 判定都吃这个字段） |
| 4.5 | 库级对账 + 脚本直调冒烟 | [R3 §每日对账](rag/R3-嵌入与幂等入库.md) | `count(documents where status='indexed')` == `count(distinct chunks.doc_id)`，不一致率 >0.1% 告警；不接 MQ，先脚本直调跑通 3 份文档全管道——**先证明管道对，再接消息** |

**出口**：test_gapfill 全绿、tests/models embedding 段全绿；同一文档连跑两次第二次 `skipped=True` 且 chunks 行数不变；人为在嵌入阶段中断后重跑无僵尸 `indexing` 文档。

> ingest worker 的 MQ 编排**有意后置**：等 RocketMQ 真连（§四）。状态机 / 重试 3 次进 DLQ / prefetch=2 背压 / progress 事件四要点见 R3 §MQ 编排，届时照抄。

### S5 · 在线链路补全（2.5–3 天）

| # | 任务 | 契约来源 | 要点 |
| --- | --- | --- | --- |
| 5.1 | `rag/online/types.py` | test_online.py + [R0 §在线契约](rag/R0-契约与启动自检.md) | RetrieveQuery（embedding 由调用方算好传入，retrieve 内部不调嵌入——R6 的 L1 缓存要复用同一向量）；Candidate(Hit) 带 dense_rank / sparse_rank / rerank_score |
| 5.2 | `rag/online/fuse.py` | test_online.py + [R5 §RRF](rag/R5-融合重排与组装.md) | rrf_fuse 只用名次：score = Σ 1/(k+rank)，k=60；双命中天然高于单命中；POOL 常量。单测钉两条：双命中 > 任一单命中；两路注入不同量纲分数结果只取决于名次 |
| 5.3 | `models/rerank.py` | tests/models/test_embedding_rerank.py + [R5 §logprobs](rag/R5-融合重排与组装.md) | yes_no_probability 从 top_logprobs 还原 P(yes)，**缺 yes/no 抛 ValueError 不猜默认分**；OllamaReranker + build_prompt；三级降级链（本地→云 API→RRF 序），降级必须带 degraded 标记 |
| 5.4 | `rag/online/assemble.py` | test_online.py + [R5 §组装](rag/R5-融合重排与组装.md) | 父块优先（parent_content 冗余列省 DB 往返）；同父块兄弟子块去重；U 型排序（超预算 continue 不 break）；**label 在排序后重打**（A6 证据回查按 label 反查，错位全乱）。approx_tokens 复用 chunking 的实现，不写第二份 |
| 5.5 | `rag/online/cache.py` | test_gapfill 缓存段 + [R6](rag/R6-三级缓存与性能预算.md) | sha256_key（json.dumps sort_keys 规范化，杜绝 "a\|bc"=="ab\|c" 撞键）；RetrievalCache 读失败当 miss、写失败 debug 日志；FakeCache 内存版。L2 键含 doc_version、L3 键含 prompt_version——两条正确性防线必须有测试 |
| 5.6 | retrieve 接通 + 真库实测 | [R4](rag/R4-查询构造与双路召回.md) | retrieve/__init__.py 已写好，S5 前四项落地后 import 链自然接通；真库起来后跑三个真实评分项查询，留档"稠密路命中、稀疏路 0 命中"案例；mock fetch 各 sleep 0.1s 验证真并发（总耗时 ≈0.1s 不是 0.2s） |

**出口**：test_online + test_models 全绿；越权单测（project_id 不匹配 → PermissionDenied 而非空列表）在 CI 常驻；真库三个查询实测通过。

### S6 · golden set + 评测标定（2–3 天，另加人工标注）

| # | 任务 | 契约来源 | 要点 |
| --- | --- | --- | --- |
| 6.1 | `agent/eval_runner.py` 评测半边 | test_gapfill eval_runner 段 | load_golden / build_report / confusion / _refusal_metrics / compare / save_report。**与 Agent 线 A8 是同一份文件，不另建 run_eval.py**（两套脚本必然漂移） |
| 6.2 | golden set 标注 | [R7 §golden set](rag/R7-评测与增益实验.md) | 人工任务，**立即启动先标 40 项**（§四）；四类 verdict 样本都要够（NOT_FOUND 优先）；页码锚点；最终 ≥80 项 |
| 6.3 | E1/E2/E3 单变量实验 | R7 | 定长→结构化分块 / 单路→双路 RRF / 无重排→重排，一次只变一个，无增益就回滚，逐项增益表落盘 |
| 6.4 | 阈值与 🟡 参数标定 | R7 §配置参数治理 | 拒答阈值扫 {0.10, 0.15, 0.20, 0.25}，取"精确率 ≥0.9 且漏检率 ≤5%"工作点写回 rag.yaml（git log 留标定提交）；R7 列出的 10 个 🟡 参数逐项过 |
| 6.5 | 越权测试进 CI | R7 | 人为把 `WHERE project_id` 挪出 SQL，门禁必须报错 |

---

## 三、执行纪律（每批开工前过一遍）

1. **先读测试再写实现**。本文列的每个模块，动手前把对应测试文件完整读一遍——测试就是接口文档，签名、边界、报错行为全在里面。
2. **[代码生成自查清单](代码生成自查清单.md) 7 条照办**，尤其：阈值不进代码、不重写已存在模块、脚本走 common.logging 不用 print。
3. **顺序不改**：先 normalize 再 clean、先落盘再登记、软删不先删后写——每条都是踩过坑换来的，pipeline 注释里有原因。
4. **不上清单**：LLM 查询改写（确定性模板够用）、MinHash/LSH（百页量级 Jaccard 够用）、PII 脱敏（毁证据链）、tiktoken（approx_tokens 够用）。每一样都有明确的"量级到了再上"的触发条件，写在对应 R 章里。
5. **每批收尾跑全量**：`uv run pytest -q` + `uv run ruff check src/ tests/`。已有批次的测试不允许被后续批次打红。

---

## 四、并行任务（不占 RAG 代码工期，但影响 RAG 出口）

| 任务 | 什么时候做 | 卡谁 |
| --- | --- | --- |
| golden set 标注（先 40 项） | **立即启动**，人工活越晚开始越晚完工 | S6 全部数字；没有它 S0–S5 做完也出不了基线 |
| MinerU 批量解析 51 个项目 | S2 出口后即可开跑（断点续跑 + 每日对账） | R7 评测覆盖率、后续 Agent 全量回归 |
| RocketMQ 三进程真连 + e2e | 任何时候（半小时的事，别拖） | ingest worker 的 MQ 模式（S4 有意后置的部分） |
| MinIO 容器启动 | 任何时候 | documents.file_uri 真实读写；S4 可先用本地路径占位 |
| R0–R7 头部"当前状态"行修正 | S1 完成后一次性处理：逐章替换为"实时状态以 [14](../14-RAG线开发计划（真实代码基线）.md) 为准" | 所有后续读文档的人（11 §七仪表盘同理） |

---

## 五、验证命令（每批出口跑）

```bash
cd review-engine
uv run pytest -q                              # 全量；S1 后逐批转绿，已绿的不许打红
uv run ruff check src/ tests/                 # 零告警
uv run python ../scripts/verify_parsers.py    # S2 起；--with-mineru 做 L4 端到端
uv run python ../scripts/check_parse_sync.py  # S2 起，对真实数据
uv run python ../scripts/check_env.py         # S4/S5 真库相关批次，七项全绿
```

---

## 六、本文的维护约定

- 每完成一个批次：更新 §二 总表状态列，批次小节末尾加一行"完成于 YYYY-MM-DD，commit xxx"。
- R0–R7 各章头部的"当前状态"行不再单独维护，统一指向本文（见 §四 最后一行）。
- 与 11 的分工：11 管三线阶段级进度，本文管 RAG 线任务级进度；**11 §P2/P3 的 RAG 行以本文为准**。

---

*基准日 2026-11-05。状态过期比没有状态更糟——改完代码不顺手更新状态列的 PR 不许合。*
