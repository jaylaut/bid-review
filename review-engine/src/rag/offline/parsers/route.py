"""解析路由：拿前几页的文本密度决定走哪个 MinerU 后端。

扫描件是图片，没有文字层，常规通道解析出来是空的；文本型 PDF 走视觉通道又慢又贵。
52 个项目里两类都有，所以解析前先判一下走哪条路。

探针只看前 10 页。整本跑一遍探针比解析本身还慢，而标书不会出现
"前 10 页是正文、后面突然全是图片"这种情况。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 对外只暴露这三个。_text_density 是内部实现，改它不该影响调用方。
__all__ = ["ParseRoute", "detect_route", "sample_pages"]

# 算密度时用来去掉空白。\s 同时匹配半角空格、全角空格、换行、制表符。
_WS_RE = re.compile(r"\s")

# 能取到"页"的格式。
# docx 不算：Word 的分页是打开时按纸张和字号算出来的，文件里没有页边界，
# 读不出"前 10 页"，所以不进这个集合，直接走默认通道。
_PAGE_SUFFIXES = {".pdf", ".txt", ".md"}


@dataclass(frozen=True)
class ParseRoute:
    """一次路由的结果。

    backend：mineru（常规通道）或 mineru-vlm（视觉通道，能 OCR）。
    reason ：判断依据，会写进日志。判错的时候看这一句就知道错在哪。
    """

    backend: str
    reason: str


def _text_density(pages: list[str]) -> float:
    """算文本密度：非空白字符数 ÷ 总字符数。

    扫描件每页只 OCR 出零散字符，密度一般在 0.05 以下；原生 PDF 一页上千字，接近 1。
    两者差距很大，所以这个指标粗糙一点不影响判断。

    pages 是前若干页的文本。
    """
    # 分子：每页去掉空白后剩多少字符。
    content = sum(len(_WS_RE.sub("", page)) for page in pages)
    # 分母：每页原始长度。空页按 1 算，避免整份都是空页时除零。
    total = sum(max(len(page), 1) for page in pages)
    # 不用再判空：pages 非空时 total 至少等于页数。
    return content / total


def detect_route(page_samples: list[str], *, scanned_threshold: float) -> ParseRoute:
    """判这份文档该走哪个后端。

    阈值必须由调用方从配置传进来。这里写默认值等于把阈值藏进代码，
    以后改 yaml 不生效，还不会有任何提示。

    page_samples 是前 N 页的文本（N 从配置读，基线 10）。
    scanned_threshold 是扫描件判定阈值（config 里 router.scanned_threshold）。
    """
    # 一页都没取到：docx、没装 pypdf、PDF 加密，都会走到这里。
    # 没有依据就不猜，按常规通道处理——比硬判成扫描件安全。
    if not page_samples:
        return ParseRoute("mineru", "没有样本，按默认通道处理")

    # 算密度。
    density = _text_density(page_samples)

    # 低于阈值当扫描件，走视觉通道。
    if density < scanned_threshold:
        return ParseRoute("mineru-vlm", f"文本密度 {density:.3f} < {scanned_threshold}，判为扫描件")

    # 其余都是文本型，走常规通道，快且便宜。
    return ParseRoute("mineru", f"文本密度 {density:.3f} >= {scanned_threshold}，判为文本型")


def sample_pages(path: Path, limit: int) -> list[str]:
    """取前 limit 页文本，给探针用。

    取不到就返回空列表，不抛异常。探针只影响"是不是扫描件"的判断，
    它失败不该让整份文档解析不了——后面 MinerU 还能跑。

    path 是文件路径，limit 是抽几页。
    """
    # 后缀统一小写，Windows 上 .PDF 和 .pdf 是一回事。
    suffix = path.suffix.lower()

    # 不认识的格式不探，省掉后面的判断。
    if suffix not in _PAGE_SUFFIXES:
        return []

    # 纯文本：按 \f 切页。清洗那边用同一个分隔符，两边对"页"的定义保持一致。
    if suffix in {".txt", ".md"}:
        # errors="ignore"：标书 txt 里偶有坏字节，不能因为一个字节读不出整份文档。
        text = path.read_text(encoding="utf-8", errors="ignore")
        return text.split("\f")[:limit]

    # PDF 用 pypdf 抽文本。没装就降级：探针是加分项，不是解析的前提。
    try:
        from pypdf import PdfReader
    except ImportError:
        return []

    # 打开并读取。加密、损坏、缺依赖都在这里抛，统一兜住当成"没有样本"。
    try:
        reader = PdfReader(str(path))
        # 只读前 limit 页。扫描页 extract_text() 返回空串，这是正常结果，
        # 后面靠密度把它判成扫描件。
        return [(page.extract_text() or "") for page in reader.pages[:limit]]
    except Exception:
        # pypdf 各版本的异常类型不统一（PdfReadError / FileNotDecryptedError /
        # DependencyError），逐个 import 反而会因版本差异出错，这里统一兜住。
        return []
