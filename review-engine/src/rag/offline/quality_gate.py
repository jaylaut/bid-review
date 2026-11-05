"""
R1 质量门（docs/rag/R1-解析与清洗.md §质量门）。

【这个模块解决什么问题】
标书解析经常失败：加密 PDF、损坏文件、扫描件模糊到 OCR 出来全是乱码。
如果这些"坏数据"不拦下来，乱码 chunk 会安静地进到数据库里，
然后在某个评分项上表现为"检索不到依据"，你查一个星期都查不到解析器头上。

【这个模块怎么做】
给每一份解析产物打一个 0~8 分，分数低于及格线的进"人工复核队列"，
而不是直接进库。这样"静默的坏数据"就变成了"显式的待办事项"。

【阈值从哪来】
全部来自 config/rag.yaml → rag.offline.parsing.quality_gate，
由调用方读出来后用 QualityThresholds.from_mapping(...) 构造配置对象传进来。
本模块不读文件、不写文件、不设任何默认值——阈值硬编码进代码是清单第 1 条明令禁止的事。

【约定】本文件是纯逻辑、无副作用（不写文件、不碰数据库），方便写单元测试。
"""
# 导入标准库模块。
# re：正则表达式，用来匹配"乱码"这类文本模式。
# Mapping / Any：只用于标注"从 yaml 读出来的配置字典"的类型，运行时不产生依赖。
# dataclass：用一个 @dataclass 装饰器自动生成类的 __init__/__repr__ 等方法，少写样板代码。
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# ── 乱码判定用的字符白名单 ──────────────────────────────────────────────
# 思路：不枚举"坏字符"（永远枚举不完），而是枚举"读得出来的字符"。
# 一行里读不出来的字符占比超阈值，这行才判为乱码行；标点密度、表格分隔线、换行
# 都属于白名单内，天然不会被误判（旧实现把 "\n\n#### " 也当成乱码，见文件末尾说明）。
# 白名单包含：
#   \s                                  空白（空格/换行/制表）
#   \x20-\x7e                           ASCII 可打印字符（含 | - = # 等 markdown 语法符）
#   \u00a0-\u00ff                       Latin-1 补充（°±×÷©® 等）
#   \u2000-\u2bff                       通用标点 + 箭头 + 几何图形 + 带圈数字（…—①★→■ 等）
#   \u3000-\u303f                       CJK 标点（，。、《》「」【】〔〕等）
#   \u3400-\u4dbf                       CJK 扩展 A（生僻字）
#   \u4e00-\u9fff                       CJK 基本区（正文汉字）
#   \uf900-\ufaff                       CJK 兼容表意区
#   \uf000-\uf0ff                       Word 的 Symbol/Wingdings 私有区项目符号
#                                       （◆▪ 这类，实测真实标书里有 7 处，属合法字符）
#   \uff00-\uffef                       全角字符（Ａ１，：（）等）
# 取反集就是"读不出来的字符"：替换符 U+FFFD、控制符、私用区、以及 OCR 误识出来的
# 西里尔/阿拉伯/天城文等异体文字。
_ILLEGIBLE = re.compile(
    r"[^\s\x20-\x7e\u00a0-\u00ff\u2000-\u2bff\u3000-\u303f"
    r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uf000-\uf0ff\uff00-\uffef]"
)

# 编码无法识别时用来占位的替换字符 U+FFFD。单独计数：它的出现是"OCR 彻底失败"的强信号。
_REPLACEMENT = re.compile(r"\ufffd")

# 乱码特征词：由"合法汉字"组成的典型乱码，字符集白名单判不出来，只能用字面量兜底。
# 锟斤拷/烫烫烫/屯屯屯是 GBK↔UTF-8 互转的经典产物；ï¿½ 是 UTF-8 被当 Latin-1 读的结果。
_MOJIBAKE = ("锟斤拷", "烫烫烫", "屯屯屯", "ï¿½")


@dataclass
class QualityReport:
    """
    一份解析产物的质量评估结果。

    这个类是一个"数据容器"，只负责装数据、不负责干活。
    用 dataclass 修饰后，Python 会自动帮我们生成 __init__ 构造方法，
    我们只需要像填表一样声明"有哪些字段、各是什么类型"。

    字段说明：
        score: int          总分（0~8），由下面各个维度累加得到。
        reasons: list[str]  每个维度扣分/得分的文字说明，方便人工复核时一眼看懂为什么这么低分。
        needs_review: bool  是否需要人工复核（score 低于 quality_gate.min_score 时为 True）。
    """

    score: int                  # 总分，int 类型
    reasons: list[str]          # 说明列表，类型 list，元素 str
    needs_review: bool          # 是否进入人工复核队列


@dataclass(frozen=True)
class QualityThresholds:
    """
    质量门的全部阈值（对应 config/rag.yaml 的 quality_gate 段）。

    【为什么要这个类】
    四个维度一共要用 9 个数字。如果每个都当函数参数往下传，函数签名会烂掉；
    如果写死在函数体里，就违反了"阈值全部进配置"的硬规矩。
    用一个不可变对象把它们打包，既能一次性传下去，又保证"传进来的就是配置里的"。

    【为什么没有默认值】
    有默认值 = 阈值偷偷藏在代码里。缺 key 必须当场炸（KeyError），
    而不是静默用默认值跑出一份看起来正常的评分——那是更难查的错。

    字段说明（括号里是 rag.yaml 里的路径）：
        min_score:            及格线（min_score）
        replacement_warn:     替换符"少量"上限（thresholds.encoding.replacement_warn）
        garbage_lines_warn:   乱码行"少量"上限（thresholds.encoding.garbage_lines_warn）
        garbage_ratio:        行内不可读字符占比 ≥ 此值 → 该行判为乱码行（thresholds.encoding.garbage_ratio）
        min_chars_full:       内容充实下限（thresholds.length.min_chars_full）
        min_chars_partial:    内容偏少下限（thresholds.length.min_chars_partial）
        min_headers_full:     标题充足下限（thresholds.structure.min_headers_full）
        min_headers_partial:  标题稀疏下限（thresholds.structure.min_headers_partial）
        residue_warn:         水印可容忍残留处数（thresholds.watermark.residue_warn）
    """

    min_score: int                      # 及格线
    replacement_warn: int               # 替换符"少量"上限
    garbage_lines_warn: int             # 乱码行"少量"上限
    garbage_ratio: float                # 单行不可读字符占比阈值
    min_chars_full: int                 # 内容充实下限
    min_chars_partial: int              # 内容偏少下限
    min_headers_full: int               # 标题充足下限
    min_headers_partial: int            # 标题稀疏下限
    residue_warn: int                   # 水印残留可容忍处数

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any]) -> "QualityThresholds":
        """
        从 rag.yaml 的 quality_gate 段构造配置对象。

        【参数】
            cfg: Mapping  —— 例如 yaml["rag"]["offline"]["parsing"]["quality_gate"]，
                            形如 {min_score: 6, sample_rate: 0.05, thresholds: {...}}。

        【返回值】
            QualityThresholds  —— 已校验过类型的阈值对象。

        【为什么缺 key 要抛 KeyError】
            fail fast。少配一个阈值就当场炸在启动时，而不是跑完 52 份标书之后
            才发现其中一份的分数是按默认值算的。sample_rate 本函数不用（它属调度侧），
            所以不在这里读。
        """
        # 先把四段子配置取出来：任何一段缺失都会立刻 KeyError，报错信息里带着缺失的 key 名。
        encoding = cfg["thresholds"]["encoding"]
        length = cfg["thresholds"]["length"]
        structure = cfg["thresholds"]["structure"]
        watermark = cfg["thresholds"]["watermark"]

        # int(...) / float(...) 兜一层：yaml 里手滑写成 "6" 也能转成数字，写错则当场 ValueError。
        return cls(
            min_score=int(cfg["min_score"]),
            replacement_warn=int(encoding["replacement_warn"]),
            garbage_lines_warn=int(encoding["garbage_lines_warn"]),
            garbage_ratio=float(encoding["garbage_ratio"]),
            min_chars_full=int(length["min_chars_full"]),
            min_chars_partial=int(length["min_chars_partial"]),
            min_headers_full=int(structure["min_headers_full"]),
            min_headers_partial=int(structure["min_headers_partial"]),
            residue_warn=int(watermark["residue_warn"]),
        )


def _count_garbage_lines(md: str, ratio: float) -> int:
    """
    数"乱码行"的条数（行级判定，替代原来"数怪异符号片段"的做法）。

    【功能实现原理】
    逐行看：这一行里"读不出来的字符"占该行非空白字符的比例。
    占比 ≥ ratio 就把这行计为 1 条乱码行。空行直接跳过（不参与判定）。
    最后再加上几个乱码特征词的出现次数（它们由合法汉字组成，字符集判不出来）。

    【为什么要换口径】
    旧实现 `[^\\w\\u4e00-\\u9fff]{3,}` 把空白换行也算"怪异符号"，
    于是 markdown 的 "\\n\\n#### "、句号+换行+井号都成了"乱码段"。
    实测一份完全干净的真实标书，旧口径数出 1323 处 → 该维度恒判 0 分。
    表格分隔线 "|---|"、连续中文标点在新口径下占比为 0，不再被误伤。

    【参数】
        md: str     —— 整份文档的文本。
        ratio: float—— 单行不可读字符占比阈值（来自配置 garbage_ratio）。

    【返回值】
        int  —— 乱码行数（含乱码特征词出现次数）。
    """
    garbage_lines = 0

    # 逐行扫描：splitlines() 同时处理 \n / \r\n / \r，不会把行尾符混进内容里
    for line in md.splitlines():
        # 去掉首尾空白后为空的行（纯空行 / 纯缩进行）没有判定的意义，跳过
        body = line.strip()
        if not body:
            continue

        # 这一行里"读不出来的字符"有几个
        bad = len(_ILLEGIBLE.findall(body))

        # bad 为 0 时不必算比例（也避免除零讨论）；占比过线才算一条乱码行
        if bad and bad / len(body) >= ratio:
            garbage_lines += 1

    # 乱码特征词：每出现一次算一条，用来兜住"合法汉字拼出来的乱码"
    for token in _MOJIBAKE:
        garbage_lines += md.count(token)

    return garbage_lines


def _score_encoding(md: str, th: QualityThresholds) -> tuple[int, str]:
    """
    打分维度一：检查文本是否有乱码。

    【功能实现原理】
    解析失败的典型表现是 OCR 输出一堆不可读的符号，
    比如 "�"（替换字符，U+FFFD）或者整行读不出来的字符。
    这里统计两个量：替换符个数、乱码行数；两者都干净才给满分。

    【参数】
        md: str                 —— 一份已解析出来的 markdown 文本（整份文档的字符串）。
        th: QualityThresholds   —— 阈值配置对象。

    【返回值】
        tuple[int, str]  —— (本维度得分 0~2, 人读的说明)。
    """
    # 替换符个数：OCR 彻底失败的强信号，单独计
    replacement_count = len(_REPLACEMENT.findall(md))

    # 乱码行数：读不出来的字符成片出现的行
    garbage_lines = _count_garbage_lines(md, th.garbage_ratio)

    # 两种都为 0：文本干净，给满分 2 分
    if replacement_count == 0 and garbage_lines == 0:
        return 2, "文本干净，无乱码"

    # 都在"少量"容忍线内：有瑕疵但不致命，给 1 分
    if replacement_count <= th.replacement_warn and garbage_lines <= th.garbage_lines_warn:
        return 1, f"存在少量乱码(替换符{replacement_count}处，乱码行{garbage_lines}行)"

    # 超出容忍线：这份文档的正文已经不可读了，给 0 分
    return 0, f"大量乱码(替换符{replacement_count}处，乱码行{garbage_lines}行)"


def _score_length(md: str, th: QualityThresholds) -> tuple[int, str]:
    """
    打分维度二：检查内容是否"空壳"（满分 2 分）。

    【功能实现原理】
    解析失败还有一种典型表现：文档看着有内容，去掉空白后几乎没字
    （扫描件 OCR 全失败，只识别出几个零散字符）。
    用"非空白字符总数"衡量内容是否充实。

    【参数】
        md: str                 —— 整份文档的 markdown 文本。
        th: QualityThresholds   —— 阈值配置对象。

    【返回值】
        tuple[int, str]  —— (本维度得分 0~2, 说明)。
    """
    # 去掉所有空白字符后，剩余字符数
    # re.sub(r"\s","",md) 把空格/换行/制表符 替换为空串，剩下真正内容，在len计数
    content_chars = len(re.sub(r"\s", "", md))

    # 内容充实，加 2 分
    if content_chars >= th.min_chars_full:
        return 2, f"内容充实({content_chars} 字符)"

    # 内容偏少，给 1 分
    if content_chars >= th.min_chars_partial:
        return 1, f"内容偏少({content_chars} 字符)，疑似解析不全"

    # 几乎没内容：给 0 分
    return 0, f"内容近乎为空（{content_chars} 字符）"


def _score_structure(md: str, th: QualityThresholds) -> tuple[int, str]:
    """
    打分维度三：检查是否有正常标题结构（满分 2 分）。

    【功能实现原理】
    正常解析的标书 markdown 应有若干 `#` 开头的标题行。
    若一个标题都没有，说明要么纯扫描件没解析出结构，要么结构丢失，
    后续 R2 无法按层级切节，引用定位也就废了。

    【参数】
        md: str                 —— 整份文档的 markdown 文本。
        th: QualityThresholds   —— 阈值配置对象。

    【返回值】
        tuple[int, str]  —— (本维度得分 0~2, 说明)。
    """
    # 找出所有 "行首 1~6个 # 后面跟空白" 的标题。
    # ^#{1,6}\s 表示行首 + 1~6 个 # + 空白；re.MULTILINE 让 ^ 匹配每行行首
    # （不加 MULTILINE 时 ^ 只认整个字符串开头，整本标书最多命中 1 个标题）
    headers = re.findall(r"^#{1,6}\s", md, flags=re.MULTILINE)

    # 标题足够，结构完整，给 2 分
    if len(headers) >= th.min_headers_full:
        return 2, f"标题结构正常({len(headers)}个标题)"

    # 有标题，但是少，给 1 分
    if len(headers) >= th.min_headers_partial:
        return 1, f"标题结构稀疏（仅 {len(headers)} 个标题）"

    # 一个标题都没有：给 0 分
    return 0, "无标题结构，疑似纯扫描件或解析失败"


def _score_page_text(md: str, th: QualityThresholds) -> tuple[int, str]:
    """
    打分维度四：检查是否有水印/页眉页脚残留（满分 2 分）。

    【功能实现原理】
    清洗模块本应去掉水印和页眉页脚。若清洗后还大面积残留
    "机密/内部资料/严禁外传"这类水印词，说明清洗没生效，
    这些噪声会污染后续分块和检索。

    【参数】
        md: str                 —— 清洗之后的 markdown 文本。
        th: QualityThresholds   —— 阈值配置对象。

    【返回值】
        tuple[int, str]  —— (本维度得分 0~2, 说明)。
    """
    # 统计三种常见水印词出现的总次数，| 表示"或"
    watermark_count = len(re.findall(r"(机密|内部资料|严禁外传)", md))

    # 无水印残留：给满分 2 分
    if watermark_count == 0:
        return 2, "无水印/页眉页脚残留"

    # 少量残留：给 1 分
    if watermark_count <= th.residue_warn:
        return 1, f"存在 {watermark_count} 处水印残留"

    # 大量残留：给 0 分
    return 0, f"水印/页眉页脚残留严重（{watermark_count} 处）"


def score_parse_quality(md: str, th: QualityThresholds) -> QualityReport:
    """
    质量门入口：给一份解析产物打 0~8 分，并判断是否进复核队列。

    【功能实现原理】
    四个维度各 2 分，加起来 8 分：
        编码干净(0~2) + 内容充实(0~2) + 结构正常(0~2) + 无水印(0~2) = 0~8。
    得分累加、说明收集，最后用 th.min_score 判断是否低于及格线。

    【参数】
        md: str                 —— 清洗后的 markdown 文本。
        th: QualityThresholds   —— 阈值配置对象，由调用方从 config/rag.yaml 的
                                  quality_gate 段构造（清单第 1 条：阈值不硬编码，
                                  本函数不设默认值，强制显式传入）。

    【返回值】
        QualityReport  —— score(总分)、reasons(各维度说明)、needs_review(是否复核)。
    """
    # 依次调用四个打分函数，得到 4 个元组
    dims = [
        _score_encoding(md, th),            # 维度一：编码是否干净
        _score_length(md, th),              # 维度二：内容是否充实
        _score_structure(md, th),           # 维度三：结构是否正常
        _score_page_text(md, th),           # 维度四：是否有水印残留
    ]

    # 把四个维度的第一项(得分)累加得到总分
    total = sum(d[0] for d in dims)

    # 把四个维度的第二项(说明文字)收集成列表
    reasons = [d[1] for d in dims]

    # 判断是否进入人工复核：总分严格小于及格线
    needs_review = total < th.min_score

    # 组装 QualityReport 返回
    return QualityReport(score=total, reasons=reasons, needs_review=needs_review)
