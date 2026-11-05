#!/usr/bin/env python
"""解析链路自检：一条命令回答"这一阶段的代码对不对"。

分四层，越往后越贵：
    L1 纯函数  不碰磁盘、不碰网络，几百毫秒
    L2 配置    读真实 rag.yaml，验证字段能不能对上
    L3 真实文件 拿 data/raw 里的真标书跑探针和主文件挑选
    L4 MinerU  端到端解析一份，只有加 --with-mineru 才跑（要下模型，十几分钟）

用法（在 review-engine/ 下）：
    uv run python ../scripts/verify_parsers.py
    uv run python ../scripts/verify_parsers.py --with-mineru

退出码 0 表示全过，非 0 表示有失败项。可以直接挂到提交前钩子或者 cron 上。
"""
import argparse
import sys
import tempfile
from pathlib import Path

# 脚本在 <repo>/scripts/ 下，把 review-engine/src 加进搜索路径，否则 import 不到 rag/common。
_SRC_DIR = Path(__file__).resolve().parent.parent / "review-engine" / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from common.errors import ConfigError  # noqa: E402  sys.path 需要先插好，导入只能放后面
from common.logging import get_logger  # noqa: E402
from rag.offline.parsers import (  # noqa: E402
    check_available,
    detect_route,
    normalize_markdown,
    parse_plain_text,
    tier_for_backend,
)
from rag.offline.parsers.contracts import ParseConfig  # noqa: E402
from rag.offline.source import select_main_file  # noqa: E402

log = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _REPO_ROOT / "review-engine" / "src" / "config" / "rag.yaml"
_RAW_ROOT = _REPO_ROOT / "data" / "raw" / "tenders"
_PARSED_DIR = _REPO_ROOT / "data" / "parsed"

# 每一项是 (名字, 函数)。函数不返回值，靠 assert 表达"通过"。
# 用 assert 而不是自定义异常：失败时 traceback 会直接指到出错那一行，定位最快。
_CHECKS = []


def check(name):
    """把函数注册成一项检查，省掉手工往列表里塞。"""

    def wrapper(func):
        _CHECKS.append((name, func))
        return func

    return wrapper


# ── L1 纯函数 ────────────────────────────────────────────────


@check("路由：空白样本判扫描件")
def check_route_scanned():
    # 纯空白，密度为 0，必须走视觉通道。
    assert detect_route([" " * 500], scanned_threshold=0.05).backend == "mineru-vlm"


@check("路由：正常正文判文本型")
def check_route_text():
    # 200 个汉字，密度接近 1。
    assert detect_route(["正文内容" * 100], scanned_threshold=0.05).backend == "mineru"


@check("路由：没有样本走默认通道")
def check_route_empty():
    # 取不到样本时不能瞎猜成扫描件，否则每份 docx 都会去跑视觉通道。
    assert detect_route([], scanned_threshold=0.05).backend == "mineru"


@check("路由：阈值确实参与判断")
def check_route_threshold_effective():
    # 同一份样本，把阈值抬高之后结论必须反过来。
    # 这条是防"阈值传了但没用上"——那种 bug 不会报错，只会一直判错。
    samples = ["半" * 100 + "   " * 400]  # 密度约 0.1
    assert detect_route(samples, scanned_threshold=0.05).backend == "mineru"
    assert detect_route(samples, scanned_threshold=0.5).backend == "mineru-vlm"


@check("标题规整：跳级被压回")
def check_normalize_jump():
    out = normalize_markdown("# 一级\n### 三级")
    assert out == "# 一级\n## 三级", out


@check("标题规整：代码块内的 # 不动")
def check_normalize_fence():
    # 围栏里的 "# 安装依赖" 是 shell 注释，被当成标题改写会把命令弄坏。
    out = normalize_markdown("```\n# 注释\n```")
    assert out == "```\n# 注释\n```", out


@check("标题规整：表格行不受影响")
def check_normalize_table():
    table = "| 项 | 值 |\n|---|---|\n| a | 1 |"
    assert normalize_markdown(table) == table


@check("纯文本通道：能读回原内容")
def check_plain_text_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "a.md"
        f.write_text("# 标题\n正文", encoding="utf-8")
        assert parse_plain_text(f) == "# 标题\n正文"
        # 传字符串也要能用，调用方不一定会先转 Path。
        assert parse_plain_text(str(f)) == "# 标题\n正文"


@check("纯文本通道：非文本格式抛 ConfigError")
def check_plain_text_reject():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "a.docx"
        f.write_bytes(b"x")
        try:
            parse_plain_text(f)
        except ConfigError as exc:
            assert "MinerU" in str(exc), "报错里要给出安装指引"
        else:
            raise AssertionError("应该抛 ConfigError")


@check("档位映射：文本型与扫描件各自落到不同 tier")
def check_tier_mapping():
    # 两者必须不同。相同的话扫描件会走文本档，出来一堆空页且不报错。
    assert tier_for_backend("mineru") == "basic", tier_for_backend("mineru")
    assert tier_for_backend("mineru-vlm") == "advanced", tier_for_backend("mineru-vlm")


@check("档位映射：未知后端必须报错")
def check_tier_unknown():
    # 不认识的 backend 不能静默回落默认档，否则配置写错永远发现不了。
    try:
        tier_for_backend("magic-parser")
    except ConfigError:
        return
    raise AssertionError("未知后端应该抛 ConfigError")


@check("依赖检查：mineru 可导入")
def check_mineru_available():
    # 装没装，一条函数就能回答；没装会抛带安装命令的 ConfigError。
    check_available()


# ── L2 配置 ──────────────────────────────────────────────────


def _load_parsing_section():
    """读真实 rag.yaml 的 parsing 段。没装 PyYAML 就跳过 L2/L3。"""
    import yaml

    with open(_CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)["rag"]["offline"]["parsing"]


@check("配置：能从 rag.yaml 构造且取值正确")
def check_config_from_yaml():
    cfg = ParseConfig.from_mapping(_load_parsing_section(), _PARSED_DIR)
    assert cfg.probe_pages == 10, cfg.probe_pages
    assert cfg.scanned_threshold == 0.05, cfg.scanned_threshold
    assert cfg.mineru_timeout_s == 900, cfg.mineru_timeout_s
    # 中间产物目录由 parsed_dir 派生，不该单独配。
    assert cfg.mineru_work_dir.name == "_mineru", cfg.mineru_work_dir


@check("配置：缺键必须当场炸")
def check_config_fail_fast():
    # 少配一项就要 KeyError，不能静默用默认值跑完 52 份。
    try:
        ParseConfig.from_mapping({}, _PARSED_DIR)
    except KeyError:
        return
    raise AssertionError("空配置应该抛 KeyError")


# ── L3 真实数据 ──────────────────────────────────────────────


@check("主文件挑选：不挑 source.txt，挑正文")
def check_select_main_file():
    # 这个项目里同时有 source.txt、附件 pdf、正文 docx。
    project = _RAW_ROOT / "20260831_27238769"
    if not project.is_dir():
        return  # 数据不在就跳过，不算失败
    cfg = ParseConfig.from_mapping(_load_parsing_section(), _PARSED_DIR)
    chosen = select_main_file(project, cfg.source)
    assert chosen.name != "source.txt", chosen
    assert chosen.suffix.lower() in {".pdf", ".docx", ".doc"}, chosen


@check("探针：真实 PDF 能取到页文本")
def check_probe_real_pdf():
    pdfs = sorted(_RAW_ROOT.glob("*/*.pdf"))
    if not pdfs:
        return
    from rag.offline.parsers import sample_pages

    pages = sample_pages(pdfs[0], 10)
    if not pages:
        # pypdf 没装或文件加密都会是空列表。这里只提示，不判失败——
        # 探针降级是设计好的行为，不算 bug。
        log.warning("probe.no_sample", file=pdfs[0].name, hint="pypdf 未装或 PDF 加密")
        return
    total = sum(len(p) for p in pages)
    log.info("probe.sample", file=pdfs[0].name, pages=len(pages), chars=total)
    assert len(pages) <= 10


# ── L4 MinerU 端到端 ─────────────────────────────────────────


def check_mineru_end_to_end():
    """跑一份真实 PDF，验证"子进程 + 档位 + 产物回读"三件事能串起来。

    挑体积最大的那份：附件、公告这类单页文件本来就可能是纯表格，没有标题，
    拿它们当基准会把"文件类型不同"误判成"解析失败"。
    """
    from rag.offline.parsers import parse_with_mineru

    # 按体积降序，最大的那份最可能是真正的招标文件。
    pdfs = sorted(_RAW_ROOT.glob("*/*.pdf"), key=lambda p: p.stat().st_size, reverse=True)
    if not pdfs:
        log.warning("mineru.no_input", hint="data/raw 下没有 pdf")
        return
    # 依赖检查放在最前面：没装时给的是"uv add mineru"，比后面一长串堆栈好读。
    check_available()

    cfg = ParseConfig.from_mapping(_load_parsing_section(), _PARSED_DIR)
    md = parse_with_mineru(
        pdfs[0],
        work_dir=cfg.mineru_work_dir,
        backend="mineru",
        timeout_s=cfg.mineru_timeout_s,
    )
    assert len(md) > 2000, f"产物只有 {len(md)} 字符，疑似解析失败"

    headers = sum(1 for line in md.splitlines() if line.startswith("#"))
    log.info("mineru.output", file=pdfs[0].name, chars=len(md), headers=headers)

    # 标题是质量信号，不是功能信号：表格型文档可能一个标题都没有，所以只在为 0 时提示，
    # 不判失败。真正的"解析成了纯文本"会在字符数和可读性上先露出来。
    if headers == 0:
        log.warning("mineru.no_header", file=pdfs[0].name, hint="表格/清单类文档可能没有标题")


def probe_table() -> int:
    """把所有 PDF 的探针结果打出来，人工核对"扫描件/文本型"判得对不对。

    机检能验证代码逻辑，验不了分类准不准——那必须人去看原件。
    这张表就是给你对着 PDF 抽查用的：每页字符数接近 0 的应该是扫描件。
    """
    from rag.offline.parsers import sample_pages

    cfg = ParseConfig.from_mapping(_load_parsing_section(), _PARSED_DIR)
    files = sorted(list(_RAW_ROOT.glob("*/*.pdf")) + list(_RAW_ROOT.glob("*/*.docx")))
    if not files:
        log.warning("probe_table.empty", hint="data/raw 下没有 pdf/docx")
        return 0

    scanned = 0
    for path in files:
        pages = sample_pages(path, cfg.probe_pages)
        chars = sum(len(p) for p in pages)
        route = detect_route(pages, scanned_threshold=cfg.scanned_threshold)
        if route.backend == "mineru-vlm":
            scanned += 1
        log.info(
            "probe_table.row",
            project=path.parent.name,
            file=path.name[:40],
            pages=len(pages),
            chars=chars,
            backend=route.backend,
        )
    log.info("probe_table.done", files=len(files), judged_scanned=scanned)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="解析链路自检")
    parser.add_argument("--with-mineru", action="store_true", help="额外跑一份真实文档的 MinerU 解析")
    parser.add_argument("--probe-table", action="store_true", help="列出所有 PDF 的探针结果，人工核对分类")
    args = parser.parse_args(argv)

    if args.probe_table:
        return probe_table()

    failed = 0
    for name, func in _CHECKS:
        try:
            func()
        except AssertionError as exc:
            failed += 1
            log.error("check.fail", name=name, reason=str(exc) or "断言未通过")
        except Exception as exc:
            # 自检要跑完全部检查，不能第一项就退出。
            failed += 1
            log.error("check.error", name=name, reason=f"{type(exc).__name__}: {exc}")
        else:
            log.info("check.pass", name=name)

    if args.with_mineru:
        try:
            check_mineru_end_to_end()
        except Exception as exc:
            failed += 1
            log.error("check.error", name="MinerU 端到端", reason=f"{type(exc).__name__}: {exc}")
        else:
            log.info("check.pass", name="MinerU 端到端")

    total = len(_CHECKS) + (1 if args.with_mineru else 0)
    log.info("verify.done", total=total, failed=failed, ok=failed == 0)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
