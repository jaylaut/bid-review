# 导入标准库：hashlib 提供 sha256 哈希算法
import hashlib


def build_embedding_ver(model, dim, version, distance, instruction):
    """根据嵌入方案各要素，拼出唯一版本号字符串"""
    # 把查询指令文本用 utf-8 编码成字节串，供哈希使用
    instr_bytes = instruction.encode("utf-8")
    # 对指令做 sha256 哈希，得到 64 位十六进制串
    full_hash = hashlib.sha256(instr_bytes).hexdigest()
    # 只取前 12 位，足够区分且便于阅读
    instr_hash = full_hash[:12]
    # 用 @ 连接各要素，组成形如 qwen3-emb-0.6b@1024@v1@ip@a1b2c3d4e5f6 的版本号
    ver = f"{model}@{dim}@{version}@{distance}@{instr_hash}"
    # 返回拼好的版本号
    return ver