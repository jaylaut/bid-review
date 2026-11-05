# -*- coding: utf-8 -*-
"""主文件的挑选规则。

标书目录里通常不止一个文件：主文件、采购公告、承诺函、代理合同、附件清单，
还有下载脚本留下的 source.txt。账本按项目编号登记，一个项目只能有一条记录，
所以流水线上来第一件事就是确定"解析谁"。

这个模块只做判断，不碰任何 IO 写操作，给个目录就能单测。
"""
from __future__ import annotations

from collections.abc import Mapping     # 导入 Mapping 抽象接口：用作"只读字典"的参数类型，比 dict 更宽（yaml 加载结果、普通 dict 都兼容）
from dataclasses import dataclass       # 导入 @dataclass 装饰器：用类属性声明自动生成 __init__/__eq__/__repr__ 等样板代码
from pathlib import Path              # 导入 Path 类：面向对象的路径操作，比 os.path 更强大更易用
from typing import Any                 # 导入 Any 类型：表示任意类型，比 object 更宽（所有类型都兼容）


# 把下面的类变成"不可变数据类"：frozen=True 让实例创建后字段不可改写，
# 防止策略在批量处理过程中被某处意外篡改，保证同批文档用同一套规则、结果可复现
@dataclass(frozen=True)
class SourcePolicy:
    '''
    挑选策略。策略会在批量过程中被反复读，如果中途被改写，同一批里前后两份文档用的就不是同一套规则，出问题没法复现
    '''
    suffixes: tuple[str, ...]              # 字段①：允许当主文件的扩展名白名单，如 (".pdf", ".docx")；用 tuple 而非 list 也是为了配合 frozen 不可变
    exclude_names: tuple[str, ...]         # 字段②：要排除的文件名黑名单（已统一小写），如 source.txt 这类下载残留
    main_keywords: tuple[str, ...]          # 字段③：命中即可基本确认是主文件的关键词，如 招标/磋商/采购文件；第 1 排序档
    aux_keywords: tuple[str, ...]           # 命中就往下降权，不排除。真实数据里存在正本叫"附件：xxx采购文件"的情况

    @classmethod             # 声明为类方法：第一个参数是类本身（cls），常用于"工厂方法"——从别的数据构造实例
    def from_mapping(cls,cfg: Mapping[str,Any]) -> SourcePolicy:
        '''
        从yaml的source段构造。
        '''
        return cls(               # cls(...) 即 SourcePolicy(...)，用工厂方法而不是直接类名是为了子类继承时仍构造正确类型
            # 后缀和文件名统一小写
            #  # str(s) 包一层是防御：yaml 可能把 ".pdf" 解析成别的类型（如无引号的数字项），转 str 保证 .lower() 一定可用
            suffixes=tuple(str(s).lower() for s in cfg["suffixes"]),                 # 把 yaml 里的 suffixes 列表逐项转字符串再转小写，冻结成 tuple
            exclude_names=tuple(str(n).lower() for n in cfg["exclude_names"]),       # 同上：排除名单统一小写（文件名比较时大小写不敏感才稳定）
            main_keywords=tuple(str(k).lower() for k in cfg["main_keywords"]),      # 同上：主关键词统一小写；中文关键词 .lower() 无副作用，安全
            aux_keywords=tuple(str(k).lower() for k in cfg["aux_keywords"]),         # 辅助关键词统一小写
        )

def _rank(path: Path,policy: SourcePolicy) -> tuple[int,int,int]:
    '''
    候选文件的排序，越小越优先。
    三档依次比较，顺序不能换：
    1. 名字里有"招标文件/磋商文件"这类词，几乎可以确定；
    2. 命中辅助词（公告/承诺函/合同/附件）的往下压；
    3. 同档内比体积。公告通常几百 KB，主文件常见几 MB 到几十 MB。
    '''
    name = path.name         # 取文件名（不含目录部分），如 "XX招标文件.docx"
    main_hit = any(keyword in name for keyword in policy.main_keywords)     # 第 1 档判断：文件名里是否包含任意一个主关键词；any 短路求值，命中即 True
    aux_hit = any(keyword in name for keyword in policy.aux_keywords)     # 第 2 档判断：是否命中任意一个辅助关键词

    # 取负值：体积大的排在前面
    main_key = 0 if main_hit else 1         # 排序键①：命中主关键词 → 0（排最前），没命中 → 1（靠后）
    aux_key = 1 if aux_hit else 0            # 排序键②：命中辅助词 → 1（降权靠后），没命中 → 0（保持原位）
    size_key = -path.stat().st_size         # 排序键③：文件字节数取负——元组比较升序时，负值越小=原体积越大=排越前，实现"同档比大小、大者优先"
    # path.stat() 是文件系统调用，每个候选都会真实读一次 st_size
    return (main_key, aux_key, size_key)  # 返回三元组作为排序键：tuple 比较按元素从左到右依次比，天然形成"先看档①，平级看档②，再平级看档③"的三档优先级

def select_main_file(raw_dir: Path, policy: SourcePolicy) -> Path:
    '''
    从项目的目录中挑选出唯一的主文件。
    '''
    candidates: list[Path] = []                         # 候选文件列表：先粗筛出所有"有资格当主文件"的路径，类型注解写法
    for entry in sorted(raw_dir.iterdir()):             # 遍历目录下所有条目；iterdir() 返回顺序不保证，先 sorted 固定成字典序，
                                                        # 保证排序键完全相等时（如同名同体积）最终选谁也是确定的、可复现
        if not entry.is_file():                         # 粗筛①：跳过子目录等非文件条目（只处理普通文件）
            continue                                    # 不是文件 → 进入下一轮循环
        if entry.name.lower() in policy.exclude_names:  # 粗筛②：文件名（转小写）落在黑名单里？与 exclude_names 的小写形式比较
            continue                                    # 命中黑名单（如 source.txt）→ 直接淘汰

        if entry.suffix.lower() not in policy.suffixes: # 粗筛③：扩展名（转小写）不在允许列表里？如 .jpg 这种解析不了的后缀
            continue                                    # 后缀不允许 → 淘汰
        candidates.append(entry)                        # 三关都通过：进入候选集
    if not candidates:                                  # 一个候选都没有 → 目录内容不符合预期
        raise FileNotFoundError(f"{raw_dir} 下没有可解析的文件")   # 抛"找不到文件"，把原始目录写进报错信息方便排查

    return min(candidates, key=lambda p: _rank(p, policy))   # 从候选集里取排序键"最小"的那个：
                                                              # min 按 key 返回元组逐元素比较的结果，等价于按三档优先级选出唯一主文件
                                                              # key=lambda p: _rank(p, policy) 把每个候选 Path 换成它的三元组排序键