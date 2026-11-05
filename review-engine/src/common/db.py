"""数据库模块：管 PostgreSQL 的异步连接池，全项目共用一个。

给 Java 同学的定位：相当于 HikariCP 数据源 + JdbcTemplate 的合体——
池子全局只有一个，常用查询和事务的快捷方法都在这里，上层不用自己管连接的借还。

为什么代码里到处是 async / await？
这个服务同时处理几十个评审任务。传统同步模式下，一个查询等数据库的 50 毫秒里
线程就干等着（Java 通常靠多线程扛并发）。Python 的 async 是另一条路：
await 等待的时候把控制权让出去，让别的任务先跑，一个线程就能同时挂着几十个等待中的请求。
代价是语法上到处都要写 async / await——不是我们想写这么复杂，
是数据库驱动（asyncpg）全是异步的，调用它的代码被"传染"了，只能一路 async 到底。

对外提供（tests/common/test_db.py 按这份清单测）：
    get_pool()    拿连接池（第一次调用时才创建，之后复用同一个）
    acquire()     借一条连接（async with 用法，用完自动归还）
    close_pool()  关池（测试之间复位用）
    ping()        探活，能执行 SELECT 1 就返回 True
    fetch() / fetch_val() / execute()   三种常用查询的快捷方法
    transaction() 事务，块内代码抛异常自动回滚

另外，每条连接建立时都会注册 pgvector 的类型转换（见 _init_connection），
这是后面往数据库存向量数据的前提。
"""
# 这行的作用见 config.py 顶部的说明，照抄即可。
from __future__ import annotations

# asyncio 是 Python 异步编程的标准库。下面只用到它的 wait_for：
# 给一个异步操作加超时，超时还没完成就报错。类比 Java 的 future.get(3, TimeUnit.SECONDS)。
import asyncio

# AsyncIterator：类型标注，表示"异步的迭代器"。被 @asynccontextmanager 装饰的函数都用它标注返回值。
from collections.abc import AsyncIterator

# asynccontextmanager：装饰器，把带 yield 的异步函数变成能被 async with 使用的对象。
# 原理是本文件最需要理解的一个点，见 acquire() 函数的详细注释。
from contextlib import asynccontextmanager

# Any：类型标注，表示"随便什么类型"。SQL 参数的个数和类型都不固定，用它兜底。
from typing import Any

# asyncpg：PostgreSQL 的异步驱动，项目连库就靠它（pyproject.toml 里声明过依赖）。
# 项目规定不用同步驱动：同步驱动等待时会卡住整个服务。
import asyncpg

# register_vector：pgvector 库提供的函数，给连接装上"数据库向量列 ↔ Python 列表"的双向转换。
from pgvector.asyncpg import register_vector

# 只用到配置里的三个值：连接串、池最小/最大连接数。
from common.config import get_settings

# 导出清单，含义见 config.py。上层按这份清单 import。
__all__ = [
    "acquire",
    "close_pool",
    "execute",
    "fetch",
    "fetch_val",
    "get_pool",
    "ping",
    "transaction",
]

# 全进程唯一的池。None 表示还没创建过。类比 Java 里一个 static 的 DataSource 字段。
_pool: asyncpg.Pool | None = None

# 单条 SQL 的超时（秒）。网络抖动、锁等待时快速失败，
# 别让一个卡住的查询把整个服务拖住。
_COMMAND_TIMEOUT_S = 30.0
# 探活的超时（秒）。探活就要快，3 秒出不了结果就当数据库有问题。
_PING_TIMEOUT_S = 3.0


async def _init_connection(conn: asyncpg.Connection) -> None:
    """池创建新连接时的回调，asyncpg 会在每条连接建立时自动调用一次。

    参数 conn：刚握完手的连接对象。

    做的事只有一件：注册向量类型的转换规则。注册之后，
    这条连接上 Python 的 list[float] 可以直接读写数据库的 vector 列，
    不用手动转 JSON 字符串。

    注意两点（都是踩过坑的）：
    1. 这个函数必须是 async 的：pgvector 新版把 register_vector 做成了协程函数，
       必须用 await 调用；asyncpg 的 init 回调也支持协程函数，两边正好对上。
    2. 函数里不发任何 SQL，只是往连接对象里塞类型映射规则，速度很快。
    """
    # await 注册转换规则。不注册的后果：往向量列插 list[float] 直接报类型错误，
    # 这是后面联调入库时最容易卡住的地方。
    await register_vector(conn)


async def get_pool() -> asyncpg.Pool:
    """拿连接池。第一次调用时创建，之后每次都返回同一个。

    为什么放到第一次用时才创建（懒加载），而不是模块导入时就建？
    建池是要真的去连数据库的。而 import db 这个模块的进程不一定用数据库
    （各种脚本、只打日志的进程），import 时就建的话，环境里没有数据库它们连导入都过不了。

    asyncpg.Pool 类比 Java 的 HikariDataSource：内部维护若干条长连接，
    用的时候借、用完还，免得每次查询都重新走一遍 TCP 握手。
    """
    # 声明修改的是模块顶部的 _pool，不是新建局部变量（详见 config.py 里 global 的说明）。
    global _pool
    # 池已存在且没在被关闭：直接复用。这是最常走的路径，开销接近零。
    # is_closing() 是 asyncpg 池的方法：调过 close() 之后返回 True。
    # 注意这个版本（asyncpg 0.32）的方法名是 is_closing，不是 is_closed，网上老代码容易写错。
    if _pool is not None and not _pool.is_closing():
        return _pool
    # 走到这里说明是第一次调用，或者池被 close_pool() 关过了：重新创建。
    settings = get_settings()
    # create_pool 的参数逐个说明：
    #   dsn              连接串。用 config 里剥过皮的版本（asyncpg 不认 "+asyncpg" 那段）。
    #   min_size         常驻连接数，程序启动就先建好这么多。
    #   max_size         连接上限。40 是按"大模型并发 8 × 4 + 8"算的。
    #   command_timeout  单条 SQL 的超时。
    #   init             新连接建立时的回调，就是上面那个注册向量转换的函数。
    # 前面的 await 不能省：asyncpg 0.32 的 create_pool 返回的池要 await 一下
    # 才会真正去建连接（"初始化"）。不 await 的话，第一次借连接会报
    # "pool is not initialized"——本项目装的就是 0.32，这里必须写 await。
    _pool = await asyncpg.create_pool(
        dsn=settings.pg_dsn_asyncpg,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
        command_timeout=_COMMAND_TIMEOUT_S,
        init=_init_connection,
    )
    # 返回新建的池（下次调用走上面的复用分支）。
    return _pool


# 装饰器先看懂它"做了什么"再看"怎么做的"：
# @asynccontextmanager 把下面这个函数变成能被 async with 使用的对象，
# 用法固定是 async with db.acquire() as conn: ...
# （类比 Java 的 try-with-resources，只是 Python 靠 yield 分段实现，见下面的逐行注释。）
@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    """从池里借一条连接，with 块结束时自动归还（哪怕块内抛了异常也归还）。

    用法：
        async with db.acquire() as conn:
            await conn.execute(...)

    为什么包一层、不直接暴露池的 acquire？
    保证调用方拿到的连接一定是注册过向量转换的池连接，
    以后要加"借出/归还耗时"之类的埋点，也只需要改这一处。

    yield 的工作原理（本文件最绕的点，多看两眼）：
    Python 把这个函数按 yield 分成三段：
        yield 之前的代码 → 进 async with 块"之前"执行
        yield conn       → 把 conn 递给 with 块里的代码，然后暂停，等块执行完
        yield 之后的代码 → 块结束后"回来"执行（本例没有额外收尾，
                           因为 pool.acquire() 自己的 async with 会负责还连接）
    无论块内是否抛异常，收尾段都会执行——这就是"保证归还"的实现方式。
    """
    # 先确保池存在（第一次调用在这里触发建池）。
    pool = await get_pool()
    # asyncpg 池自带的借还协议：这个 async with 块结束时自动把连接放回池里。
    async with pool.acquire() as conn:
        # 把连接交给调用方的 with 块。类比 Iterator 的 next()，只是这里会"暂停等块执行完"。
        yield conn


async def ping() -> bool:
    """探活：数据库能用返回 True，不能用返回 False（报错也返回 False，不往上抛）。

    为什么不抛异常？这个函数是给启动自检（check_env.py）用的，
    自检要的只是一个"活着没"的结论，拿到 True / False 就能决定继续还是退出。
    具体错在哪，让日志和自检的其他检查项去说。
    """
    # 建池阶段（解析地址、TCP 握手、密码认证）都可能失败，单独包一层 try。
    # try / except 相当于 Java 的 try / catch，Python 不用写异常类型时可以裸写 except，
    # 但生产代码必须写明捕获哪些异常，像下面这样。
    try:
        # 懒建或复用池。
        pool = await get_pool()
    except (OSError, asyncpg.PostgresError):
        # 括号里写多个异常类型表示"捕获其中任意一种"，相当于 Java 的 catch (A | B e)。
        # OSError：连接拒绝、网络不通这类系统级错误。
        # asyncpg.PostgresError：数据库报的错（密码错、库不存在等）的基类。
        return False
    # 借连接、发查询两段都可能超时或报错，再包一层。
    try:
        # 从池里借一条连接（这些连接都注册过向量转换）。
        async with pool.acquire() as conn:
            # wait_for 给下面的执行加超时。有一种半死状态是 TCP 通着但数据库不响应，
            # 不加超时会永远等下去，探活反而被挂死。
            # conn.execute 返回执行状态字符串（比如 "SELECT 1"），这里不关心内容。
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=_PING_TIMEOUT_S)
        # 能走到这，数据库就是活的。
        return True
    except (TimeoutError, asyncpg.PostgresError, OSError):
        # 超时、数据库错误、网络错误，统一算"不健康"。
        return False


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    """执行查询，返回所有结果行。

    参数：
        query SQL 文本。参数用 $1、$2 占位——注意 asyncpg 只认 $1 这种写法，
        不认 ? 也不认 %s，网上抄来的 SQL 要改过才能用。
        另外绝对不许用字符串拼接把变量塞进 SQL（那是 SQL 注入的唯一来源）。
        args  占位符对应的值，按顺序对应 $1、$2……个数不固定，
        所以签名里用 *args（类比 Java 的可变参数 String... args）。
        向量列直接传 list[float]，类型转换在连接建立时已经注册好了。

    返回：asyncpg.Record 的列表。Record 取值按列名：row["project_id"]。
    """
    # 确保池就绪（第一次调用在这里触发建池）。
    pool = await get_pool()
    # 池的快捷方法：借连接、执行、归还一条龙。等价于手动 acquire + 执行 + 归还。
    return await pool.fetch(query, *args)


async def fetch_val(query: str, *args: Any) -> Any:
    """执行查询，只返回第一行第一列的值。

    适合 COUNT(*)、EXISTS(...)、SELECT 1 这种只关心一个数的查询。
    查不到东西时返回 None（类比 Java 返回 null），调用方记得判空。
    其他参数说明同 fetch()。
    """
    # 确保池就绪。
    pool = await get_pool()
    # fetchval 相当于 fetch 完再取 rows[0][0]，省得自己写下标。
    return await pool.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    """执行没有结果集的 SQL（INSERT / UPDATE / DELETE / 建表），返回状态串。

    返回的是 asyncpg 的状态串，比如 "INSERT 0 1"——末尾的 1 是影响行数。
    需要精确拿到影响行数时，解析这个串，或者改用 RETURNING 子句。
    其他参数说明同 fetch()。
    """
    # 确保池就绪。
    pool = await get_pool()
    # 池的快捷执行。
    return await pool.execute(query, *args)


# 同 acquire() 的说明：装饰器把这个函数变成能被 async with 使用的"事务"对象。
@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """事务。用法：

        async with db.transaction() as conn:
            await conn.execute("UPDATE ...")
            await conn.execute("INSERT ...")

    规则就一条：块内代码全部成功 → 提交（COMMIT）；
    块内任何地方抛异常 → 回滚（ROLLBACK），然后异常继续往外抛，调用方能感知到失败。
    测试 test_transaction_rollback 验证过：块内连建表（CREATE TABLE）都会被回滚掉——
    PostgreSQL 的建表语句也支持事务回滚，这点和 MySQL 不同。

    典型场景是后面的入库逻辑：先软删旧块、再写入新块，必须在一个事务里。
    如果删了旧块结果新块没写进去，数据就永久丢了——比不删还糟。
    """
    # 事务必须跑在同一条连接上（跨连接没有事务可言），所以先借一条。
    pool = await get_pool()
    # 借一条连接给整个事务用。
    async with pool.acquire() as conn:
        # conn.transaction() 开启数据库事务。这个 async with 正常退出 = COMMIT；
        # 块内抛异常 = 先 ROLLBACK，再把异常原样往外抛。
        async with conn.transaction():
            # 把这条"处于事务中的连接"交给调用方的 with 块，
            # 块内所有的 execute 跑的都是这个事务。
            yield conn


async def close_pool() -> None:
    """关池并把缓存清掉。重复调用安全（池不存在或已关时直接返回，不报错）。

    谁在调它：
    1. 业务进程退出前的收尾。
    2. tests/conftest.py 的自动夹具，每个测试跑完调一次。
       原因：asyncpg 的池绑定"创建它时的事件循环"，而 pytest 每个测试新建一个循环。
       不在每个测试后关池的话，第二个连数据库的测试必定报 "Event loop is closed"。
       这是 asyncio 的特性决定的，不是 bug。
    """
    # 声明修改的是模块顶部的 _pool（global 的说明见 config.py）。
    global _pool
    # 从没建过池（比如这次只跑了不碰数据库的测试）：无事可做。
    if _pool is None:
        return
    # 池还开着才关。is_closing() 防止对已经关掉的池重复关——那会抛错。
    if not _pool.is_closing():
        # close 会等所有借出去的连接还回来再关，不会掐断正在执行的查询。
        await _pool.close()
    # 缓存清空。下次 get_pool 会重新建一个（绑到新的事件循环上）。
    _pool = None
