"""配置模块：把 .env 文件和环境变量里的配置，读成一个 Python 对象给全项目用。

给 Java 同学的定位：相当于 Spring 的 @ConfigurationProperties + application.yml 的合体。
你只需要声明"有哪些字段"，读文件、类型转换、默认值这些活全由框架包办。

对外就三个东西（tests/common/test_config.py 按这份清单测）：
    Settings          配置对象本身，所有配置项都是它的字段
    get_settings()    拿配置的唯一入口，任何模块要配置都找它
    require_llm_key() 检查云端大模型的 key 配了没有，没配直接报错不让跑
"""
# 这行的作用是"类型标注先别急着求值"。没有它，下面写的 `Settings | None`
# （Settings 或者 None）这种新写法会在导入阶段报语法错误。
# 照抄即可，本项目每个源文件的第一行都是它。
from __future__ import annotations

# Path 是文件路径类，类似 java.nio.file.Path。下面用它拼出 .env 文件的位置。
from pathlib import Path

# 这两个类来自 pydantic-settings 库（pyproject.toml 里声明过的依赖）：
#   BaseSettings        配置类的"模板"。继承了它，这个类就自动会读 .env 和环境变量。
#   SettingsConfigDict  一张设置表，告诉 BaseSettings 怎么读（去哪找文件、用什么编码等）。
#                       相当于贴在类上的一组注解参数，只是 Python 里写成赋值的形式。
from pydantic_settings import BaseSettings, SettingsConfigDict

# __all__ 是导出清单：声明本模块对外只提供这三个名字。
# 别人写 `from common.config import *` 时只会拿到这三个，内部变量不会被带出去。
# 类比 Java：相当于 public 成员列表，其余都是包级私有。
__all__ = ["Settings", "get_settings", "require_llm_key"]

# .env 文件的固定位置。
# Path(__file__) 是 config.py 自己的路径，.resolve() 转成绝对路径，
# .parents[2] 是往上跳两级目录：
#   src/common/config.py → parents[0]=src/common → parents[1]=src → parents[2]=review-engine/
# 所以拼出来是 review-engine/.env。用绝对路径的好处：不管从哪个目录启动程序，
# 都能找到同一个 .env。用相对路径的话，换个目录启动就找不到了。
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# 全进程唯一的配置缓存。None 表示"还没加载过"。
# 名字开头带下划线是 Python 惯例：下划线开头 = 内部变量，外部代码不要直接碰。
# 类比 Java：一个 private static 字段。
_settings: Settings | None = None


class Settings(BaseSettings):
    """所有配置项都定义在这个类里。

    用法（Java 视角）：把它当成一个"所有字段都自动从 .env / 环境变量注入"的配置类。
    pydantic 读配置的优先级，从高到低：
        1. 构造时手动传的参数（测试里用这招跳过 .env，拿纯默认值）
        2. 系统环境变量（docker -e 注入的，权重高，方便容器部署时覆盖）
        3. .env 文件
        4. 下面字段写的默认值
    """

    # model_config 不是配置项，是"这个类怎么工作"的开关，写一次即可。
    # 每个参数的含义看下面各行的注释。
    model_config = SettingsConfigDict(
        # env_file：默认去哪找 .env 文件。文件不存在不会报错，全部字段走默认值，
        # 所以环境没准备好的时候测试也能跑。
        # 测试里用 Settings(_env_file=None) 可以显式跳过 .env 拿纯默认值。
        env_file=str(_ENV_FILE),
        # env_file_encoding：.env 用 utf-8 编码读。
        # 不写这条，Windows 默认用 GBK，.env 里的中文（查询指令那行）会读出乱码甚至报错。
        env_file_encoding="utf-8",
        # case_sensitive：环境变量大小写不敏感，
        # AGENT_MAX_STEPS 和 agent_max_steps 都能对上字段 agent_max_steps。
        case_sensitive=False,
        # extra="ignore"：.env 里混进来的无关键（别的项目残留、手滑写的）直接无视。
        # 不加这条，一个多余的字段就能让程序启动时抛异常，太脆了。
        extra="ignore",
    )

    # ── 运行环境 ──────────────────────────────────────────────
    # 当前环境：dev 开发 / prod 生产。日志模块按它决定输出格式（dev 给人看，prod 给机器解析）。
    app_env: str = "dev"
    # 服务名，写进每条日志的 service 字段，多套环境的日志混在一起时靠它区分来源。
    service_name: str = "review-engine"

    # ── PostgreSQL ────────────────────────────────────────────
    # 数据库连接串。带 "+asyncpg" 是 SQLAlchemy 的习惯写法（声明驱动用谁），
    # 但我们实际连库用的 asyncpg 库不认这一段，所以另给 pg_dsn_asyncpg 做剥皮（见下方 property）。
    # 默认值是本地 docker-compose 的开发账号，不是生产密码；生产凭据只能来自 .env / 环境变量。
    pg_dsn: str = "postgresql+asyncpg://bid:bid@localhost:5432/bid_review"
    # 池里常驻的连接数：程序一启动就先建好这么多连接，免得每次查询临时握手。
    pg_pool_min: int = 5
    # 池里连接的上限。40 是按"大模型并发 8 × 4 + 8"算出来的（docs/07 §A7）。
    # 再开大没意义——瓶颈在模型那边不在数据库，开大了只会顶到 PG 自己的连接数上限。
    pg_pool_max: int = 40

    # ── Redis ─────────────────────────────────────────────────
    # 三级缓存和取消标记都走它。末尾的 /0 是 0 号库。
    redis_url: str = "redis://localhost:6379/0"

    # ── RocketMQ（5.x，gRPC proxy 协议）────────────────────────
    # 消息队列地址。注意是 proxy 的 8081 端口，不是 NameServer 的 9876。
    # 写错的典型表现是"能连上但就是收不到消息"，排查半天。
    rocketmq_endpoint: str = "localhost:8081"
    # 文档入库的 topic，ingest worker 消费。
    rocketmq_topic_ingest: str = "bid-ingest"
    # 评审任务的 topic，评审 consumer 消费。
    rocketmq_topic_review: str = "bid-review"
    # 消费组名。同组内的消费者分摊消息，扩容实例靠它分组。
    rocketmq_group_review: str = "bid-review-consumer"
    # 单次拉取的消息条数（背压用：拉多了处理不过来）。
    rocketmq_receive_batch: int = 2
    # 消息不可见时长（秒）：消费者超时没确认，broker 到点自动重投这条消息。
    rocketmq_invisible_s: int = 60

    # ── MinIO（离线管道读原件用）──────────────────────────────
    # 对象存储地址（host:port，不带协议头，minio 的 SDK 自己拼）。
    minio_endpoint: str = "localhost:9000"
    # 本地 compose 的开发账号（.env.example 同源）；生产必须用 .env 覆盖。
    minio_access_key: str = "minioadmin"
    # 同上，仅开发默认值。
    minio_secret_key: str = "minioadmin123"
    # 原始标书文件所在的桶。
    minio_bucket_raw: str = "bid-raw"
    # 本机 compose 没配证书，固定 false；生产走 https 时改 true。
    minio_secure: bool = False

    # ── 判定大模型（云端，OpenAI 兼容接口）────────────────────
    # 供应商标识：deepseek | qwen，切换时 base_url 和 model 一起换。
    llm_provider: str = "deepseek"
    # 接口地址。项目用 httpx 库直接发 HTTP 请求，不用 openai 的 SDK（pyproject 里有注释说明）。
    llm_base_url: str = "https://api.deepseek.com"
    # 云端 key，默认空串。空串 + 没开本地判定时，require_llm_key() 会当场报错。
    llm_api_key: str = ""
    # 判定用的模型名。
    llm_model: str = "deepseek-chat"

    # ── 本地模型服务（Ollama）────────────────────────────────
    # 嵌入和重排都打这个地址。
    ollama_endpoint: str = "http://localhost:11434"
    # 嵌入模型名。注意：换模型 = 旧向量全部作废 = 全量重新嵌入（很贵），不要随手换。
    embedding_model: str = "qwen3-embedding:0.6b"
    # 向量维度，必须和建表语句里的 vector(1024) 完全一致，启动自检会核对这一项。
    embedding_dim: int = 1024
    # 查询侧指令（Qwen3 这类模型的要求）。改一个字 = 查询向量全变 = 旧索引全部失真，
    # 所以这个值不能悄悄改，必须走"换版本号 + 重新嵌入"的流程。
    embedding_query_instruction: str = "Instruct: 检索投标技术方案中响应以下技术要求的段落\nQuery: "
    # Ollama 的上下文窗口大小。必须显式设置——不设的话长文本被静默截断，且不报任何错。
    ollama_num_ctx: int = 1024

    # ── 重排 ──────────────────────────────────────────────────
    # 重排模型名。它靠返回的 logprobs 还原 0-1 的连续分数。
    rerank_model: str = "dengcao/Qwen3-Reranker-0.6B:Q8_0"
    # 送进重排的候选条数（从融合候选池里取前 20 条）。
    rerank_candidates: int = 20
    # 重排后保留的条数（最终拼进上下文的段落数）。
    rerank_top_n: int = 5
    # 拒答阈值：重排分低于它就判定"文档里没有相关内容"。
    # 这个值是实验标定出来的，不要拍脑袋改。
    rerank_refusal_threshold: float = 0.15

    # ── Agent 预算与护栏（docs/07 §A6）────────────────────────
    # 单个评分项最多走几步工具循环，防止模型陷入死循环烧 token。
    agent_max_steps: int = 6
    # 单个评分项的 token 上限。
    agent_max_tokens_per_item: int = 12000
    # 单个评分项的费用上限（元），超了硬停。
    agent_max_cost_per_item_cny: float = 0.05
    # 单个评分项的耗时上限（秒），超了硬停。
    agent_max_seconds_per_item: int = 120
    # 大模型调用并发数：走云端 API，受对方限流约束，不能开太大。
    agent_llm_concurrency: int = 8
    # 重排并发数：走本地 CPU 单实例，2 是保守值，开大了内存先扛不住。
    agent_rerank_concurrency: int = 2
    # 工具调用格式：native（模型原生 tool_calls）| json（结构化输出兜底）。
    agent_tool_call_mode: str = "native"
    # true = 判定也走本地 qwen3:14b（全内网演示形态，质量会降级）。
    # 开了它 require_llm_key() 就不再要求云端 key。
    local_llm: bool = False

    # ── 观测 ──────────────────────────────────────────────────
    # 链路追踪（trace）数据的上报地址。
    otel_endpoint: str = "http://localhost:4318"
    # Langfuse 面板地址。
    langfuse_host: str = "http://localhost:3000"
    # Langfuse 项目公钥（可以公开）。
    langfuse_public_key: str = ""
    # Langfuse 项目私钥，等于密码，泄露 = 别人能看全部 prompt。
    langfuse_secret_key: str = ""

    # @property 的意思是：这个方法用起来像字段，不像方法——
    # 写 settings.pg_dsn_asyncpg 就行，不用加括号。
    # 类比 Java 的 getter，区别只是调用时不写 get 前缀。
    @property
    def pg_dsn_asyncpg(self) -> str:
        """给数据库驱动用的连接串：把连接串里的 "+asyncpg" 去掉。

        背景：pg_dsn 按 SQLAlchemy 习惯写，长这样：
            postgresql+asyncpg://bid:bid@localhost:5432/bid_review
        但我们实际连库的 asyncpg 库不认识 "+asyncpg" 这一段，原样传会连接失败。
        这里把它删掉，变成 asyncpg 认识的：
            postgresql://bid:bid@localhost:5432/bid_review
        """
        # 字符串的 replace：把 "+asyncpg" 换成空串，其余部分原样保留。
        # 原字段 pg_dsn 不动——以后如果真用上 SQLAlchemy，还需要带方言头的完整版本。
        return self.pg_dsn.replace("+asyncpg", "")


def get_settings() -> Settings:
    """拿全局配置对象。第一次调用会真的去读 .env，之后直接返回缓存好的那个。

    为什么不每次都新建？两个原因：
    1. 读文件有开销，配置在进程运行期间又不会变，读一次就够了；
    2. 全项目必须拿到同一个对象。如果 A 模块和 B 模块各建一份，
       改了其中一份另一份不知道，排查问题时两边数据对不上。
    测试 test_get_settings_is_singleton 用 `get_settings() is get_settings()` 验证这一点。
    顺带说明 Python 的 is 和 == 的区别（和 Java 正好相反，容易搞混）：
        is 相当于 Java 的 ==（比引用，是不是同一个对象）；
        == 反而是比内容（相当于 Java 的 equals）。
    """
    # 声明"我要修改的是模块顶部的 _settings"，不是新建局部变量。
    # 这是 Python 的语法规定：函数里直接写 _settings = xxx 会被当成新建局部变量，
    # 必须先用 global 报备一声。Java 里给 static 字段赋值不需要声明，这条是 Python 特有的。
    global _settings
    # 还没加载过（第一次调用）：现在加载。
    if _settings is None:
        # Settings() 一执行，pydantic 就按 model_config 的规则去读 .env 和环境变量，
        # 并做类型转换和校验。比如 PG_POOL_MAX 配成 "abc"，这里就直接报错，
        # 不会等到真正用连接池时才炸——问题暴露得越早越好查。
        _settings = Settings()
    # 返回缓存好的对象。
    return _settings


def require_llm_key() -> str:
    """检查云端大模型的 key：没配就当场报错，配了就返回它。

    为什么单独写个函数，而不是等到用的时候再说？
    key 没配这种问题，如果等第一次真正调大模型接口才暴露，
    报错混在网络错误、业务错误里很难定位。放在任务开始前检查，
    一行报错直接告诉运维"去 .env 里填 LLM_API_KEY"，省事得多。

    返回值：
        正常情况：key 字符串，调用方拿去拼 HTTP 请求头。
        local_llm 开着时：空串——判定模型跑在本地，根本不需要云端 key。
    """
    # 拿全局配置（第一次调用会触发 .env 读取）。
    settings = get_settings()
    # local_llm 为 True 表示判定模型跑在本地 Ollama 上，不需要云端 key，直接放行。
    if settings.local_llm:
        # 返回空串表示"合法地不需要 key"，和"忘了配"是两回事。
        return ""
    # 走云端的情况：key 是空串就等于没配。
    # Python 里空串本身就按 False 处理，所以 not "" 就是 True，这是 Python 的判空惯用写法。
    if not settings.llm_api_key:
        # 报错信息直接写清楚怎么修：去哪个文件填哪个字段，运维不用翻文档。
        # 用 RuntimeError 而不是自定义异常类，因为这属于"环境没准备好"，不是业务错误。
        # 注意报错文案里必须保留 "LLM_API_KEY" 这个词——测试用正则匹配它。
        raise RuntimeError(
            "LLM_API_KEY 未配置：本地判定未开启（local_llm=false）时必须提供云端 key，"
            "请在 review-engine/.env 中填写 LLM_API_KEY（模板见 .env.example）"
        )
    # 一切正常，返回 key。
    return settings.llm_api_key
