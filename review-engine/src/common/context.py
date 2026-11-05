"""请求上下文模块，管两件事：

1. AuthContext：一次评审请求的"身份"，装着用户是谁、能看哪个项目、有哪些权限标签。
   检索数据库时靠它过滤，防止 A 项目的人查到 B 项目的数据。
   类比 Java：Spring Security 里的 Principal / Authentication，一个不可变的身份对象。

2. set_run_context / clear_run_context：把"当前正在处理哪个任务、哪个评分项、第几步"
   挂到当前协程上。日志模块每打一条日志会自动带上这些信息，
   排查问题时按 item_id 一搜，就能看到这个评分项的全部日志。

为什么用 ContextVar 而不是普通全局变量？
80 个评分项是同时并发处理的。普通全局变量只有一份，任务 A 写进去的值
会被任务 B 覆盖，日志全串。ContextVar 可以理解为"协程版的 ThreadLocal"：
Java 里 ThreadLocal 是每个线程一份，ContextVar 是每个协程一份，
asyncio 创建新任务时会自动复制一份，天然互不干扰。
"""
# 这行的作用见 config.py 顶部的说明，照抄即可。
from __future__ import annotations

# json 模块，把列表转成 JSON 字符串用。类比 Java 的 Jackson ObjectMapper。
import json

# ContextVar：协程本地变量，模块说明里解释过了。
from contextvars import ContextVar

# @dataclass 是个"代码生成器"装饰器：类里只声明字段，它自动生成构造方法、toString 等。
# 类比 Java：等于 Lombok 的 @Data，或者 JDK16+ 的 record。
# frozen=True 表示对象创建后字段不许改，改了就抛异常——类比 Java record 的不可变。
from dataclasses import dataclass

# structlog 是日志库（common/logging.py 封装的就是它），这两个方法来自它：
#   bind_contextvars：往"日志上下文"里写键值对，之后本协程打的每条日志自动带这些字段。
#   clear_contextvars：把上面写的清掉，防止串到下一个请求。
# 为什么 context.py 要碰日志的类？因为测试要求日志自动带 task_id / item_id，
# 而 structlog 只认它自己的存储，所以 set_run_context 里要"双写"：
#   一份写进我们的 ContextVar（业务代码读），一份写进 structlog（日志框架读）。
from structlog.contextvars import bind_contextvars, clear_contextvars

# 导出清单，含义见 config.py。上层只 import 这五个名字。
__all__ = [
    "AuthContext",
    "clear_run_context",
    "item_id_var",
    "set_run_context",
    "step_var",
    "task_id_var",
]

# 下面三个是"协程本地变量"。构造参数 "task_id" 是变量名（调试时显示用），
# default 是"从没设置过时"读出来的默认值。
# default 给 None 而不是让它报错的原因：脚本、测试这类没走 set_run_context 的代码，
# 读一下也不会崩。类比 ThreadLocal.withInitial(() -> null)。
# 冒号后面的 str | None 是类型标注："字符串或者 None"，只是给人看和给检查工具看的，不影响运行。
task_id_var: ContextVar[str | None] = ContextVar("task_id", default=None)
# 当前评分项编号，比如 "S3.2.4"。日志检索和证据回查都靠它定位。
item_id_var: ContextVar[str | None] = ContextVar("item_id", default=None)
# 当前执行到第几步（从 1 开始数），0 表示还没开始干活。
step_var: ContextVar[int] = ContextVar("step", default=0)


# @dataclass(frozen=True) 写在类上一行，是装饰器语法。
# Python 的装饰器类似 Java 的注解，但更强：注解只是打标记，装饰器是真的在改写这个类——
# 它会根据下面声明的字段，自动生成 __init__（构造方法）、__repr__（toString）等。
# 没有它，你得手写十几行 def __init__(self, user_id, project_id, ...) 逐个赋值。
@dataclass(frozen=True)
class AuthContext:
    """一次评审请求的身份，从 API 层一路传到检索 SQL。

    字段含义：
        user_id    操作人 ID，审计日志用（记谁查的）。
        project_id 属于哪个项目，检索 SQL 过滤的第一道闸（WHERE project_id = ...）。
        role       角色（expert / admin / viewer）。目前只做展示和审计，硬权限控制靠 acl。
        acl        权限标签集合，格式 ["proj:xxx", "sec:标段1", "lvl:公开"]，
                   和数据库 documents.acl / chunks.acl 两列存的内容一一对应。

    frozen=True（不可变）不是风格偏好，是安全要求：
    如果身份对象能被中途改掉 project_id，就等于越权。
    想换个项目查，调 scoped_for() 生成新对象，别改旧对象。
    测试 test_frozen_context 就是专门验证"改字段必须报错"的。
    """

    # 下面四个是字段声明。dataclass 的字段直接写在类体里，和 Java 不同：
    # 不用先写 private 字段再写构造方法，@dataclass 会自动生成构造方法。
    # 生成后的用法：AuthContext(user_id="u1", project_id="proj-A", ...)，
    # 跟 Java 的 builder 或 AllArgsConstructor 一个意思。
    # 带默认值 = 的字段（role、acl）可以不传，相当于 Java 里另一个少参数的构造重载。
    user_id: str
    # 身份所属项目，必填。
    project_id: str
    # 角色名，默认 viewer。
    role: str = "viewer"
    # tuple 是"不可变的列表"，类比 Java 的 List.of(...) 建出来的不可变 List。
    # tuple[str, ...] 读作"字符串元组，长度不限"。() 是空元组，即"没配任何标签"。
    acl: tuple[str, ...] = ()

    def acl_tags(self) -> list[str]:
        """把权限标签整理成列表，给 acl_json() 和日志用。

        为什么要兜底？数据库的过滤条件是 SQL 的 @> 操作符（意思是"包含"），
        它对空数组永远返回"不包含"。acl 没配的话，所有数据都会被过滤成空，
        而且不报任何错——查的人只会看到"这个项目什么都搜不到"，根本想不到是权限没配。
        所以这里兜个底：acl 空的话，至少给一个项目标签，保证"自己项目默认可见"。
        """
        # acl 有值时原样转成 list 返回。Python 里容器可以直接写在 if 里当条件用：
        # 非空元组按 True、空元组按 False 处理，相当于 Java 的 list.isEmpty() 取反。
        if self.acl:
            # list(self.acl) 把元组转成列表。
            # 为什么转？json 序列化虽然两种都支持，但统一用 list，
            # 和数据库里 JSONB 数组的形态保持一致。
            return list(self.acl)
        # acl 是空的：只给项目标签。
        # f"proj:{self.project_id}" 是 f-string（字符串模板），
        # 大括号里的表达式会被替换成实际值，相当于 Java 的 String.format("proj:%s", ...)。
        return [f"proj:{self.project_id}"]

    def acl_json(self) -> str:
        """权限标签转成 JSON 字符串，直接当 SQL 查询参数用。

        返回长这样：'["proj:proj-A","sec:标段1"]'
        数据库的 acl 列是 JSONB 类型，查询条件 acl @> 参数 直接吃这个字符串。
        测试用 json.loads 把返回值解析回来做断言，所以必须是合法 JSON。

        ensure_ascii=False 这个参数必须带：不加的话中文会被转成 \\u6807\\u6bb5
        这种转义形式，和数据库里存的原文对不上，过滤就失效了。
        """
        # 复用 acl_tags() 的兜底逻辑，本方法只负责"转 JSON"这一件事。
        # json.dumps 相当于 Jackson 的 writeValueAsString；ensure_ascii=False 是"中文原样输出"。
        return json.dumps(self.acl_tags(), ensure_ascii=False)

    def scoped_for(self, project_id: str) -> AuthContext:
        """生成一个"换到另一个项目查"的新身份，原对象不动。

        参数 project_id：目标项目 ID。
        返回：新的 AuthContext。

        为什么必须把 proj 标签换掉？
        SQL 过滤条件是 acl @> 参数（数据库里的标签必须"包含"身份带来的标签）。
        身份带着 proj:proj-A 去查 proj-B 的数据，@> 永远不成立，
        返回空列表且不报错——查的人会误以为"B 项目没有资料"。
        正确做法：proj 标签换成目标项目的；标段、密级标签保留
        （那是"这个人"的权限属性，换个项目查也不该丢）。

        方法签名里的 -> "AuthContext" 是返回值类型标注。
        加引号是因为 AuthContext 这个类还没定义完，Python 约定用字符串提前引用自己。
        """
        # 从现有标签里挑出"非 proj 开头"的（标段、密级），这些要保留。
        # 这行是列表推导式，等价的普通写法是：
        #   kept = []
        #   for tag in self.acl:
        #       if not tag.startswith("proj:"):
        #           kept.append(tag)
        # startswith 相当于 Java 的 String.startsWith。
        kept = [tag for tag in self.acl if not tag.startswith("proj:")]
        # 组装新标签：新项目的 proj 标签放最前面，后面跟上保留的标签。
        # [*a, *b] 是"拆开合并"的写法，[f"proj:...", *kept] 等于把 proj 标签和 kept 拼成一个列表。
        # 外面套 tuple() 转回元组，和字段声明的类型 tuple[str, ...] 保持一致。
        tags = tuple([f"proj:{project_id}", *kept])
        # 构造新对象返回。user_id 和 role 原样带过去——权限主体没变，只是换了个项目查。
        return AuthContext(user_id=self.user_id, project_id=project_id, role=self.role, acl=tags)


def set_run_context(task_id: str, item_id: str, *, step: int = 0) -> None:
    """给当前协程绑定"正在干什么"（任务 ID / 评分项 / 第几步）。

    参数：
        task_id 本次评审任务的 ID，任务入口处生成。
        item_id 当前评分项编号，如 "S3.2.4"。
        step    第几步，从 1 开始。

    注意参数列表里的 * 号：它后面的参数只能用关键字传（step=3），
    不能按位置传（set_run_context("a", "b", 3) 会报错）。
    这是故意的：逼着调用方写 step=3，防止日子久了没人记得第三个裸参数是什么意思。

    为什么一个函数要写两遍（存两份）？
    我们的 ContextVar 给业务代码读（trace 上报、证据回查），
    structlog 的存储给日志框架读（日志处理器只认它自己的）。
    两个体系各存一份，这个函数是唯一的写入点，不会出现两边不一致。
    """
    # ContextVar 的 set()：在"当前协程"里写值，不影响其他并发协程。类比 ThreadLocal.set()。
    task_id_var.set(task_id)
    # 同上，写评分项编号。
    item_id_var.set(item_id)
    # 同上，写步数。
    step_var.set(step)
    # structlog 侧同步写一份。写完后，本协程之后打的每条日志自动带上这三个字段。
    # test_logging.py 里"日志自动带 item_id"的测试验证的就是这一行。
    bind_contextvars(task_id=task_id, item_id=item_id, step=step)


def clear_run_context() -> None:
    """清空当前协程的上下文。请求结束、测试收尾时调用。

    为什么必须清：不清的话，上一个请求的 task_id 会留在协程里，
    万一这个协程被复用去处理下一个请求，日志就串了，排查时张冠李戴。
    """
    # ContextVar 没有"删除"操作，用默认值覆盖回去就是清空。
    task_id_var.set(None)
    # 同上。
    item_id_var.set(None)
    # 步数回 0，和变量定义处的 default 一致。
    step_var.set(0)
    # 把 structlog 那边写的键值对也清掉，之后日志里不再出现这三个字段。
    clear_contextvars()
