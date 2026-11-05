# bid-review —— 招投标技术方案评审系统

> 双栈架构：Python 负责判断（RAG + Agent），Java 负责管住判断（企业级工程）。

## 项目结构

```
bid-review/
├── docs/                # 全部文档，01–10 按开发流程编号；入口见 docs/README.md（文档地图）
├── deploy/              # 部署与中间件（compose / postgres 自建镜像 / k8s 占位）
├── review-engine/       # Python 子工程（RAG + Agent）
├── review-platform/     # Java 子工程（Spring Boot 3.x）
├── data/                # 数据（raw 原始招标文件 / parsed 解析产物）
├── eval/                # 评估集（golden_set.jsonl 待建）
└── scripts/             # 采集脚本（fetch_tenders / scan_docs）
```

## 关键约定

1. **定位：学习积累 + 底层理解**。全程手写，**不使用 LangChain / LangGraph / LlamaIndex / litellm / Spring AI 等高层框架**——Agent 循环、checkpointer、interrupt、LLM 客户端、检索链路全部自研，吃透底层机制。
2. **解析/清洗/切片代码由用户自己开发**（`review-engine/src/rag/offline/`），本仓库只提供目录骨架，不写业务代码。
3. 技术栈以 `docs/02-系统总体架构与技术选型.md` 为权威。
4. 模型服务：Ollama（qwen3-embedding 嵌入 + Qwen3-Reranker 重排，logprobs 打分），判定 LLM 走云 API。
5. 消息队列：RocketMQ 5.x（自研 gRPC 客户端，不用官方 Python SDK——它依赖 C++ 在 Windows 装不上）。本机启动步骤见 `docs/08-RocketMQ本机安装与启动手册.md`。

## 依赖方向铁律

```
agent/ → rag/online/ → models/ → common/
```

依赖永远向下，`rag/` 不依赖 `agent/`。
