"""MinerU 解析子进程入口。

为什么单独起一个进程，而不是在主进程里直接调 SDK：

1. 超时可控。SDK 自身没有超时参数，一份损坏的 PDF 能让它卡几十分钟。
   放在子进程里，父进程 subprocess.run(timeout=...) 到点就能 kill。
2. 内存隔离。解析峰值要吃几 GB，子进程退出即释放，批量跑 50 份不会越跑越涨。
3. 崩了不连坐。某个文件把原生库搞崩（DirectML / pypdfium2 都有过），
   主进程只会收到一个非 0 返回码，接着跑下一份。

命令行协议（由 rag.offline.parsers.mineru.parse_with_mineru 调用）：
    python -m rag.offline.parsers.mineru_worker <源文件> <tier> <输出 md 路径>

约定：成功时退出码 0、markdown 写到指定路径；失败时退出码非 0、traceback 打到 stderr。
"""
from __future__ import annotations

import sys
from pathlib import Path

# Windows 上 multiprocessing 用 spawn，MinerU 内部的渲染 worker 会重新 import 主模块。
# 所有实际逻辑必须放在 __main__ 保护里，否则子进程会递归再跑一遍解析，直接崩在启动阶段。
# 这一点是真踩过的坑：模块顶层写 parse 调用，报的是 "PDF render worker exited: 1"，
# 真实原因却是渲染子进程把主模块当脚本又执行了一次。


def main(argv: list[str]) -> int:
    """解析一份文件，把 markdown 写到指定路径。

    argv 形如 [模块名, 源文件, tier, 输出路径]，由父进程拼好。
    """
    if len(argv) != 4:
        print("用法: python -m rag.offline.parsers.mineru_worker <源文件> <tier> <输出md>", file=sys.stderr)
        return 2

    source = Path(argv[1])
    tier = argv[2]
    target = Path(argv[3])

    # 没见过的 tier 直接拒掉，避免写错字符串被 MinerU 当成默认值静默跑。
    if tier not in {"flash", "basic", "standard", "advanced"}:
        print(f"未知 tier: {tier}", file=sys.stderr)
        return 2

    if not source.is_file():
        print(f"源文件不存在: {source}", file=sys.stderr)
        return 2

    # 延迟导入：让 --help / 参数校验这种轻量路径不必加载几百 MB 的依赖。
    import mineru

    # tier 是 Literal 类型，运行时只是个字符串，这里显式标注让类型检查器闭嘴。
    result = mineru.parse(source, tier=tier)  # type: ignore[arg-type]
    markdown = result.markdown()

    # 父进程会先建好目录，这里兜一层，方便单独手工调用。
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")

    # 进度打到 stdout，父进程会原样收进日志。
    print(f"OK pages={len(result.pages)} chars={len(markdown)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
