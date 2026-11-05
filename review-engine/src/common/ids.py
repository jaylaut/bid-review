"""
ID 与哈希。全项目只有这一处算哈希，别处一律 import 这里。

同一份文件在 pipeline、对账脚本、入库三处都要算 sha256，
各自实现一份的结果是：某天给哈希加个"跳过末页"的规则，只有一处改了，
另外两处继续用旧口径，账本从此对不上。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

#流式读盘的块大小。1MB属于合适的粒度。
_CHUNK = 1024 * 1024

def sha256_text(text: str) -> str:
    '''
    字符串的sha256，返回64位 hex。
    '''
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def sha256_file(path: str | Path) -> str:
    '''
    文件的sha256，流式读取，内存占用固定为一块。
    用二进制模式读取。如果使用文本模式，会把 CRLF 转成 LF，
    导致同一个文件在两台机器上算出的哈希值不一致，幂等键失效。
    LF -- 换行
    CR -- 回车
    CRLF -- 回车 + 换行
    '''

    # 创建一个 sha256 哈希器对象。它是增量的，可以一次次 update，最后一次性出结果
    hasher = hashlib.sha256()
    # 以"二进制读"模式打开文件；with 语句保证读完成或出错都会自动关闭文件句柄，不泄漏
    # rb 不做任何换行/编码翻译，读到的是磁盘上的真实字节，跨平台哈希才稳定
    with open(path,"rb") as f:
        # 循环分块读取，避免一次性把整个大文件加载进内存
        while True:
            # 每次最多读 _CHUNK(1MB) 字节；f.read 返回 bytes
            block = f.read(_CHUNK)
            # 读到空 bytes(b"") 表示已到文件末尾，跳出循环
            # 注意：空文件第一次 read 就是 b""，循环一次都不进，最终返回空串的 sha256
            if not block:
                break
                # 把这一段字节喂给哈希器；哈希器内部维护状态，不保留原始数据
            hasher.update(block)
    # 全部块喂完后，hexdigest() 输出 64 位十六进制字符串形式的哈希值
    return hasher.hexdigest()

def digest(obj: Any,length: int = 8) -> str:
    '''
    任意可JSON 化对象的短摘要，用于 “这两个调用参数是否相同”的比对。
    sort_keys 是关键：Pydantic的dump顺序稳定，但手写的dict不保证。
    键序不同就算成不同参数的话，防打转检测会失效——模型拿到同样的参数反复调用，每次都被当成新的调用，导致超时。
    '''
    #这行是把任意 Python 对象序列化成 JSON 字符串，为下一步算哈希做准备。
    # 目的是把对象变成一段稳定、规范的文本——内容相同的对象必须得到一字不差的同一段字符串，
    # 这样 sha256_text(payload)[:length] 出来的短摘要才能用来比对"这两次调用参数是否相同"
    payload = json.dumps(obj,sort_keys=True,ensure_ascii=False,default=repr)
    return sha256_text(payload)[:length]

def new_id(prefix: str) -> str:
    """生成带前缀的短 ID，形如 task_3f9a1c2b7d4e。

    用 uuid4 的前 12 位 hex：48 bit 空间，单进程内一千个/秒也不会撞。
    用 uuid4 而不是自增：多进程、多机器并发时不依赖任何中心分配。
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


