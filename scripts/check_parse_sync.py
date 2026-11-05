#!/usr/bin/env python
"""R1 对账（docs/rag/R1 §命名坑）—— 口径：一个项目一份主文件。

【这个脚本解决什么问题】
覆盖类 bug 不报错、只安静地少数据。发现它的唯一办法是对账：
拿原始目录和账本（manifest.json）比一遍，对不上就说明有文件没解析、
或者产物被别人覆盖了。

【本版改了什么（为什么必须改）】
旧版把 data/raw 下**每个文件**都当"应登记项"，而账本是按项目编号登记的，
一个项目只登记主文件。20260831_27238769/ 下有 3 个文件 → 旧版永远报 2 条缺失，
真问题被 51 条误报淹掉，等于没有对账。
新口径：按项目目录分组 → 用 pipeline.select_main_file 挑出主文件 → 只跟它比。

【六类问题，两类级别】
    error  （退出码 1）：
        unregistered      项目有主文件但账本没记录（没跑过 / 跑失败了）
        stale             原件哈希变了，账本里的产物已过期（改了文件没重跑）
        artifact_missing  账本说有产物，磁盘上没有（被人删了 / 没同步过来）
        naming_conflict   同一产物名被多个项目登记（safe_stem 撞了，会互相覆盖）
    warning（不影响退出码，但要看）：
        no_main_file      项目目录里挑不出主文件（下载不全 / 只下了 zip）
        raw_dir_missing   账本有记录但原始目录不在了（数据被移走）
        orphan_artifact   产物目录里有账本没记的文件（旧命名 tender_日期.md 遗留）

【为什么 orphan_artifact 一定要查】
仓库里现在躺着一份 tender_20260828.md —— 旧命名方案的产物，
账本里没有它。它不会被任何流程引用，却会让人误以为"这个项目已经解析过了"。
对账把它显式列出来，"安静的历史包袱"就变成了可清理的待办。

【用法】在 review-engine/ 下执行（与 check_env.py 同一套约定）：
    uv run python ../scripts/check_parse_sync.py

【约定】本脚本只读：不写产物、不写账本、不改任何数据文件。
"""

# argparse：命令行参数。
import argparse
# sys：把 review-engine/src 加进模块搜索路径，并用它设置退出码。
import sys
# Path：跨平台路径对象。
from pathlib import Path

# 脚本在 <repo>/scripts/ 下；把它上一级的 review-engine/src 插进 sys.path，
# 否则 import rag / common 都会找不到（项目不把 src 装成包，见 pyproject 的 tool.uv 说明）。
_SRC_DIR = Path(__file__).resolve().parent.parent / "review-engine" / "src"
# 只插一次，且插到最前（优先于可能存在的同名第三方包）。
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# 全项目唯一日志入口：对账结果要能被 Alloy/Loki 采集并告警，不能用裸 print。
from common.logging import get_logger
# 账本对象：自带损坏检测（_read_disk 会留诊断副本抛明确异常）与字段校验。
from rag.offline.manifest import Manifest
# 复用流水线的同一套口径：任务扫描、配置加载、主文件选择、哈希计算。
# 【为什么不在这里各写一份】主文件选择规则一旦两处实现，迟早会不一致：
# 流水线挑了 A 文件登记，对账按 B 文件比对，永远对不上。
from rag.offline.pipeline import (
    ParseTask,
    build_tasks,
    load_config,
    select_main_file,
    sha256_file,
)

# logger 命名空间用脚本名，日志里能区分是谁打的。
log = get_logger(__name__)

# ── 默认路径（脚本在 <repo>/scripts/ 下，parent.parent 即仓库根）──────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
# 原始标书根目录：每个子目录 = 一个项目。
_DEFAULT_RAW_ROOT = _REPO_ROOT / "data" / "raw" / "tenders"
# 产物目录。
_DEFAULT_PARSED_DIR = _REPO_ROOT / "data" / "parsed"
# 账本。
_DEFAULT_MANIFEST = _DEFAULT_PARSED_DIR / "manifest.json"
# 配置文件。
_DEFAULT_CONFIG = _REPO_ROOT / "review-engine" / "src" / "config" / "rag.yaml"


def _issue(kind: str, severity: str, subject: str, detail: str) -> dict[str, str]:
    """构造一条问题记录。

    【为什么问题不直接打日志、而要先收集成结构】
    收集完才能汇总"error 几条、warning 几条"再决定退出码；
    边查边打则退出码要靠全局变量累加，逻辑分散、没法单测。

    【参数】
        kind     ：问题类型（unregistered / stale / ...），进日志的 kind 字段，便于分组查询。
        severity ：error | warning。
        subject  ：主体标识（通常是项目编号，产物类问题则是文件主干）。
        detail   ：人读说明（含具体文件名/哈希前缀）。
    【返回值】字典。
    """
    # 四个键固定：日志字段集稳定，Loki 里才能用同一条查询拉全。
    return {"kind": kind, "severity": severity, "subject": subject, "detail": detail}


def _open_ledger(manifest_path: Path) -> Manifest | None:
    """打开账本；文件不存在时返回空账本，损坏时返回 None。

    【三种情况必须分开处理】
    1. 账本不存在：不是"损坏"，是"还没跑过"→ 给空账本，让对账把全部项目列成 unregistered，
       这比直接退出有用：它给出了一份"待解析清单"。
    2. 账本损坏：Manifest 构造时会把原文件复制成 .corrupt-<ts>.json 再抛 ValueError。
       此时继续对账会得出"全部未登记"的错误结论，必须停下让人看。
    3. 正常：直接用。

    【参数】manifest_path —— manifest.json 路径。
    【返回值】Manifest 对象；损坏时 None。
    """
    # 账本不存在：Manifest 构造函数见文件不存在不会 load，得到空账本。
    if not manifest_path.exists():
        # 记 warning 而不是 error：第一天上手就是这个状态，属于预期内。
        log.warning(
            "parse_sync.manifest_absent",
            manifest=str(manifest_path),
            hint="先跑批量解析：PYTHONPATH=src uv run python -m rag.offline.pipeline",
        )
        # 空账本：entries 为空字典。
        return Manifest(manifest_path)
    # 存在就用它；损坏时 ValueError 由下面捕获。
    try:
        # 构造即加载（构造函数里 if path.exists(): self.load()）。
        return Manifest(manifest_path)
    except ValueError as exc:
        # 账本损坏：诊断副本已被 manifest.py 复制好，这里只负责报清楚并止血。
        log.error("parse_sync.manifest_corrupt", manifest=str(manifest_path), reason=str(exc))
        # None 表示"别继续对账了"，由 main 转成退出码 1。
        return None


def check_projects(tasks: list[ParseTask], cfg, ledger: Manifest) -> list[dict[str, str]]:
    """逐项目比对主文件与账本。

    【功能实现原理（每个项目四步）】
    1. 挑主文件（挑不出 → no_main_file，warning）；
    2. 算主文件哈希（幂等键，与流水线登记时用的是同一个函数）；
    3. 账本查这个项目（查不到 → unregistered，error）；
    4. 哈希比对（不一致 → stale）+ 产物存在性（不存在 → artifact_missing）。
    第 3、4 步刻意用独立 if 而不是 elif：一份文档可能"既改了原件、产物又被人删了"，
    两条都报出来，人一次就能修完；只报第一条会导致修一次跑一次。

    【参数】tasks —— 项目任务列表；cfg —— 配置（取 source 策略与 parsed_dir）；ledger —— 账本。
    【返回值】问题记录列表。
    """
    # 收集器。
    issues: list[dict[str, str]] = []
    # 逐项目检查。
    for task in tasks:
        # 挑主文件：与流水线完全同一套规则（同一函数），不会出现口径分歧。
        try:
            main_file = select_main_file(task, cfg.source)
        except FileNotFoundError as exc:
            # 挑不出主文件：不是覆盖问题，是数据不全，warning 足够（流水线也会跳过它）。
            issues.append(_issue("no_main_file", "warning", task.project_id, str(exc)))
            # 本项目的其余检查无从谈起，直接下一个。
            continue
        # 算当前主文件哈希。
        raw_sha256 = sha256_file(main_file)
        # 查账本记录（dict.get 语义：没有返回 None）。
        entry = ledger.get(task.project_id)
        # 没登记：要么没跑过，要么上次失败没写账本（失败不登记是 pipeline 的刻意设计）。
        if entry is None:
            issues.append(
                _issue("unregistered", "error", task.project_id, f"主文件 {main_file.name} 未登记")
            )
            # 没记录就没有哈希/产物可继续比，下一个项目。
            continue
        # 哈希不一致：原件被换过（重新下载/换了版本），旧产物已经对不上原文。
        if entry.raw_sha256 != raw_sha256:
            issues.append(
                _issue(
                    "stale",
                    "error",
                    task.project_id,
                    f"原件已变（账本 {entry.raw_sha256[:12]}… / 当前 {raw_sha256[:12]}…）",
                )
            )
        # 产物文件不存在：账本声称解析过，磁盘上却没有 —— 最危险的一类不一致。
        if not (cfg.parsed_dir / entry.parsed_path).is_file():
            issues.append(
                _issue(
                    "artifact_missing",
                    "error",
                    task.project_id,
                    f"账本记录产物 {entry.parsed_path} 在 {cfg.parsed_dir} 下不存在",
                )
            )
    # 返回全部问题。
    return issues


def check_orphan_entries(tasks: list[ParseTask], ledger: Manifest) -> list[dict[str, str]]:
    """找出"账本有记录、但原始目录已不存在"的项目。

    【为什么要查】数据目录被人手工挪走/清理掉之后，账本会留一堆死记录。
    死记录本身无害，但它会让"已解析项目数"这个指标虚高，
    人看着 52 条登记以为都齐了，实际磁盘上只剩 40 个原始目录。

    【参数】tasks —— 现存项目任务；ledger —— 账本。
    【返回值】问题记录列表（warning）。
    """
    # 现存项目编号集合：用 set 做 O(1) 成员判断，避免 52×52 的双层循环。
    present = {task.project_id for task in tasks}
    # 账本里有、现值里没有 → 死记录。
    return [
        _issue("raw_dir_missing", "warning", project_id, "账本有记录但原始目录已不存在")
        for project_id in ledger.entries
        if project_id not in present
    ]


def check_orphan_artifacts(cfg, ledger: Manifest) -> list[dict[str, str]]:
    """找出产物目录里"没有账本记录"的 markdown 文件。

    【为什么要查】旧命名方案产出的 tender_20260828.md 就躺在这里：
    它不会被任何流程引用，却会让人误判"这个项目已经解析过了"。
    把它显式列成 warning，历史包袱就从"安静存在"变成"可清理的待办"。

    【参数】cfg —— 配置（取 parsed_dir）；ledger —— 账本。
    【返回值】问题记录列表（warning）。
    """
    # 目录不存在（还没跑过任何解析）：没有产物，也就没有孤儿，直接返回空。
    if not cfg.parsed_dir.is_dir():
        return []
    # 账本登记过的产物文件名集合（账本里存的是相对 parsed_dir 的文件名）。
    registered = {Path(entry.parsed_path).name for entry in ledger.entries.values()}
    # 逐个扫产物文件，只关心 .md（.gitkeep / .tmp / .lock 都不算产物）。
    return [
        _issue("orphan_artifact", "warning", path.stem, f"产物 {path.name} 无账本记录（旧命名遗留或手工拷贝）")
        for path in sorted(cfg.parsed_dir.glob("*.md"))
        if path.name not in registered
    ]


def check_naming_conflicts(ledger: Manifest) -> list[dict[str, str]]:
    """找出"同一产物名被多个项目登记"的命名冲突。

    【为什么还有这个检查】新命名是 <项目编号>.md，理论上不会撞；
    但 safe_stem 会把非法字符替换成下划线，两个不同编号有可能撞成同一个名字
    （如 "2026/0828" 与 "2026:0828"）。撞了就是后跑的覆盖先跑的，且不报错。

    【参数】ledger —— 账本。
    【返回值】问题记录列表（error）。
    """
    # 产物名 → 占有它的项目编号列表。
    owners: dict[str, list[str]] = {}
    # 逐条登记记录归组。
    for project_id, entry in ledger.entries.items():
        # setdefault：键不存在先建空列表，再追加，等价于"分组"。
        owners.setdefault(entry.parsed_path, []).append(project_id)
    # 只保留被 2 个及以上项目共用的产物名。
    return [
        _issue("naming_conflict", "error", project_ids[0], f"{parsed_path} 被多个项目共用：{project_ids}")
        for parsed_path, project_ids in owners.items()
        if len(project_ids) > 1
    ]


def _log_issue(issue: dict[str, str]) -> None:
    """把一条问题打成结构化日志。

    【为什么用固定事件名 parse_sync.issue + kind 字段】
    事件名固定，Loki 里一条查询就能拉全部对账问题；
    若按 kind 拼事件名（parse_sync.unregistered、parse_sync.stale…），
    新增一类问题就得改告警规则。分类信息放字段里，聚合靠 group by kind。

    【参数】issue —— _issue 构造的记录。
    """
    # 统一字段集。
    payload = {"kind": issue["kind"], "subject": issue["subject"], "detail": issue["detail"]}
    # error 级进 error 流（可触发告警），warning 级进 warning 流。
    if issue["severity"] == "error":
        log.error("parse_sync.issue", **payload)
    else:
        log.warning("parse_sync.issue", **payload)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数（全部有默认值，直接跑就行）。

    【参数】argv —— 参数列表（默认取 sys.argv[1:]，单测可传假参数）。
    【返回值】argparse.Namespace。
    """
    # 构造解析器。
    parser = argparse.ArgumentParser(description="R1 raw↔parsed 对账（按项目主文件口径）")
    # 原始根目录。
    parser.add_argument("--raw", default=str(_DEFAULT_RAW_ROOT), help="原始根目录（每个子目录=一个项目）")
    # 产物目录。
    parser.add_argument("--parsed", default=str(_DEFAULT_PARSED_DIR), help="解析产物目录")
    # 账本。
    parser.add_argument("--manifest", default=str(_DEFAULT_MANIFEST), help="manifest.json 路径")
    # 配置（主文件选择策略要从这里读）。
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG), help="rag.yaml 路径")
    # 返回解析结果。
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """主流程：读配置 → 扫项目 → 四类检查 → 打日志 → 返回退出码。

    【返回值】0 = 零 error；1 = 有 error（或账本不可用）。warning 不影响退出码。

    【为什么 warning 不算失败】
    对账脚本会被挂进 cron。若 no_main_file 这种"数据不全但流程能跑"的情况也返回 1，
    那么这个任务每天都是红的，人就再也不看它了 —— 告警疲劳比没有告警更危险。
    """
    # 解析参数。
    args = parse_args(argv)
    # 读配置：主文件选择策略、产物目录都从配置/参数来，脚本里不写死。
    cfg = load_config(Path(args.config), Path(args.parsed))
    # 扫项目（与流水线同一个函数，保证两侧看到的是同一批项目）。
    tasks = build_tasks(Path(args.raw))
    # 开局日志：把这次比对的范围钉住，日后能回答"那天到底比了什么"。
    log.info(
        "parse_sync.start",
        raw_root=args.raw,
        parsed_dir=args.parsed,
        manifest=args.manifest,
        project_count=len(tasks),
    )
    # 打开账本（不存在 → 空账本；损坏 → None）。
    ledger = _open_ledger(Path(args.manifest))
    # 账本损坏：不再继续（继续只会得出错误结论），直接判失败。
    if ledger is None:
        log.error("parse_sync.aborted", reason="manifest 不可用，无法对账")
        return 1
    # 四类检查合并（+= 把列表拼接，保持顺序：项目级在前，产物级在后）。
    issues: list[dict[str, str]] = []
    issues += check_projects(tasks, cfg, ledger)
    issues += check_orphan_entries(tasks, ledger)
    issues += check_orphan_artifacts(cfg, ledger)
    issues += check_naming_conflicts(ledger)
    # 逐条输出。
    for issue in issues:
        _log_issue(issue)
    # 分级计数。
    error_count = sum(1 for issue in issues if issue["severity"] == "error")
    # warning 数 = 总数 - error 数。
    warning_count = len(issues) - error_count
    # 收尾汇总：五个数字，告警规则直接读 ok / error_count。
    log.info(
        "parse_sync.done",
        ok=error_count == 0,
        project_count=len(tasks),
        registered_count=len(ledger.entries),
        error_count=error_count,
        warning_count=warning_count,
    )
    # 有 error 返回 1（cron 判红），否则 0。
    return 1 if error_count else 0


# 直接运行时执行；被 import（单测）时不执行。
if __name__ == "__&#8203;main__":
    # 退出码交给 shell。
    sys.exit(main())
