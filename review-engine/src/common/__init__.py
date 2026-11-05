"""common 层：config / logging / trace / db / context / errors / ids。

依赖方向铁律的最底层（agent/ → rag/online/ → models/ → common/），
本包内代码禁止 import 上层（agent / rag / models）的任何模块。

对外约定：
- get_settings()      全局配置单例（读 review-engine/.env）
- logsetup.setup()    日志初始化，进程入口调用一次
- trace.setup()       OTel 初始化，进程入口调用一次；未配 endpoint 时零开销
- db.get_pool()       asyncpg 连接池（懒建）；db.acquire() 拿连接
- context.AuthContext 行级权限上下文，检索层强制注入的入参来源
"""

from common import logging as logsetup

__all__ = ["AuthContext", "get_settings", "logsetup"]


def __getattr__(name: str):
    """按需导出 get_settings / AuthContext（惰性导入）。

    【why】这两个符号来自 common.config / common.context。如果写在模块顶层，
    只要其中之一缺失（或依赖没装好），整个 common 包 import 就会失败——
    连日志都拿不到，也就失去了"用一行日志说清为什么起不来"的最后手段。
    改成惰性后：只 import logsetup 的进程（日志、可观测、脚本）不受影响；
    真正用到配置的地方仍会在第一次访问时抛出指向缺失模块的明确错误。
    """
    if name == "get_settings":
        from common.config import get_settings as _get_settings
        return _get_settings
    if name == "AuthContext":
        from common.context import AuthContext as _auth_context
        return _auth_context
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
