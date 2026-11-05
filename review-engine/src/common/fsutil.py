# -*- coding: utf-8 -*-
"""文件落盘的原子写。

直接 open(path, "w") 会先把目标截断成 0 字节再写。写到一半进程被 kill 或断电，
磁盘上就留半截内容：账本变半截 JSON，产物变半截 markdown。
两者都不会报错，只是下次读的时候解析失败或者少几章——最难查的那类故障。

做法固定为三步：同目录临时文件 → fsync → os.replace。
临时文件必须同目录，os.replace 跨文件系统会抛 EXDEV，不做原子替换。
"""
from __future__ import annotations

import os
import tempfile                     # 导入临时文件工具：用到 tempfile.mkstemp（安全地创建唯一命名的临时文件）
from contextlib import suppress     # 导入 suppress 上下文管理器：忽略指定异常（用于删临时文件时容忍"文件已不存在"）
from pathlib import Path

_RETRIES = 3                        # 模块级常量：os.replace 失败时的最大重试次数（只在 PermissionError 时重试）
_BACKOFF = 0.05                      # 模块级常量：重试退避基数（秒）。指数退避：0.05s、0.1s、0.2s……

def _replace(tmp_name: str,path: Path) -> None:
    '''
    带重试的os.replace
    最后一次失败就如实抛出。重试3次仍失败说明不是瞬时问题
    '''
    for attempt in range(_RETRIES):
        try:
            '''
              核心一步：把临时文件原子重命名为目标路径。
              POSIX 上 rename 是原子的；Windows 上 os.replace 等价于
              MoveFileEx(MOVEFILE_REPLACE_EXISTING)，目标已存在也会被直接替换，
              且同目录内同样保证原子性。从这一刻起，path 要么是完整旧内容、要么是完整新内容
            '''
            os.replace(tmp_name,path)
            return
        except PermissionError:
            # 只捕 PermissionError：Windows 上若目标文件正被其它进程占用（杀毒扫描、编辑器打开、另一 worker 在读），
            # 重命名会瞬时失败，等一会儿再试大概率能成
            if attempt == _RETRIES - 1: # 如果这是最后一次尝试
                raise                    # 不再吞掉，把异常原样抛给调用方——重试 3 次仍失败说明不是瞬时问题
            import time                  # 只在需要等待时才导入 time（延迟导入，成功路径不付出导入成本）

            time.sleep(_BACKOFF * (2 ** attempt))    # 指数退避后重试：第 0 次失败等 0.05s，第 1 次等 0.1s，第 2 次等 0.2s

def write_text_atomic(path: Path,text: str) -> None:
    '''
    把text 原子 写到 path。统一用LF 换行。
    '''
    # 确保目标文件的父目录存在：parents=True 允许创建多级目录，
    # exist_ok=True 目录已存在也不报错。避免后面写入时因目录缺失而失败
    path.parent.mkdir(parents=True,exist_ok=True)

    # mkstemp 自带随机后缀，多进程同时写同一目录不会重名
    # 在 path 所在目录（dir=...，保证同目录，后续 os.replace 才能原子替换）创建一个临时文件：
    #   prefix=".文件名." → 临时文件形如 .ledger.json.ab12cd34.tmp，以 . 开头不污染目录列表，且能看出是谁的临时文件
    #   返回值：fd 是已打开文件的"裸文件描述符"（整数句柄），tmp_name 是临时文件的路径字符串
    fd,tmp_name = tempfile.mkstemp(dir=str(path.parent),prefix=f".{path.name}.",suffix=".tmp")
    try:
        # os.fdopen 把裸fd包成文件对象，这样with退出时会正确关闭。
        # 用文件对象而不是直接 os.write(fd, ...)，是为了：① with 语法自动关闭；② 能指定 encoding 和 newline
        #   "w"：文本写模式
        #   encoding="utf-8"：统一按 UTF-8 编码落盘
        #   newline="\n"：关键！禁止 Python 把 "\n" 翻译成系统默认换行（Windows 是 "\r\n"），
        #   保证本文件写出的文本在所有平台上都是统一 LF 换行
        with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as f:
            f.write(text)            # 把整段文本写入临时文件（此刻还写的是操作系统的页缓存，未必已到磁盘）
            f.flush()                # 把 Python 层缓冲区的数据交给操作系统——flush 只代表"交给 OS"，不代表已落盘
            # 真正的落盘指令：命令操作系统把该文件描述符对应的数据+元数据强制刷到磁盘。
            # 没有这一步，断电/系统崩溃时即使 write 成功磁盘上仍可能是半截。
            # 这是三步 atomic write 中"fsync"那一步
            os.fsync(f.fileno())
        # with 正常退出后：临时文件已完整写到磁盘，且文件名全局唯一
        # 原子替换：把已完整落盘的临时文件一步换成目标文件名。
        # 这一步之后 path 的内容 100% 是完整的；这一步之前 path 一直是旧内容。
        # 任何时刻崩溃，磁盘上都不会存在"半截的 path"
        _replace(tmp_name,path)
    except BaseException:
        # 兜底清理：捕获所有异常（BaseException 连 KeyboardInterrupt、SystemExit 也抓，
        # 保证人为中断 Ctrl+C 时临时文件也不残留），此时替换还没发生，目标文件仍是完整旧内容
        with suppress(FileNotFoundError):
            # 删除半成品临时文件；suppress(FileNotFoundError) 是因为万一 os.replace 其实已成功
            # 只是随后抛异常，临时文件已不存在，unlink 会报"找不到文件"——这种可以安全忽略
            os.unlink(tmp_name)      # 删除临时文件（unlink = 删除一个文件名/硬链接）
        raise