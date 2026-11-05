#!/usr/bin/env python
"""把 data/raw/tenders 下的 zip 解开，摊平到各自的项目目录里。

为什么要先做：52 个有附件的项目里有 14 个的主文件在 zip 里，MinerU 不认压缩包。
不解包，这 14 个项目必然解析失败，日志里看起来像"解析器有问题"，其实是数据没摊开。

摊平（不保留 zip 内部目录）是有意的：pipeline 的 select_main_file 只看项目目录这一层。
中文标书的 zip 通常就是一份招标文件加几个公告，摊平不会丢信息，还能让挑主文件的规则直接复用。

重复跑是安全的：同名同大小的文件跳过，不再写一遍。
"""
import argparse
import shutil
import sys
import zipfile
from pathlib import Path

# 只解析那些能被解析器确认的后缀名。
_TARGET_SUFFIXES = {".pdf",".docx","doc",".txt",".md"}

#压缩包里可能会出现，但是没用的文件
_JUNK_NAMES = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}

# 文件名冲突时的最大重试次数，避免死循环
_MAX_COLLISION = 50

def decode_member_name(info: zipfile.ZipInfo) -> str:
    """
    还原压缩包内的文件名。

    中文 zip 有个老问题：WinRAR/系统自带压缩通常按 GBK 存文件名，但 zip 规范没写编码，
    zipfile 一律按 cp437 解，于是 "招标文件.pdf" 会变成一串乱码。
    能拿到的唯一线索是 flag_bits 的 0x800 位：置位表示"文件名是 UTF-8"，没置位就按 GBK 猜。

    参数：info 压缩包里的一个条目。
    返回：解码后的文件名（可能是带目录的相对路径）。
     """

    # 0x800 位置：压缩时就声明了UTF-8，直接用
    if info.flag_bits & 0x800:
        return info.filename
    # 没声明：先按cp437 还原为原始字节，在按GBK解
    try:
        return info.filename.encode("cp437").decode("gbk")
    except (UnicodeEncodeError,UnicodeDecodeError):
        return info.filename

def is_safe_member(name: str) -> bool:
    """判断解压路径是否安全（防 zip slip）。

    压缩包里的条目名可以是 "../../etc/passwd" 或 "C:\\Windows\\x.pdf"。
    直接拼到目标目录上就会写到目录外面去，这是 zip 解压最经典的漏洞。

    参数：name 条目名。
    返回：True 表示可以解。
    """
    # 统一成 / 分隔再判断，windows的 \ 也要认
    normalized = name.replace("\\","/")
    # 绝对路径、盘符开头、含..的一律拒绝
    if normalized.startswith("/") or ":" in normalized:
        return False
    return ".." not in normalized.split("/")

def target_path_for(dest_dir: Path,name:str) -> Path:
    """给条目算一个落盘路径，冲突时改名。

    只取文件名部分（摊平）。同名时依次试 xxx_2.pdf、xxx_3.pdf。

    参数：dest_dir 项目目录；name 条目名。
    返回：可写的目标路径。
    """
    # 只保留最后一段，丢掉zip内部的目录层级
    base_name = Path(name).name
    # 第一次尝试原名
    candidate = dest_dir / base_name
    # 序号从2开始，最多尝试 _MAX_COLLISION次
    index = 2
    # 已存在就换个名字
    while candidate.exists() and index <= _MAX_COLLISION:
        # 拆出主干和后缀，拼上序号。
        candidate = dest_dir / f"{Path(base_name).stem}_{index}{Path(base_name).suffix}"
        # 序号递增
        index += 1
    return candidate

def unpack_one(zip_path: Path,dest_dir: Path) -> list[Path]:
    """解开一个 zip，返回真正落盘的文件列表。

    参数：zip_path 压缩包；dest_dir 项目目录。
    返回：新写出的文件路径列表（已存在的同名同尺寸文件不计入）。
    """
    #收集结果
    written: list[Path] = []
    # zipfile 打开会在关闭时自动释放句柄
    with zipfile.ZipFile(zip_path) as archive:
        # 逐个条目处理
        for info in archive.infolist():
            # 目录条目没有内容，跳过
            if info.is_dir():
                continue
            # 解码文件名 （GBK问题的唯一出口）
            name = decode_member_name(info)
            # 过滤系统垃圾文件
            if any(part in _JUNK_NAMES for part in name.replace("\\","/").split("/")):
                continue
            # 过滤路径穿越
            if not is_safe_member(name):
                continue

            # 只保留解析器能处理的文件
            if Path(name).suffix.lower() not in _TARGET_SUFFIXES:
                continue
            # 计算目标路径
            target = target_path_for(dest_dir,name)
            # 同名同大小的文件默认已经处理，跳过
            if target.exists() and target.stat().st_size == info.file_size:
                continue
            # 流失写，避免遇到大文件整个读进内存
            with archive.open(info) as src,open(target,"wb") as dst:
                shutil.copyfileobj(src,dst)
            # 追加
            written.append(target)

    # 返回本次新写的文件
    return written

def main(argv: list[str] | None = None) -> int:
    """入口：扫所有项目目录下的 zip，逐个解开。

   返回：0 表示全部成功，1 表示有包失败（加密、损坏）。
   """
    # 脚本在 <repo>/scripts/ 下，上一级是仓库根。
    default_raw = Path(__file__).resolve().parent.parent / "data" / "raw" / "tenders"
    # 参数。
    parser = argparse.ArgumentParser(description="解开 data/raw/tenders 下的 zip")
    parser.add_argument("--raw", default=str(default_raw), help="项目根目录")
    parser.add_argument("--dry-run", action="store_true", help="只列出会解哪些包")
    args = parser.parse_args(argv)

    # 根目录不存在就没法进行。
    raw_root = Path(args.raw)
    if not raw_root.is_dir():
        print(f"目录不存在：{raw_root}", file=sys.stderr)
        return 1

    # 计数器。
    total_files = 0
    failed = 0
    # 每个项目目录单独看。
    for project_dir in sorted(p for p in raw_root.iterdir() if p.is_dir()):
        # 项目下可能有多个 zip。
        for zip_path in sorted(project_dir.glob("*.zip")):
            # 干跑只报计划。
            if args.dry_run:
                print(f"[plan] {project_dir.name} <- {zip_path.name}")
                continue
            # 单个包失败不能中断整批。
            try:
                written = unpack_one(zip_path, project_dir)
            except (zipfile.BadZipFile, RuntimeError) as exc:
                # BadZipFile：文件损坏；RuntimeError：加密包（需要密码）。
                print(f"[fail] {project_dir.name}/{zip_path.name}: {exc}", file=sys.stderr)
                failed += 1
                continue
            # 累加。
            total_files += len(written)
            print(f"[ok] {project_dir.name}/{zip_path.name} -> {len(written)} 个文件")
    # 汇总。
    print(f"完成：新解出 {total_files} 个文件，失败 {failed} 个包")
    # 有失败就返回 1，方便串到批处理里判红。
    return 1 if failed else 0

if __name__ == "__&#8203;main__":
    sys.exit(main())