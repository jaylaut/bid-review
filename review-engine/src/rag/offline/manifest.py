# -*- coding: utf-8 -*-
"""
R1 解析产物登记（manifest）（docs/rag/R1-解析与清洗.md §命名坑）。

【这个模块解决什么问题】
解析产物如果按 `tender_{日期}.md` 命名，同一天有多个项目时，
后跑的项目会把先跑的项目产物覆盖掉。覆盖类 bug 最可怕的地方在于：
它不报错，只是安静地少数据。

【这个模块怎么做】
维护一份 manifest.json 登记表，记录"哪个项目编号 → 解析成了哪个文件"。
项目编号天然唯一、可追溯，用完整项目编号做命名键就不会互相覆盖。

【本版修补的三件事：持久化的时机、原子性、并发】
1. 落盘时机：upsert 默认自动落盘。漏掉 save() 时，日志看着成功了、进程一退出记录全丢，
   这是账本类模块最常见的不对称坑。批量场景用 `with m.batch():` 攒批，退出时只写一次，
   避免"每登记一条就全量重写整个文件"的 O(n²) 写放大。
2. 写入原子性：原来 open(path,"w") 会先把文件截断，写到一半被 kill 就留下半截 JSON，
   下次 load() 直接 JSONDecodeError——整个账本读不出来，比没有账本更糟。
   改为"写临时文件 → fsync → os.replace 原子替换"，读者永远只会看到旧版本或新版本。
3. 并发：两个解析进程同时 load→改→save，后写覆盖先写，正是它要防的覆盖问题换了个层级重演。
   改为文件锁 + "合并式落盘"：落盘前在锁内重读磁盘最新状态，只覆盖本方持有的项目编号，
   其他进程写的条目原样保留。

【约定】本文件只负责 manifest 的读/写/增删改查，不做实际解析。
       manifest 是数据管道的"账本"，没有它，谁覆盖了谁都查不出来。
"""
# 导入标准库。
# json：读写 JSON 文件（manifest.json 就是 JSON 格式）。
# os：做原子替换（os.replace）和 fsync（确保数据真的落盘）。
# shutil：账本损坏时复制一份诊断副本。
# tempfile：创建临时文件，用来实现"先写临时文件再替换"的原子写。
# time：替换失败时的指数退避等待，以及生成损坏副本的时间戳。
import json
import os
import shutil
import tempfile
import time
# contextmanager：把"进入/退出"的成对操作写成一个 with 语句可用的函数（batch 与加锁都用它）。
# suppress：忽略指定的异常，这里用来"删临时文件失败就算了，别掩盖真正的错"。
from contextlib import contextmanager, suppress
# dataclass：自动生成数据类的样板方法；asdict：把数据类对象转回普通字典。
from dataclasses import asdict, dataclass
# Path：跨平台的路径对象，比手拼字符串路径更安全。
from pathlib import Path

# 第三方库：跨平台文件锁（Windows 走 msvcrt、Linux 走 fcntl，差异由它抹平）。
# 需要先加进 pyproject.toml：`uv add filelock`。
from filelock import FileLock

# os.replace 在 Windows 上可能被外部句柄占用而失败，重试次数与退避基数。
# 为什么需要重试：Windows 上杀软扫描、编辑器打开、Explorer 预览缩略图都会短暂持有文件句柄，
# 此时替换会抛 PermissionError(WinError 32)。这类占用通常几十毫秒就释放。
_REPLACE_RETRIES = 3         # 最多尝试 3 次
_REPLACE_BACKOFF = 0.05      # 首次退避 50ms，之后翻倍：50 → 100 → 200

@dataclass
class ManifestEntry:
    """
    manifest 里的一条记录，对应"一个项目的一次解析"。

    用 @dataclass 修饰后，Python 自动帮我们生成 __init__/__repr__/__eq__，
    这里只需要像填表一样声明"有哪些字段、各是什么类型"。

    字段说明（对齐 R1 文档 §命名坑 第 2 种方案）：
        project_id: str     项目编号（命名键，天然唯一、可追溯）。
        raw_sha256: str     原始文件的 SHA256 哈希（幂等键，判断原始文件是否变过）。
        parsed_path: str    解析产物文件的相对路径。
        parse_backend: str  用的哪个解析后端（mineru 或 mineru-vlm）。
        quality_score: int  质量门打的分（0~8）。
        ts: str             登记时间戳（ISO 格式字符串）。
    """
    project_id: str             # 项目编号
    raw_sha256: str             # 原始文件哈希
    parsed_path: str            # 解析产物路径
    parse_backend: str          #解析后端
    quality_score: int          # 质量分
    ts: str                     # 时间戳

class Manifest:
    """
    manifest 的读写封装。

    【为什么封装成类而不是散函数】
    manifest 有两个核心状态：文件路径（path）和内存中的记录表（entries）。
    用一个类把这两样东西绑在一起，读写方法都能访问到同一个路径和同一份数据，
    避免"函数 A 读这个文件、函数 B 写那个文件"的混乱。
    """
    def __init__(self, path: str | Path, *, lock_timeout: float = 10.0) -> None:
        """
        构造函数：创建 Manifest 对象时自动调用。

        【功能实现原理】
        把路径统一转成 Path 对象存起来，初始化空记录表与锁对象。
        如果文件已经存在，就立刻读进来——"打开即带上历史"，
        调用方拿到对象后不需要额外的 build/init 步骤。

        【参数】
            path: str | Path      —— manifest.json 文件所在路径。
            lock_timeout: float   —— 抢文件锁最长等多久（秒）。
                                     默认 10 秒：正常批量解析不会锁这么久，
                                     超时说明有进程卡死了，应该炸而不是无限等。

        【注意】lock_timeout 后面带 * 表示"只能按关键字传"，
        这样调用方写 Manifest(p, 30) 这种位置参数会被当场拒绝，
        避免"我以为第二个参数是别的"这类低级误会。
        """
        # Path(path) 把 str 或 Path 统一转换成 Path 对象
        self.path = Path(path)

        # 初始化记录：空字典。键是 project_id，值是 ManifestEntry。
        # 用字典是因为"按项目编号查记录"是频繁操作，字典的键查找是 O(1)，比列表遍历快得多。
        self.entries: dict[str, ManifestEntry] = {}

        # 锁文件放在账本旁边（manifest.json.lock）。
        # 为什么不锁 manifest.json 本身：因为落盘用的是"替换"——旧文件的 inode 被换掉了，
        # 锁在一个已经被替换掉的文件上毫无意义。必须锁一个恒定的、只用于加锁的侧文件。
        self._lock = FileLock(f"{self.path}.lock", timeout=lock_timeout)

        self._autosave = True  # 默认每次 upsert 都落盘（安全的那条路应当是默认值）
        self._dirty = False  # 刚构造时内存与磁盘一致，无未落盘改动

        # 如果文件已经存在，就把它读进来，把磁盘上的历史记录加载到内存。
        if self.path.exists():
            self.load()

    # ── 读：磁盘 → 内存 ────────────────────────────────────────────────

    def load(self) -> None:
        """
        从磁盘读取 manifest.json，把内容加载进 self.entries。

        【功能实现原理】
        JSON 文件读进来后是一个"字典套字典"的结构
        （形如 {"项目A": {...字段...}, "项目B": {...字段...}}）。
        遍历最外层字典，把每个内层字典转回 ManifestEntry 对象，
        这样才能用 entry.project_id 而不是 data["project_id"]。

        【为什么这里不加锁】
        落盘用的是 os.replace 原子替换：读者要么看到完整旧文件、要么看到完整新文件，
        不存在"读到一半"的中间态。这是原子写带来的额外好处——读侧可以完全免锁。
        （反过来说，如果当初是原地覆盖写，这里就非加锁不可。）

        【返回值】None，本方法只改 self.entries 内部状态，不返回数据。
        """
        self.entries = self._read_disk()  # 委托给 _read_disk 做"读 + 还原 + 损坏处理"
        self._dirty = False  # 刚读完，内存与磁盘一致

    def _read_disk(self) -> dict[str, ManifestEntry]:
        """
        真正干活的方法：把磁盘文件读成 {project_id: ManifestEntry} 字典。

        【为什么要单独抽出来】
        load() 和 flush() 都要读磁盘（flush 是"合并式落盘"，必须知道磁盘现状），
        损坏处理、字段校验这些逻辑抽一份，才不会两处各写一遍、日后改一处漏一处。

        【返回值】文件不存在时返回空字典；存在时返回解析结果。
        """
        # 文件不存在：首次运行时就是这种情况，返回空表即可（不要报错）。
        if not self.path.exists():
            return {}

        try:
            with open(self.path, encoding="utf-8") as f:
                raw: dict = json.load(f)
        except json.JSONDecodeError as exc:
            # 账本损坏。最糟的处理方式是"静默重建空表"——那等于把已有的解析记录全丢了，
            # 而且把问题藏起来，等到对账时才发现。
            # 正确做法：先留一份诊断副本（copy 不是 move，别在出错的路上再破坏一次现场），
            # 再抛一个带路径的明确异常，让人去查根因或从 raw 重建。
            backup = self.path.with_suffix(f".corrupt-{int(time.time())}.json")
            shutil.copy2(self.path, backup)  # copy2 保留时间戳，方便日后比对
            raise ValueError(
                f"manifest 已损坏无法解析：{self.path}（诊断副本：{backup}）。"
                "请检查并修复，或从 data/raw 重建账本。"
            ) from exc

        # 遍历读到的每个 (项目编号, 字段字典) 键值对。
        out: dict[str, ManifestEntry] = {}
        for project_id, data in raw.items():
            try:
                # 用 data 把字段字典展开成关键字参数，传给 ManifestEntry 构造函数，还原成对象。
                out[project_id] = ManifestEntry(**data)
            except TypeError as exc:
                # 字段多了/少了都会在这里炸。带上 project_id，否则几百条里你找不到是哪条。
                raise ValueError(f"manifest 条目字段不匹配：{project_id} → {data}") from exc
        return out

    # ── 写：内存 → 磁盘 ────────────────────────────────────────────────

    def save(self) -> None:
        """
        把内存中的 self.entries 原样全量写回磁盘（内存是唯一真相）。

        【与 flush 的区别——这两个方法不能混用】
            save()  —— 全量覆盖：磁盘上有什么不重要，以内存为准。
                        用于首次建账、重建、单测里造数据。
            flush() —— 合并落盘：先读磁盘最新状态，只覆盖本方持有的项目编号。
                        日常登记一律用它，否则会覆盖掉别的进程刚写的条目。
        """
        # asdict(e) 把数据类对象转成普通字典，得到 {project_id: {...字段...}} 结构。
        payload = {pid: asdict(e) for pid, e in self.entries.items()}
        self._write_atomic(payload)
        self._dirty = False  # 已落盘，清掉脏标记

    def flush(self) -> None:
        """
        把内存中的记录**合并**到磁盘：锁 → 重读 → 合并 → 原子写。

        【为什么不能直接 save】
        两个进程各自 load 了一份旧账本，各自 upsert 自己的项目，然后各自 save：
        后 save 的那个会把前一个刚写的条目整条抹掉。这不是假设，多进程批处理必然发生。
        所以落盘前必须在锁内重读磁盘，把"别人写的条目"先接过来再合并。

        【合并的方向】
        merged 先装磁盘上的全部条目，再 update 本方内存条目：
        update 的语义是"同键覆盖、新键新增"，所以同一个项目重跑时本方覆盖旧记录（正确），
        其他项目则原样保留（也正确）。

        【锁只包住这一小段】
        锁的临界区只有"读 + 写"两次文件 IO，不含解析过程。
        解析 52 个文件可能要几十分钟，把锁包在解析外面会让并发彻底失去意义。
        """
        with self._lock:  # 抢到文件锁才允许读改写
            merged = self._read_disk()  # 1. 读磁盘最新状态（别人可能刚写过）
            merged.update(self.entries)  # 2. 本方条目覆盖同名键，其余保留
            payload = {pid: asdict(e) for pid, e in merged.items()}
            self._write_atomic(payload)  # 3. 原子落盘
            self.entries = merged  # 4. 内存同步为合并后的全量，保持与磁盘一致
        self._dirty = False

    def _write_atomic(self, payload: dict[str, dict]) -> None:
        """
        原子写：先写同目录下的临时文件，fsync 之后再 os.replace 替换过去。

        【为什么要这么绕】
        open(path, "w") 会立刻把原文件截断为 0 字节，然后一点一点往里写。
        如果这个过程中进程被 kill（或断电），磁盘上就留下一个半截 JSON，
        下次启动 load() 直接 JSONDecodeError，整个账本报废。
        换成"写临时文件 + 原子替换"后，任何时刻磁盘上的 manifest.json 都是完整的一份。

        【参数】
            payload: dict[str, dict] —— 已经序列化好的"字段字典"结构，可直接 json.dump。
        """
        # 目录可能还不存在（首次运行），先建出来；parents=True 连父目录一起建，exist_ok 允许已存在。
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # 临时文件必须建在**同一个目录**下：
        # os.replace 只能在同一文件系统内做原子替换，跨盘会抛 EXDEV（Invalid cross-device link）。
        # mkstemp 返回 (文件描述符, 路径)，且文件名自带随机后缀，多进程同时写不会撞名。
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            # os.fdopen 把文件描述符包装成正常的文件对象，这样能用 with 自动关闭。
            # newline="\n" 避免 Windows 下写成 CRLF，否则 git diff 会整文件显示改动。
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                # ensure_ascii=False 让中文直接写原字符（否则会变成 \uXXXX）。
                # indent=2 让 JSON 有缩进——出问题时你会需要肉眼翻这个账本。
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")  # 末尾补换行，符合 POSIX 文本文件惯例
                f.flush()  # 把 Python 缓冲区推给操作系统
                os.fsync(f.fileno())  # 把操作系统缓存刷到磁盘：不做这步，断电后可能换成空文件
            # 替换：os.replace 是覆盖语义且原子。
            # 不能用 os.rename —— Windows 上目标已存在时会直接 FileExistsError。
            self._replace_with_retry(tmp_name)
        except BaseException:
            # 任何异常（包括 Ctrl+C 的 KeyboardInterrupt）都要清理临时文件，否则目录会越积越脏。
            # suppress(FileNotFoundError)：临时文件可能已被替换走，删不到就算了，别掩盖真正的异常。
            with suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise  # 清理完必须把原异常抛出去，绝不能吞掉

    def _replace_with_retry(self, tmp_name: str) -> None:
        """
        执行 os.replace，遇到 Windows 的文件占用错误时短暂退避重试。

        【为什么需要它】
        Windows 上如果另一个进程正持有 manifest.json 的句柄（杀毒软件扫描、VS Code 打开着、
        Explorer 在读），替换会抛 PermissionError(WinError 32)。
        这类占用通常几十毫秒就释放，退避重试一次就过去了；直接失败会让整批解析白跑。
        """
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp_name, self.path)  # 原子替换，成功即返回
                return
            except PermissionError:
                # 最后一次还失败就如实抛出——重试 3 次仍被占用，说明不是瞬时问题，
                # 继续等下去只会让人以为程序卡死。
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                # 指数退避：50ms → 100ms。指数增长比固定间隔更合适，能覆盖不同长度的占用。
                time.sleep(_REPLACE_BACKOFF * (2 ** attempt))

    # ── 增 / 查 ────────────────────────────────────────────────────────

    def upsert(self, entry: ManifestEntry, *, autosave: bool = True) -> None:
        """
        新增或更新一条记录（upsert = update + insert）。

        【功能实现原理】
        以 project_id 为键写入字典。如果这个键已存在，就覆盖（更新）；
        如果不存在，就新增。这样同一个项目重新解析时，登记表不会积累重复条目。

        【参数】
            entry: ManifestEntry —— 要登记的一条记录。
            autosave: bool       —— 是否立刻落盘，默认 True。

        【autosave 默认 True 的理由】
        "登记了但没落盘"是最常见的事故：程序在日志里一切正常，进程退出后记录全没了。
        让安全的那条路成为默认值，只有明确知道自己在攒批（with m.batch():）时才关掉它。
        注意 batch() 内部会自动把它关掉，所以调用方通常不需要手动传这个参数。
        """
        # 用项目编号做键，把整条记录塞进字典。
        # 字典的 [] 赋值语义：键存在则覆盖值，键不存在则新建键值对。
        self.entries[entry.project_id] = entry
        self._dirty = True  # 标记"内存有未落盘改动"

        # 攒批期间 _autosave 为 False，此处不会落盘，等 batch 退出时统一写一次。
        if autosave and self._autosave:
            self.flush()

    @contextmanager
    def batch(self):
        """
        批量登记上下文：块内不落盘，退出时只落盘一次。

        【为什么需要】
        一次解析 52 个文件，如果每条 upsert 都落盘，就是 52 次"全量序列化整个账本 + fsync"，
        写放大是 O(n²)。攒批之后只有 1 次写入。

        【为什么用 try/finally 而不是只写 yield 之后的代码】
        finally 保证异常路径也会执行落盘。upsert 只在解析成功后才会被调用，
        所以内存里的条目都是"产物文件确实已经写出来了"的。
        中途异常如果不刷盘，就会出现"parsed/ 里有文件、账本里没记录"的孤儿文件，
        check_parse_sync.py 会把它报成缺失，而你会误以为是解析失败——查错方向从一开始就歪了。
        宁可多写一次，也不要制造账本与产物的不一致。

        【用法】
            with m.batch():
                for f in files:
                    m.upsert(ManifestEntry(...))
        """
        prev = self._autosave  # 记录调用前的状态，支持嵌套调用（内层退出时恢复成外层设置）
        self._autosave = False
        try:
            yield self  # 把 self 交给 with 块，块内可以继续 m.upsert(...)
        finally:
            self._autosave = prev  # 先恢复标志位，再落盘（顺序反了会把恢复动作也攒进批里）
            if self._dirty:
                self.flush()  # 有改动才写，空块退出不产生任何 IO

    def get(self, project_id: str) -> ManifestEntry | None:
        """
        按项目编号查询记录。

        【功能实现原理】
        dict.get(key) 在键存在时返回对应值，
        键不存在时返回 None（而不是抛 KeyError 异常），
        这样调用方可以安全地判断"这个项目有没有解析过"。

        【参数】
            project_id: str —— 要查询的项目编号。

        【返回值】
            ManifestEntry | None —— 找到则返回记录对象，找不到返回 None。

        【典型用法（幂等判重）】
            old = m.get(pid)
            if old and old.raw_sha256 == sha256_of(raw):
                return Path(old.parsed_path)     # 原件没变，直接复用，不重复解析
        """
        return self.entries.get(project_id)