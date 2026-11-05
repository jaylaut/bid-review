"""common 层统一异常体系（依赖方向最底层，不 import 任何上层模块）。

【为什么必须有统一异常树】
上层（agent / api / rag）都要写"兜底捕获"。若各模块自建异常类，兜底只能写
`except Exception` —— 那把真正的编程错误（拼错变量、TypeError）也一起吞了，
线上表现就是"任务失败，但日志里没有 traceback"。
挂到 AppError 这棵树上后，兜底写 `except AppError` 即可：抓得住业务错误，
漏得掉真 bug（该崩就崩，才能被看见）。

【code 给机器，message 给人】
告警按 code 聚合（tool_error 突然变多 = 工具层坏了）；
message 里可以随便写中文长句。
两者混用会导致"改一句文案，告警规则全失效"这种低级事故。

【retriable 默认 False】
宁可少重试（任务进人工），不可多重试（429 风暴把上游打挂）。
需要重试的场景（429 / 超时）显式声明 retriable=True，由 tenacity 那一层读取判断。

【与 rag/offline/pipeline.py 的关系】
pipeline 用 `type(exc).__name__` 记录失败原因，所以这里的类名会直接进账本与日志；
类名一旦改动，历史日志的检索口径就断了——改名前先想清楚。
"""
# 延迟注解：让 `dict[str, Any] | None` 这类写法在运行时不求值，也避免循环 import。
from __future__ import annotations

# Any：标注 details 的值可以是任意 JSON 安全类型（str/int/list/dict）。
from typing import Any

# 显式导出，避免 `from common.errors import *` 时把内部辅助量也带出去。
__all__ = [
    "AppError",
    "ToolError",
    "PermissionDenied",
    "LockHeld",
    "UpstreamError",
    "StorageError",
    "ContractViolation",
    "ConfigError",
]


class AppError(Exception):
    """全部业务异常的基类。

    【字段】
        code      ：机器可读的错误码（snake_case），子类用类属性覆写。
        message   ：人可读的中文描述。
        details   ：结构化补充信息（哪个项目、哪个 section、哪个文件），进日志/trace。
        retriable ：是否值得重试。

    【为什么 code 用类属性而不是构造参数】
    "同一类错误必须有同一个 code"是硬约束。放构造参数里，总有人手滑传成别家的 code，
    告警聚合立刻被污染。写成类属性，实例层面改不了（想改只能改类定义，评审能看见）。

    【为什么 to_dict 只回三个键】
    HTTP 响应体 / SSE 事件 / MQ 死信消息都要把异常序列化出去，
    键集必须稳定：多一个键，Jackson 侧严格模式就报 unknown property。
    traceback 不进 to_dict —— 那是日志的事，不是协议的事。
    """

    # 基类的错误码；子类必须覆写成各自不同的值（测试会检查 code 不重复）。
    code: str = "app_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retriable: bool = False,
    ) -> None:
        """构造异常。

        【参数】
            message   ：中文描述，写清"什么对象、什么状态、期望什么"。
            details   ：关键字传，结构化上下文；不传则规范化为空字典（不是 None）。
            retriable ：关键字传，默认 False（见模块说明里的理由）。

        【为什么 details 要 `or {}` 规范化】
        下游 `exc.details.get("project_id")` 是常见写法；
        若这里是 None，每个调用点都得先判空，漏一个就是 AttributeError。
        """
        # 交给 Exception 保存原始消息：traceback 最末行会原样打印它。
        super().__init__(message)
        # 单独存一份：to_dict / 日志取用方便，不必再拼 args[0]。
        self.message = message
        # None → {}：让 details 永远可安全下标取值。
        self.details = details or {}
        # 是否可重试（供上层重试策略读取）。
        self.retriable = retriable

    def to_dict(self) -> dict[str, Any]:
        """序列化成协议层可用的三键字典。

        【返回值】{"code", "message", "details"}，键集固定，顺序稳定（便于快照测试）。
        """
        # 逐键显式构造，而不是 {"**self.__dict__"} —— 后者会把 retriable 也带出去，破坏协议。
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }

    def __str__(self) -> str:
        """人读形式：有 details 时附在末尾，方便一行日志看清上下文。

        【为什么在有 details 时才拼】
        `pytest.raises(ConfigError, match="MinerU")` 是对 str(exc) 做正则匹配；
        无条件加前缀/后缀会改变匹配结果。保持"无 details 时 str == message"最稳。
        """
        # 没有附加信息：直接返回原文，不引入任何前后缀。
        if not self.details:
            return self.message
        # 有附加信息：按 k=v 平铺，日志里一眼能看出是哪个项目/文件。
        rendered = ", ".join(f"{key}={value}" for key, value in self.details.items())
        # 拼接成 "消息 [k=v, k=v]"。
        return f"{self.message} [{rendered}]"


class ToolError(AppError):
    """工具层错误：工具入参不合法、目标对象不存在、工具内部执行失败。

    【典型场景】agent/tools/registry.py 的 get_section 收到不存在的 section_id；
    这类错误要回给模型让它换参数重试，而不是让整个任务失败。
    """

    # 独立错误码：与"上游挂了""没权限"在告警里必须能区分开。
    code = "tool_error"


class PermissionDenied(AppError):
    """权限拒绝：跨项目/跨标段越权，或 ACL 不覆盖目标数据。

    【典型场景】rag/online/retrieve 的 _acl_guard：查询项目 ≠ 身份项目。
    绝对不可重试 —— 重试一百次还是没权限，只会刷日志。
    """

    # 独立错误码：权限类告警要单独看（突然变多 = 有人扫库或前端传错 project_id）。
    code = "permission_denied"


class LockHeld(AppError):
    """锁被占用：同一任务/同一文档已有别的执行者在处理。

    【典型场景】checkpointer 或 ingest worker 抢文档级锁失败。
    这类错误通常 retriable=True（等对方释放），但默认值仍按基类规则保持 False，
    由真正需要重试的调用点显式声明。
    """

    # 独立错误码：锁竞争是容量/并发问题，与业务错误分开统计。
    code = "lock_held"


class UpstreamError(AppError):
    """上游依赖错误：LLM / 嵌入 / 重排 / MQ / MinIO 返回异常或超时。

    【典型场景】Ollama 504、云端 LLM 429、rerank 超时。
    429/超时必须带 retriable=True 抛出，否则重排降级链不会启动（见 03 §5.4）。
    """

    # 独立错误码：上游错误率是核心 SLO 指标，必须能单独聚合。
    code = "upstream_error"


class StorageError(AppError):
    """存储层错误：PG 写入失败、MinIO 读写失败、向量维度不匹配等。

    【典型场景】chunks 插入时 embedding 维度与 DDL 的 1024 不一致（R0 自检第 5 项拦的就是它）。
    """

    # 独立错误码：存储错误往往意味着数据不一致，要能单独拉出来对账。
    code = "storage_error"


class ContractViolation(AppError):
    """契约违约：跨模块/跨语言的数据结构不符合既定 schema。

    【典型场景】LLM 返回的 Verdict JSON 缺字段、类型不对（A0 契约冻结后由 Pydantic 兜住）；
    Java ↔ Python 双栈字段不一致（J0 的验收项）。
    这类错误**不可重试**：同样的输入重试一百次还是违约，重试只会烧 token。
    """

    # 独立错误码：契约违约是"代码/协议"问题，跟运行时抖动要分开看。
    code = "contract_violation"


class ConfigError(AppError):
    """配置/依赖缺失：必填配置项没配、重依赖没装、路径不存在。

    【典型场景】parsers.parse_with_mineru 未安装 magic-pdf 时抛它，
    并在 message 里给出可执行的安装命令（而不是裸 ImportError）。
    这类错误发生在启动/首次调用，属于"环境没准备好"，不该被重试逻辑吞掉。
    """

    # 独立错误码：配置类错误一出现就是部署问题，告警要直接指向运维。
    code = "config_error"
