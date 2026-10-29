"""压缩后端：统一接口 + 各格式实现 + 自动降级 + 强制模式。

架构
----
* ``ZipCompressor``     : 标准库 zipfile（支持 ZipCrypto 与传统口令）
* ``TarStreamCompressor``: tarfile 流式写入，压缩层可插拔（gz/bz2/xz/zst）
* ``SevenZipCompressor``: 先构建未压缩 tar 暂存，再调用 7-Zip 压缩（原生分卷 / AES-256）
* ``RarCompressor``     : 同一套 tar 暂存，交给 RAR 生成 .rar（原生分卷 / 头部加密）
* ``split_file()``      : 通用分卷（tar 系与强制模式使用）

``create_archive()`` 是唯一对外入口，负责：
1. 按格式挑选可用后端；不可用时给出「缺少什么软件 + 安装命令」，``force=True`` 时尝试替代方案；
2. 分卷策略（7z 原生 / RAR 原生 / 通用切片）；
3. 进度回调（按源字节计）；
4. 失败时清理半成品，并把失败原因包装成带修复建议的异常。
"""

from __future__ import annotations

import bz2
import contextlib
import logging
import gzip
import io
import lzma
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol

try:  # 可选依赖
    import zstandard as _zstd_mod  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    _zstd_mod = None

try:
    import py7zr  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    py7zr = None

from .platform_info import get_platform_info
from .staging import force_rmtree, make_staging_dir, same_volume
from .tools import find_7z, find_rar, probe_unrar_trial

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

TAR_FORMATS = {"tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst"}
SUPPORTED_FORMATS: tuple[str, ...] = ("zip", "7z", "rar", "tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst")

FORMAT_EXTENSIONS: dict[str, str] = {
    "zip": ".zip",
    "7z": ".7z",
    "rar": ".rar",
    "tar": ".tar",
    "tar.gz": ".tar.gz",
    "tar.bz2": ".tar.bz2",
    "tar.xz": ".tar.xz",
    "tar.zst": ".tar.zst",
}

# 解压时需要先选哪个文件（GUI/CLI 提示用）
FIRST_VOLUME_HINT: dict[str, str] = {
    "7z-split": "{stem}.001",
    "rar-native": "{stem}.part1.rar",
    "split": "{stem}.part01",
}

LEVEL_LABELS = {0: "仅存储", 1: "最快", 3: "较快", 5: "标准", 7: "较好", 9: "最强"}


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------

class CompressionError(RuntimeError):
    def __init__(self, message: str, *, backend: str = "", fix: str = "") -> None:
        super().__init__(message)
        self.backend = backend
        self.fix = fix

    def __str__(self) -> str:
        base = super().__str__()
        extra = []
        if self.backend:
            extra.append(f"后端：{self.backend}")
        if self.fix:
            extra.append(f"建议：{self.fix}")
        return base + ("\n" + "\n".join(extra) if extra else "")


class BackendUnavailable(CompressionError):
    """本机缺少创建该格式所需的软件。"""


class PasswordRequired(CompressionError):
    pass


class ArchiveCancelled(CompressionError):
    def __init__(self) -> None:
        super().__init__("备份已取消")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class Item:
    """待备份条目。``size`` 对目录而言是子项字节总和。"""

    source: Path
    arcname: str
    size: int = 0
    mtime: float | None = None
    is_dir: bool = False
    is_link: bool = False
    link_target: str = ""


@dataclass
class ArchiveSpec:
    dest_path: Path
    fmt: str
    password: str | None = None
    encrypt_mode: str | None = None
    level: int = 5
    multivolume: str | None = None
    volume_size: int = 0
    follow_symlinks: bool = False
    force: bool = False


@dataclass
class VolumeInfo:
    path: Path
    size: int


@dataclass
class ArchiveResult:
    path: Path
    fmt: str
    # 产物**真实容器格式**。通常等于 ``fmt``，但强制模式降级时可能不同：
    # 用户选 RAR 而无 RAR 引擎 → 产物是 ZIP 容器（扩展名仍是 .rar）。
    # 校验/解压必须按真实容器走，否则 `unrar t` 打 ZIP 会报退出码 13。
    container_fmt: str = ""
    volumes: list[VolumeInfo] = field(default_factory=list)
    files: int = 0
    dirs: int = 0
    source_bytes: int = 0
    skipped: int = 0
    #: 因「被占用 / 无权限」被跳过的条目（归档内名 + 原因）。
    #: 非空时 engine 会把任务标成 ``partial`` 而不是 ``success``
    #: （用户测试 P0：Excel/PSD 打开的文件被静默跳过却报成功）。
    skipped_busy: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    backend: str = ""
    encrypted: bool = False
    encrypt_mode: str | None = None
    secure: bool = True
    multivolume: str | None = None
    volume_size: int = 0
    elapsed: float = 0.0
    forced: bool = False
    notes: list[str] = field(default_factory=list)
    #: 备份身份名（manifest 的 stem = 该文件的键）：由 create_archive 从 dest **正向**推导，
    #: 调用方（engine）不得再从产物名反推 —— 反推用 `str.find(ext)` 首匹配，任务名里含
    #: 扩展名（如任务名 `foo.zip`）时会命中第一个 `.zip`，导致多包共用同一个 manifest
    #: 互相覆盖（审查 W-A2-2，S1）。
    stem: str = ""

    @property
    def archive_bytes(self) -> int:
        if self.volumes:
            return sum(v.size for v in self.volumes)
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    @property
    def ratio(self) -> float:
        return (self.archive_bytes / self.source_bytes) if self.source_bytes else 0.0


class Progress:
    """进度聚合器（可被任意线程调用）。"""

    def __init__(self, callback: Callable[[dict], None] | None = None) -> None:
        self._cb = callback
        self.total_bytes = 0
        self.done_bytes = 0
        self.total_items = 0
        self.done_items = 0
        self.phase = "prepare"
        self._last = 0.0

    def set_plan(self, total_bytes: int, total_items: int) -> None:
        self.total_bytes = max(0, total_bytes)
        self.total_items = max(0, total_items)
        self.emit(force=True)

    def describe(self, phase: str) -> None:
        self.phase = phase
        self.emit(force=True)

    def add(self, n: int) -> None:
        if n:
            self.done_bytes += n
        self.emit()

    def item_done(self) -> None:
        self.done_items += 1

    def emit(self, force: bool = False) -> None:
        if self._cb is None:
            return
        now = time.monotonic()
        if not force and now - self._last < 0.12:
            return
        self._last = now
        with contextlib.suppress(Exception):
            self._cb(self.snapshot())

    def snapshot(self) -> dict:
        return {
            "phase": self.phase,
            "done_bytes": self.done_bytes,
            "total_bytes": self.total_bytes,
            "done_items": self.done_items,
            "total_items": self.total_items,
        }


# --------------------------------------------------------------------------
# 扫描 / 规划
# --------------------------------------------------------------------------

def _iter_tree(root: Path, arcname: str, follow: bool) -> Iterator[tuple[Path, str, bool, bool, str]]:
    """产出 (路径, 归档名, 是否目录, 是否符号链接, 链接目标)。"""
    try:
        is_link = root.is_symlink()
    except OSError:
        is_link = False
    if is_link and not follow:
        yield root, arcname, False, True, _safe_readlink(root)
        return
    if not root.is_dir() or (is_link and follow):
        yield root, arcname, False, False, ""
        return
    yield root, arcname, True, False, ""
    stack: list[tuple[Path, str]] = [(root, arcname)]
    while stack:
        current, current_arc = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            continue
        for entry in entries:
            p = Path(entry.path)
            arc = f"{current_arc}/{entry.name}".replace("\\", "/")
            try:
                if entry.is_symlink():
                    if follow:
                        if p.is_dir():
                            yield p, arc, True, False, ""
                            stack.append((p, arc))
                        else:
                            yield p, arc, False, False, ""
                    else:
                        yield p, arc, False, True, _safe_readlink(p)
                elif entry.is_dir(follow_symlinks=False):
                    yield p, arc, True, False, ""
                    stack.append((p, arc))
                else:
                    yield p, arc, False, False, ""
            except OSError:
                continue


def _safe_readlink(p: Path) -> str:
    try:
        return os.readlink(p)
    except OSError:
        return ""


def _build_matcher(patterns: Iterable[str]):
    """构造排除匹配器。

    ## 关键修复：目录被排除后，其**子文件**也必须被排除（P0，CLI Agent 实测）

    `--help` 与 README 给的示例是 ``--exclude '*.tmp,node_modules'``。
    旧实现只拿**当前条目**的 name / rel 去 fnmatch：

        node_modules/skip.txt  →  name='skip.txt'  rel='node_modules/skip.txt'
        对模式 'node_modules'  →  两个都不匹配  →  **文件仍被完整打包**

    实测 6 种模式对照：``node_modules``❌ / ``node_modules*``✅ /
    ``*node_modules*``✅ / ``**/node_modules/**``✅ / ``*/node_modules/*``❌。

    也就是说**官方示例本身不生效** —— 用户照抄文档，以为排除了 node_modules，
    实际备份包塞满 node_modules。

    ## 修法：对路径的**每一个前缀**都试一遍

    把 ``a/b/c.txt`` 展开成 ``a``、``a/b``、``a/b/c.txt`` 分别匹配。
    这样 ``node_modules`` 能命中前缀 ``node_modules``，子文件自然被排除。
    同时保留原有语义（name 匹配、rel 匹配、``**/`` 特殊处理、``re:`` 正则）。
    """
    import fnmatch
    import re

    # **`re:` 模式不能做反斜杠→斜杠转换**（P0）：正则里的 `\.` `\d` `\s` 都是
    # 有意义转义，被替换成 `/.` `/d` 后正则就废了。实测 `re:\.log$` → `re:/.log$`
    # → 永不匹配。只对普通 glob 做 Windows 路径分隔符归一。
    pats = [
        (p.strip() if p.strip().startswith("re:") else p.strip().replace("\\", "/"))
        for p in patterns
        if p and p.strip()
    ]
    regexes = []
    for p in pats:
        if p.startswith("re:"):
            try:
                regexes.append(re.compile(p[3:]))
            except re.error:
                continue

    def _hit(prefix: str, pattern: str) -> bool:
        """单个前缀是否命中单个模式。

        除了「整个前缀匹配」，还支持**路径中任一段**匹配 —— 这是
        `node_modules` 这种「纯目录名」模式能排除任意深度的关键：
        `a/b/node_modules` 的末段是 `node_modules`，fnmatch 整体比会 False，
        但按段比就命中。用户写 `--exclude node_modules` 的意图正是这个。
        """
        if fnmatch.fnmatchcase(prefix, pattern):
            return True
        # 模式里没有 `/` → 按路径段逐个比（任一段匹配即排除）
        if "/" not in pattern:
            for seg in prefix.split("/"):
                if seg and fnmatch.fnmatchcase(seg, pattern):
                    return True
        if pattern.startswith("**/") and fnmatch.fnmatchcase(prefix, pattern[3:]):
            return True
        # `dir/**` 应排除 dir 本身及全部后裔
        if pattern.endswith("/**") and fnmatch.fnmatchcase(prefix, pattern[:-3]):
            return True
        if pattern.endswith("/*") and fnmatch.fnmatchcase(prefix, pattern[:-2]):
            return True
        return False

    def match(name: str, rel: str) -> bool:
        rel_norm = rel.replace("\\", "/")
        if not rel_norm and not name:
            return False
        # 正则先判（`re:` 模式直接搜 rel / name）
        for rgx in regexes:
            if rgx.search(rel_norm) or rgx.search(name):
                return True

        # 候选前缀：相对路径的**各级祖先**（含最深一级目录）+ 完整路径 + 文件名
        # 例 `a/b/node_modules/y.js` →
        #   ['a', 'a/b', 'a/b/node_modules', 'a/b/node_modules/y.js', 'y.js']
        prefixes: list[str] = []
        if rel_norm:
            parts = rel_norm.split("/")
            for i in range(1, len(parts) + 1):
                prefixes.append("/".join(parts[:i]))
        if name:
            prefixes.append(name)

        for p in pats:
            if p.startswith("re:"):
                continue
            for pref in prefixes:
                if pref and _hit(pref, p):
                    return True
        return False

    return match


# 系统 / 软件产生的元数据文件：默认跳过，避免往压缩包里塞垃圾数据
_AUTO_SKIP_NAMES = {
    "thumbs.db",
    "desktop.ini",
    ".ds_store",
    "ehthumbs.db",
    "$recycle.bin",
    "system volume information",
    ".spotlight-v100",
    ".fseventsd",
    ".trashes",
    "last-run.json",
}


def _auto_skip(path: Path) -> bool:
    name = path.name.lower()
    if name in _AUTO_SKIP_NAMES:
        return True
    if name == ".backupstudio" or name.startswith(".bs-staging") or name.endswith(".full-tmp") or ".part-tmp" in name:
        return True
    return False


#: 最近一次 `plan_items` 跳过的「非普通文件」（FIFO/设备/socket）。
#: 这些无法备份（FIFO 会永久阻塞），必须让上层能告知用户。
_LAST_SPECIAL_SKIPPED: list[str] = []


def _notify_skipped(backend, reason: str) -> None:
    """把「这个条目没能写进包」登记到后端的 skipped_busy。

    为什么需要它（红队 F2/F4/F5，都是 P0 级假绿）：
    历史上「跳过一个文件」有**三条互不相通的记账路径**：
      * 后端自己 catch OSError → 只有 ExternalCompressor/TarStreamCompressor 会登记
      * create_archive 的 except → 只 append 到 result.errors
      * plan_items 的 stat/scandir 失败 → 只有一个 skipped 计数器，**从不影响 status**
    而 engine 的 partial 判定**只看 skipped_busy** → 后两条都是静默丢数据却报 success。

    这个函数统一收口：无论哪个后端（包括没有 skipped_busy 属性的内置 zip/py7zr），
    都用 setattr 补一个列表再登记，保证「丢了一个文件」一定能反映到 status。
    """
    try:
        bucket = getattr(backend, "skipped_busy", None)
        if bucket is None:
            bucket = []
            setattr(backend, "skipped_busy", bucket)
        if reason not in bucket:
            bucket.append(reason)
    except Exception:  # noqa: BLE001 - 记账失败不能影响备份主流程
        pass


def plan_items(
    sources: Iterable[Item],
    exclude_globs: Iterable[str] = (),
    exclude_abs_dirs: Iterable[str | os.PathLike[str]] = (),
    follow_symlinks: bool = False,
) -> tuple[list[Item], int]:
    """展开来源、套用排除规则，返回 (条目, 被排除数量)。"""
    matcher = _build_matcher(exclude_globs)
    excluded = []
    for d in exclude_abs_dirs:
        try:
            excluded.append(os.path.abspath(str(d)).lower().rstrip("/\\"))
        except (OSError, ValueError):
            continue

    def in_excluded(path: Path) -> bool:
        try:
            ab = os.path.abspath(str(path)).lower()
        except (OSError, ValueError):
            return False
        for ex in excluded:
            if ab == ex or ab.startswith(ex + os.sep) or ab.startswith(ex + "/"):
                return True
        return False

    items: list[Item] = []
    skipped = 0
    special_skipped: list[str] = []
    for root in sources:
        src = Path(root.source)
        if not src.exists() and not src.is_symlink():
            items.append(Item(src, root.arcname, 0, None, False))
            continue
        collected: list[Item] = []
        for path, arc, is_dir, is_link, target in _iter_tree(src, root.arcname, follow_symlinks):
            if in_excluded(path) or _auto_skip(path):
                skipped += 1
                continue
            rel = arc[len(root.arcname) :].lstrip("/") if arc.startswith(root.arcname) else arc
            if matcher(path.name, rel):
                skipped += 1
                continue
            try:
                st = path.stat() if follow_symlinks or not is_link else path.lstat()
            except OSError:
                skipped += 1
                continue
            # 只处理**普通文件 / 目录 / 符号链接**，其余类型一律跳过（S0）。
            #
            # 元数据专家实测：FIFO 让**三个后端全部永久挂死**（faulthandler 栈停在
            # `add_file` 的 `open()` / `read()`）。原因是 FIFO 的 `stat()` 正常返回、
            # `st_size` 是 0，于是被当成普通文件放行；随后 `open(path,"rb")` 会
            # **阻塞到有写端出现**——备份进程就此挂住，用户只能强杀。
            # 同类还有字符/块设备（`/dev/zero` 会把备份撑爆）、socket。
            #
            # 这些都不是「可备份的数据」，跳过是唯一正确处理。
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                skipped += 1
                special_skipped.append(str(path))
                continue
            size = 0 if (is_dir or is_link) else st.st_size
            collected.append(Item(path, arc, size, st.st_mtime, is_dir, is_link, target))

        # 目录条目保留「子树内文件字节之和」（供进度条与既有契约使用），
        # 但**每一级祖先只加自己的直接子树，不重复累加**。
        #
        # 旧实现那层 `while True` 让每个文件向**每一级祖先（含源根）各贡献一次**，
        # 于是把目录与文件一起求和会重复计数 → 虚高公式 `倍数 = 目录层数 + 2`：
        #   文件在源根下 → 2×；+1 层 → 3×；+4 层 → 6×；+5 层 → 7×
        #
        # 现在目录 size 只用于「进度条估算」，而 `source_bytes` 与 `engine` 的
        # 空间预检**只累加非目录条目**，两者不再互相污染。
        dir_bytes: dict[str, int] = {}
        # 1) 先把所有**目录**登记为 0，保证每个目录都存在于字典里
        for it in collected:
            if it.is_dir:
                dir_bytes.setdefault(it.arcname, 0)
        # 2) 每个文件的字节记到它的**直接父目录**
        for it in collected:
            if it.size and not it.is_dir:
                arc = it.arcname
                parent = arc.rsplit("/", 1)[0] if "/" in arc else None
                if parent is not None:
                    dir_bytes[parent] = dir_bytes.get(parent, 0) + it.size
        # 3) 自底向上：把每个子目录的累计值加到它的父目录（深者先处理）
        for arc in sorted(dir_bytes, key=lambda s: s.count("/"), reverse=True):
            if "/" in arc:
                parent = arc.rsplit("/", 1)[0]
                dir_bytes[parent] = dir_bytes.get(parent, 0) + dir_bytes[arc]
        for it in collected:
            if it.is_dir and it.arcname in dir_bytes:
                it.size = dir_bytes[it.arcname]
            items.append(it)
    # 把「非普通文件被跳过」的清单暴露给上层（让用户知道 FIFO/设备文件没进包）
    if special_skipped:
        _LAST_SPECIAL_SKIPPED[:] = special_skipped
    return items, skipped


# --------------------------------------------------------------------------
# 后端协议
# --------------------------------------------------------------------------

class BaseCompressor(Protocol):
    name: str

    def add_dir(self, arcname: str, mtime: float | None = None) -> None: ...
    def add_file(self, src: Path, arcname: str, mtime: float | None = None) -> int: ...
    def add_bytes(self, data: bytes, arcname: str, mtime: float | None = None) -> int: ...
    def add_symlink(self, arcname: str, target: str, mtime: float | None = None) -> None: ...
    def close(self) -> None: ...
    def abort(self) -> None: ...
    @property
    def warnings(self) -> list[str]: ...


class _Reader:
    """把迭代器包装成 read() 接口，供 tarfile.addfile 使用。"""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._it = chunks
        self._buf = b""

    def read(self, n: int = -1) -> bytes:
        want = n if n and n > 0 else (1 << 20)
        while len(self._buf) < want:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        if n and n > 0:
            out, self._buf = self._buf[:n], self._buf[n:]
            return out
        out, self._buf = self._buf, b""
        return out


def _chunks_of_fh(
    fh,  # noqa: ANN001 - 已打开的二进制文件对象
    progress: Progress,
    chunk: int = 1 << 20,
    cancel: Callable[[], bool] | None = None,
) -> Iterator[bytes]:
    while True:
        if cancel is not None and cancel():
            raise ArchiveCancelled()
        data = fh.read(chunk)
        if not data:
            break
        progress.add(len(data))
        yield data


def _chunks_of(
    src: Path,
    progress: Progress,
    chunk: int = 1 << 20,
    cancel: Callable[[], bool] | None = None,
) -> Iterator[bytes]:
    with open(src, "rb") as fh:
        yield from _chunks_of_fh(fh, progress, chunk, cancel)


# --------------------------------------------------------------------------
# ZIP
# --------------------------------------------------------------------------

#: ZIP 支持的 deflate 级别下界。**0 不是级别** —— 0 表示「不压缩」（STORED），
#: zlib 的合法级别是 1..9，传 0 会变成 zlib.Z_DEFAULT_COMPRESSION（=6），
#: 于是「级别 0」与「不设级别」产出同样的字节，用户看不出区别（S3 的另一半）。
ZIP_MIN_DEFLATE_LEVEL = 1
ZIP_MAX_DEFLATE_LEVEL = 9


def zip_deflate_level(level: int) -> int:
    """把产品的 0..9 级别夹到 ZIP deflate 的合法区间 1..9。

    仅用于 ``compress_type == ZIP_DEFLATED`` 的条目。``level == 0`` 应走 STORED，
    调用方**不要**把 0 传进来（这里也夹成 1 只是防御，不代表 0 应当压缩）。
    """
    return max(ZIP_MIN_DEFLATE_LEVEL, min(ZIP_MAX_DEFLATE_LEVEL, int(level)))


def zip_info_level(zi: zipfile.ZipInfo, level: int, *, is_dir: bool = False) -> None:
    """给 ``ZipInfo`` 同时设好 compress_type 与压缩级别（S3 的修复点）。

    **两条都必须设，缺一即错**：

    * ``compress_type`` —— ``ZipInfo`` 自带该字段，默认 ``0 = STORED``，会**覆盖**
      ``ZipFile(compression=...)``；不设就整包不压缩（审查 Q6）。
    * 压缩级别 —— ``zipfile`` 写入时从 ``ZipInfo`` 上取级别交给 ``zlib.compressobj``；
      默认 ``None`` → 落到 ``zlib.Z_DEFAULT_COMPRESSION``（恒 6）。**只设
      compress_type 的话，level=1 与 level=9 产出的字节完全相同**（实测均 231809 B）
      —— 用户以为自己在调压缩级别，实际永远是 6。

    **属性名在 Python 3.13 变过**（这是 CI 在 3.12 上 AttributeError 的根因）：

    * Python **3.10–3.12**：``ZipInfo.__slots__`` 里是 ``_compresslevel``（私有）
    * Python **3.13+**：改名为 ``compress_level``（公开属性）

    所以必须按运行时**实际拥有的属性名**去设，不能写死。CI 用的是 Python 3.12，
    写死 ``zi.compress_level`` 会在三个 runner 上全部
    ``AttributeError: 'ZipInfo' object has no attribute 'compress_level'``
    （run #39 实证：Windows 15 条、Linux 15 条、macOS 15 条因此失败）。

    STORED（目录条目、``level == 0``）不设级别：zlib 不参与，设了也没有意义。
    """
    if is_dir or level == 0:
        zi.compress_type = zipfile.ZIP_STORED
        return
    zi.compress_type = zipfile.ZIP_DEFLATED
    _set_zipinfo_level(zi, zip_deflate_level(level))


def _set_zipinfo_level(zi: zipfile.ZipInfo, level: int) -> None:
    """按当前 Python 版本设置 ``ZipInfo`` 的压缩级别。

    3.13+ 用 ``compress_level``，3.12 及更早用 ``_compresslevel``。
    用 ``hasattr`` 探测而非比对版本号——对 PyPy、对未来改名都更稳。
    """
    if hasattr(zi, "compress_level"):
        zi.compress_level = level  # Python 3.13+
    elif hasattr(zi, "_compresslevel"):
        zi._compresslevel = level  # Python 3.10–3.12
    # 两者都没有（极老版本）：静默跳过。此时 zipfile 本就不支持按条目设级别，
    # 强行 setattr 在带 __slots__ 的类上会抛 AttributeError，比"用默认级别"更糟。


class ZipCompressor:
    """标准库 zipfile 后端（不支持加密）。

    若任务设置了密码，本后端会直接拒绝（PasswordRequired）——fail-closed：
    宁可明确报错，也不产出"记录为已加密、实际明文"的归档。
    需要加密 ZIP 请安装 7-Zip（走 SevenZipCompressor）或改用 7z 格式。
    """

    name = "python-zipfile"

    def __init__(self, dest_path: Path, spec: ArchiveSpec, progress: Progress) -> None:

        self.skipped_busy: list[str] = []  # 被占用/无权限的条目（P0：不能假绿）
        self.spec = spec
        self.progress = progress
        self.dest = dest_path
        self._warnings: list[str] = []
        self.notes: list[str] = []
        if spec.password:
            raise PasswordRequired(
                "内置 ZIP 引擎不支持加密：本次未创建归档（不会产出明文包）",
                backend=self.name,
                fix="安装 7-Zip 以加密 ZIP，或改用 7z 格式，或关闭密码保护",
            )
        comp = zipfile.ZIP_STORED if spec.level == 0 else zipfile.ZIP_DEFLATED
        self._zip = zipfile.ZipFile(
            dest_path, "w", compression=comp, compresslevel=max(1, min(9, spec.level)), allowZip64=True
        )
        self._cancel: Callable[[], bool] | None = None

    @property
    def warnings(self) -> list[str]:
        return self._warnings

    def set_cancel(self, cancel: Callable[[], bool] | None) -> None:
        """登记取消回调（审查 R3-N3：内置 ZIP 此前不支持取消）。"""
        self._cancel = cancel

    def _check_cancel(self) -> None:
        if self._cancel is not None and self._cancel():
            raise ArchiveCancelled()

    def _info(self, arcname: str, mtime: float | None, is_dir: bool) -> zipfile.ZipInfo:
        ts = time.localtime(mtime if mtime else time.time())[:6]
        if ts[0] < 1980:
            ts = (1980, 1, 1, 0, 0, 0)
        zi = zipfile.ZipInfo(arcname + ("/" if is_dir else ""), date_time=ts)
        zi.external_attr = (0o40755 if is_dir else 0o100644) << 16
        zi.create_system = 3
        # ZipInfo 自带 compress_type，默认 0=STORED，会**覆盖** ZipFile(compression=...)；
        # 不显式设置，整个内置 ZIP 后端就退化成「不压缩」（审查 Q6）。目录条目保持 STORED。
        # S3：**还必须设 compress_level** —— Python 3.13+ 才叫 zinfo.compress_level；3.12 及更早是 _compresslevel，
        # 默认 None → 恒用 zlib 默认级别 6，级别 1..9 全部产出同样的字节。
        zip_info_level(zi, self.spec.level, is_dir=is_dir)
        return zi

    def add_dir(self, arcname: str, mtime: float | None = None) -> None:
        self._zip.writestr(self._info(arcname, mtime, True), b"")

    def add_symlink(self, arcname: str, target: str, mtime: float | None = None) -> None:
        self.add_bytes(target.encode("utf-8", "surrogateescape"), arcname, mtime)

    def add_file(self, src: Path, arcname: str, mtime: float | None = None) -> int:
        self._check_cancel()
        st = src.stat()
        # 先探测可读性：失败则登记并跳过（P0：不能假绿）
        try:
            _probe = open(src, "rb")
        except PermissionError:
            self.skipped_busy.append(f"{arcname}（被占用或无权限）")
            return 0
        else:
            _probe.close()
        with open(src, "rb") as fh, self._zip.open(self._info(arcname, mtime or st.st_mtime, False), "w") as out:
            while True:
                self._check_cancel()
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                self.progress.add(len(chunk))
        return st.st_size

    def add_bytes(self, data: bytes, arcname: str, mtime: float | None = None) -> int:
        self._zip.writestr(self._info(arcname, mtime, False), data)
        self.progress.add(len(data))
        return len(data)

    def close(self) -> None:
        self._zip.close()

    def abort(self) -> None:
        with contextlib.suppress(Exception):
            self._zip.close()
        with contextlib.suppress(OSError):
            self.dest.unlink()


# --------------------------------------------------------------------------
# tar（Python tarfile 流式）
# --------------------------------------------------------------------------

_CODEC_LEVEL_RANGE = {"bz2": (1, 9), "gz": (1, 9), "xz": (0, 9), "zst": (1, 19)}


class TarStreamCompressor:
    name = "python-tarfile"

    def __init__(self, dest_path: Path, spec: ArchiveSpec, progress: Progress) -> None:
        self.spec = spec
        self.progress = progress
        self.dest = dest_path
        self._warnings: list[str] = []
        self.notes: list[str] = []
        self.codec = spec.fmt.split(".", 1)[1] if "." in spec.fmt else ""
        # **必须写到 `.bs-tmp-<pid>` 事务名，不能直接写最终文件名**（P0，红队 F3）。
        #
        # 直接写最终名的两个后果：
        #  1. 强杀后残包**文件名完全正常**地躺在备份目录里，而
        #     `sweep_orphans` 只认 `.bs-tmp-` / `bs-stage-` 前缀 → 永远清不掉，
        #     保留策略也不回收（它只处理有清单的包）→ 永久占盘。
        #  2. 那个残包是**截断的**，而裸 tar 边界截断后 `verify_archive` 会误判
        #     `passed`（红队 F1）→ 用户以为有备份，实际尾部文件全丢。
        #
        # 与 `ExternalCompressor` 统一（它早已用 `.bs-tmp-<pid>`）。
        self._tmp_dest = dest_path.with_name(f"{dest_path.name}.bs-tmp-{os.getpid()}")
        self._raw = open(self._tmp_dest, "wb")
        self._proc: subprocess.Popen | None = None
        self._cancel: Callable[[], bool] | None = None
        # **必须在实例上初始化**：类体里的 `skipped_busy: list[str] = []`
        # 是类级别共享列表，所有实例共用同一个对象 → 上一个备份跳过的文件
        # 会累加到后续所有备份（实测导致 9 个测试失败；GUI/daemon 常驻进程里
        # 会让第 2 个任务误报 partial）。
        self.skipped_busy = []
        self._comp = self._make_compressor(self.codec)
        self._tar = tarfile.open(fileobj=self._comp, mode="w|", format=tarfile.PAX_FORMAT, bufsize=1 << 20)

    def _make_compressor(self, codec: str):
        lo, hi = _CODEC_LEVEL_RANGE.get(codec, (1, 9))
        level = max(lo, min(hi, self.spec.level if self.spec.level else (hi if codec != "xz" else 6)))
        if codec == "gz":
            return gzip.GzipFile(fileobj=self._raw, mode="wb", compresslevel=level, mtime=0)
        if codec == "bz2":
            return bz2.BZ2File(self._raw, "wb", compresslevel=level)
        if codec == "xz":
            return lzma.LZMAFile(self._raw, "wb", preset=level)
        if codec == "zst":
            if _zstd_mod is not None:
                # write_checksum 必须开启：校验侧流式解压只有带帧校验和时才能检出内容损坏（审查 M18）
                return _zstd_mod.ZstdCompressor(level=level, write_checksum=True).stream_writer(
                    self._raw, closefd=False
                )
            zstd = shutil.which("zstd")
            if not zstd:
                raise BackendUnavailable(
                    "创建 tar.zst 需要 zstandard 模块或系统 zstd 程序",
                    fix="pip install zstandard 或安装 zstd（apt install zstd / brew install zstd）",
                )
            self._proc = subprocess.Popen([zstd, "-q", "--check", f"-{level}", "-c"], stdin=subprocess.PIPE, stdout=self._raw)
            return self._proc.stdin
        return self._raw

    @property
    def warnings(self) -> list[str]:
        return self._warnings

    def _ti(self, arcname: str, mtime: float | None, mode: int, typ: bytes, size: int = 0) -> tarfile.TarInfo:
        ti = tarfile.TarInfo(arcname)
        ti.type = typ
        ti.size = size
        ti.mtime = int(mtime if mtime else time.time())
        ti.mode = mode
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    def add_dir(self, arcname: str, mtime: float | None = None) -> None:
        self._tar.addfile(self._ti(arcname.rstrip("/") + "/", mtime, 0o755, tarfile.DIRTYPE))

    def add_symlink(self, arcname: str, target: str, mtime: float | None = None) -> None:
        ti = self._ti(arcname, mtime, 0o777, tarfile.SYMTYPE)
        ti.linkname = target
        self._tar.addfile(ti)

    #: 因「被占用 / 无权限」被跳过的条目（与 ExternalCompressor 同契约）。
    #: 用户测试 P0 回归：早前只给 ExternalCompressor 加了 skipped_busy，
    #: tar/tar.gz/tar.bz2/tar.xz/tar.zst **全都没加** → 被占用文件静默丢失，
    #: 仍报 status=success。实测（4 文件锁 1 个）：包内只有 3 个，errors=[]。
    skipped_busy: list[str] = []

    def add_file(self, src: Path, arcname: str, mtime: float | None = None) -> int:
        if src.is_symlink():
            self.add_symlink(arcname, _safe_readlink(src), mtime)
            return 0
        # 先开文件后写头（审查 E1-1.4）：若 open 失败而头已写出，tar 流会
        # 永久错位（后续所有条目都读不出来）；顺序反了就只是一次可跳过的条目。
        #
        # 被占用/无权限时**登记到 skipped_busy 并跳过**（与 ExternalCompressor 同契约）：
        # 让其余文件照常备份，上层据此把任务标 partial 并列出清单——不再假绿。
        #
        # **必须先排除「目录被当成文件」**：Windows 上 `open(目录, "rb")` 抛的正是
        # `PermissionError`（EACCES），与「文件被 Excel 独占」同一个异常类型，
        # 只按异常类型分不开。所以先 stat 判断是不是目录 —— 目录属于**调用方错误**，
        # 必须让异常冒泡（测试 test_tar_open_failure_does_not_corrupt_stream
        # 明确要求它抛出，且该断言同时验证 tar 流不错位）。
        try:
            fh = open(src, "rb")
        except PermissionError:
            if src.is_dir():
                raise  # 目录不该走 add_file：交给调用方记错误，别当"被占用"吞掉
            self.skipped_busy.append(f"{arcname}（被占用或无权限）")
            return 0
        try:
            st = os.fstat(fh.fileno())
            ti = self._ti(arcname, mtime or st.st_mtime, (st.st_mode & 0o7777) or 0o644, tarfile.REGTYPE, st.st_size)
            self._tar.addfile(ti, _Reader(_chunks_of_fh(fh, self.progress, cancel=self._cancel)))
        finally:
            fh.close()
        return st.st_size

    def add_bytes(self, data: bytes, arcname: str, mtime: float | None = None) -> int:
        self._tar.addfile(self._ti(arcname, mtime, 0o644, tarfile.REGTYPE, len(data)), io.BytesIO(data))
        self.progress.add(len(data))
        return len(data)

    def close(self) -> None:
        self._tar.close()
        with contextlib.suppress(Exception):
            self._comp.close()
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()
                self._proc.wait(timeout=30)
        with contextlib.suppress(Exception):
            self._raw.close()
        # 事务性落地：临时名 → 最终名（P0，红队 F3）。
        # 只有走到这里（全部数据写完、流已正确收尾）才改名，
        # 强杀/异常时 `.bs-tmp-<pid>` 会被 `sweep_orphans` 认出来并清掉。
        tmp = getattr(self, "_tmp_dest", None)
        if tmp is not None and tmp.exists():
            try:
                os.replace(tmp, self.dest)
            except OSError as exc:
                raise CompressionError(
                    f"tar 产物落地失败（{tmp.name} → {self.dest.name}）：{exc}",
                    backend=self.name,
                    fix="检查目标目录是否有写权限，或杀毒软件是否锁住了文件",
                ) from exc

    def abort(self) -> None:
        with contextlib.suppress(Exception):
            self._tar.close()
        with contextlib.suppress(Exception):
            self._comp.close()
        with contextlib.suppress(Exception):
            self._raw.close()
        if self._proc is not None and self._proc.poll() is None:
            with contextlib.suppress(Exception):
                self._proc.kill()
        # 清理临时产物（不要留下半截 `.bs-tmp-<pid>`）
        for p in (getattr(self, "_tmp_dest", None), self.dest):
            if p is not None:
                with contextlib.suppress(OSError):
                    Path(p).unlink()

    def split_volumes(self) -> None:
        """通用分卷：整包生成后切成 ``.partNN``（7-Zip 执行失败时会退回这里）。"""
        vol_size = self.spec.volume_size
        if not (vol_size and self.dest.exists()):
            return
        temp_full = self.dest.with_name(self.dest.name + ".full-tmp")
        try:
            self.dest.replace(temp_full)
            self.volumes = split_file(temp_full, self.dest, vol_size)
            if not self.volumes:
                self.notes.append("内容小于分卷大小，已生成单个完整压缩包（未分卷）")
        finally:
            with contextlib.suppress(OSError):
                temp_full.unlink()


# --------------------------------------------------------------------------
# 外部程序后端（7-Zip / RAR）：tar 暂存 + 外部压缩
# --------------------------------------------------------------------------

def human_volume(size: int, suffix: str = "") -> str:
    for unit, div in (("g", 1 << 30), ("m", 1 << 20), ("k", 1 << 10)):
        if size >= div:
            value = size / div
            text = f"{int(value)}" if abs(value - int(value)) < 1e-9 else f"{value:.1f}"
            return f"{text}{unit}{suffix}"
    return f"{max(1, size // 1024)}k{suffix}"


def product_stem(name: str) -> str:
    """产物名 → 备份身份名（manifest 的 stem），由后端**正向**给出，绝不反推。

    规则（审查 W-A2-2）：
    * stem 的**唯一职责**是给出 manifest 文件名 `<stem>.json`（collect_groups 按 manifest
      建组时以 `manifest.volumes` 为准，不参与 stem 匹配），所以 stem 必须在同一目的地目录内
      **每份备份唯一**。
    * 做法：先去掉**尾部**的卷序号后缀（`.001` / `.part01` / `.part1`），剩下就是主名。
    * 关键：**不按扩展名长度做前缀截断**。任务名含扩展名时（任务名 `foo.zip` →
      产物 `foo.zip_20261023_042116.zip`），旧的 `str.find(ext)` 首匹配会把 stem 截成
      `foo.zip` → 该任务的所有备份共用一个 manifest 互相覆盖（实测 2 产物 1 清单）。
      这里 `foo.zip_20261023_042116.zip` 的 stem = **完整文件名**，manifest 文件名 =
      `stem + ".json"`，天然一包一清单。
    * 分卷场景（`.partNN` / `.001`）stem 仍是"去掉卷后缀的主名"，与 manifest.volumes
      里各卷的公共主名一致（`X.zip.001` → `X.zip`；`X.tar.gz.part01` → `X.tar.gz`）。
    """
    p = Path(name)
    base = p.name
    low = base.lower()
    # 1) 尾部纯数字卷后缀：.001 / .0001 / .part01 / .part1
    for sep in (".part", "."):
        idx = low.rfind(sep)
        if idx > 0 and low[idx + len(sep) :].isdigit():
            return base[:idx]
    # 2) 无卷后缀：主名就是整个文件名（不去扩展名——见 docstring）
    return base


def split_file(src: Path, dest_base: Path, volume_size: int) -> list[VolumeInfo]:
    """把整包切成 ``<dest_base>.partNN`` 分卷文件。

    内容不足一个分卷时不切分：直接把整包改回目标文件名，
    避免产生只有一个 ``.part01`` 的「伪分卷」。
    """
    volumes: list[VolumeInfo] = []
    idx = 1
    with open(src, "rb") as fh:
        while True:
            chunk = fh.read(volume_size)
            if not chunk:
                break
            part = dest_base.with_name(f"{dest_base.name}.part{idx:02d}")
            part.write_bytes(chunk)
            volumes.append(VolumeInfo(part, len(chunk)))
            idx += 1
    if len(volumes) <= 1:
        with contextlib.suppress(OSError):
            for v in volumes:
                v.path.unlink()
            src.replace(dest_base)
        return []
    return volumes


class ExternalCompressor:
    """所有依赖外部可执行文件的压缩后端基类。"""

    #: 是否给子进程追加 `-sccUTF-8`（仅 7-Zip 系可执行文件支持该开关）。
    #: 中文 Windows 上 7za 的报错按 GBK 输出，而本产品一律 UTF-8 解码 → 乱码；
    #: 加上本开关后 7za 全量输出 UTF-8，现有解码原样正确（审查 W-A3-2 / M2b）。
    _SCC_UTF8 = False

    def __init__(self, dest_path: Path, spec: ArchiveSpec, progress: Progress) -> None:
        # 统一解析成绝对路径：本类会把子进程 cwd 切到别处（见 _popen/_pack_from），
        # 相对路径在子进程里会指向错误位置（审查 D：产物被写进 src_dir 内部）。
        dest_path = Path(dest_path).resolve()
        self.dest = dest_path
        self.spec = spec
        self.progress = progress
        self._warnings: list[str] = []
        self.notes: list[str] = []
        # 同上：类体里的 skipped_busy 是共享列表，必须在实例上重建
        self.skipped_busy = []
        self.volumes: list[VolumeInfo] = []
        # 暂存目录放在目标同卷，规避 7z 跨卷写入失败并节省一次整包复制
        self.tmpdir = make_staging_dir(dest_path.parent)
        self.staging_path = self.tmpdir / "staging.tar"
        self._fh = open(self.staging_path, "wb")
        self._tar = tarfile.open(fileobj=self._fh, mode="w|", format=tarfile.PAX_FORMAT, bufsize=1 << 20)
        self._proc: subprocess.Popen | None = None
        self.exe = ""
        self._cancel: Callable[[], bool] | None = None

    def set_cancel(self, cancel: Callable[[], bool] | None) -> None:
        """登记取消回调：写入阶段可随时中断。"""
        self._cancel = cancel

    # ---- 暂存阶段
    @property
    def warnings(self) -> list[str]:
        return self._warnings

    def _ti(self, arcname: str, mtime: float | None, size: int) -> tarfile.TarInfo:
        ti = tarfile.TarInfo(arcname)
        ti.size = size
        ti.mtime = int(mtime if mtime else time.time())
        return ti

    def add_dir(self, arcname: str, mtime: float | None = None) -> None:
        ti = self._ti(arcname.rstrip("/") + "/", mtime, 0)
        ti.type = tarfile.DIRTYPE
        ti.mode = 0o755
        self._tar.addfile(ti)

    def add_symlink(self, arcname: str, target: str, mtime: float | None = None) -> None:
        ti = self._ti(arcname, mtime, 0)
        ti.type = tarfile.SYMTYPE
        ti.linkname = target
        ti.mode = 0o777
        self._tar.addfile(ti)

    #: 记录「因被占用而跳过」的文件（供上层汇总到 warnings/errors）
    skipped_busy: list[str] = []

    def add_file(self, src: Path, arcname: str, mtime: float | None = None) -> int:
        """把文件写进 tar 暂存流。

        **必须在写 TarInfo 头之前确认文件可读**（用户测试 P0）：

        ``_chunks_of()`` 是惰性迭代器，``tarfile.addfile()`` 会**先写 512 字节头**
        再消费它。若 ``open(src)`` 此时抛 ``PermissionError``（文件被 Excel/PSD/IDE
        独占），头已经写进流里、而数据一个字节都没写，**整条 tar 流从此错位**：
        后续所有条目偏移，解包后表现为「中间文件名配下一个文件的内容」+
        「后面若干文件凭空消失」，而外层 ``status=success / verification=passed``。

        实测（锁住 5 个文件中的第 3 个，修复前）：产物只剩 3 条，
        file3 内容是 ``b'src/file4.txt\\x00...'``（下一个文件的名字），
        file4/file5 完全消失。

        **修法**：先 ``open()`` 试探。
        * 成功 → 用同一个句柄写数据（不留 TOCTOU 窗口）；
        * 失败（被占用/无权限）→ **跳过该文件并登记到 ``skipped_busy``**，
          让其余文件照常备份。上层据此把任务标为 ``partial`` 并列出缺失清单
          ——既不会假绿，也不会因为一个打开着的 Excel 让整次备份白跑。
        """
        if self._cancel is not None and self._cancel():
            raise ArchiveCancelled()
        if src.is_symlink():
            self.add_symlink(arcname, _safe_readlink(src), mtime)
            return 0
        try:
            st = src.stat()
        except OSError as exc:
            self.skipped_busy.append(f"{arcname}（无法读取属性：{type(exc).__name__}）")
            return 0
        # 探测可读性：失败必须在写 tar 头之前发生
        try:
            probe = open(src, "rb")
        except OSError:
            self.skipped_busy.append(f"{arcname}（被占用或无权限）")
            return 0
        try:
            ti = self._ti(arcname, mtime or st.st_mtime, st.st_size)
            ti.mode = st.st_mode & 0o7777
            self._tar.addfile(ti, _Reader(_chunks_of_fh(probe, self.progress, cancel=self._cancel)))
        finally:
            with contextlib.suppress(OSError):
                probe.close()
        return st.st_size

    def add_bytes(self, data: bytes, arcname: str, mtime: float | None = None) -> int:
        ti = self._ti(arcname, mtime, len(data))
        ti.mode = 0o644
        self._tar.addfile(ti, io.BytesIO(data))
        self.progress.add(len(data))
        return len(data)

    def _finish_staging(self) -> Path:
        self._tar.close()
        self._fh.close()
        return self.staging_path

    def _unpack_staging(self, tar_path: Path) -> Path:
        """把 staging.tar 解开为目录（7-Zip 不会自动展开 tar，必须先解再打包）。

        返回解包目录；随后删除 staging.tar 控制磁盘峰值（解包后即释源包）。

        **优先用 Python ``tarfile``，只在它失败时才回退到 7-Zip**——原因（实测）：

        p7zip 16.02（Debian/Ubuntu 的 ``p7zip-full``，CI 的 ubuntu-22.04 就是它）
        用 ``7z x staging.tar`` 解包时，会把**非 ASCII 文件名替换成 ``?``**：

            Python tarfile 建包    → data/sub/中文 emoji 🎈.txt
            7z x staging.tar       → data/sub/?? emoji ?.txt      ← 中文变 ?，不可逆
            7z x -sccUTF-8 ...     → data/sub/?? emoji ?.txt      ← 同样丢
            Python tarfile.extract → data/sub/中文 emoji 🎈.txt    ← 正确

        后果：备份产物里的中文文件名全部损坏，用户恢复后文件名不可读。
        ``-sccUTF-8`` 只影响**控制台输出**编码，不影响文件名，所以治不了这个问题。

        7-Zip 21+（官方）正常，本机 24.09 / VM 25.01 都测不出问题，因此长期未被发现。

        staging.tar 是**我们自己用 ``tarfile`` 写的**，格式确定，``tarfile`` 必然能读；
        回退到 7-Zip 只为止损（比如未来的 tar 变体 tarfile 解析不了）。
        """
        unpacked = self.tmpdir / "unpacked"
        unpacked.mkdir(parents=True, exist_ok=True)
        if self._unpack_with_tarfile(tar_path, unpacked):
            with contextlib.suppress(OSError):
                tar_path.unlink()
            return unpacked
        # 回退：tarfile 解不开（格式意外）时仍用 7-Zip，至少能出产物
        self._run([self.exe, "x", str(tar_path), f"-o{unpacked}", "-y", "-bso0", "-bsp0"])
        with contextlib.suppress(OSError):
            tar_path.unlink()
        return unpacked

    @staticmethod
    def _unpack_with_tarfile(tar_path: Path, dest: Path) -> bool:
        """用 tarfile 解 staging.tar；成功返回 True。

        为什么要自己实现而不用 ``tf.extractall``：
        * ``filter="data"`` 是 Python 3.12+ 才有（本产品声明支持 >=3.10）；
        * 需要**完整保留**符号链接、硬链接、权限位、mtime —— 这些都是
          用户备份的一部分，丢了就是静默的数据/元数据丢失。

        安全要点：staging.tar 是我们自己写的，理论上不会有越界成员，
        但解包前仍逐个校验收敛到 dest 内（防御 tar-slip）。链接另做
        落点校验：``linkname`` 解析后必须仍在 dest 内。
        """
        try:
            with tarfile.open(tar_path, "r") as tf:
                base = dest.resolve()
                for member in tf.getmembers():
                    target = (dest / member.name).resolve()
                    try:
                        target.relative_to(base)
                    except ValueError:
                        # 越界成员：跳过而不是让整次解包失败
                        continue

                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        SevenZipCompressor._restore_meta(target, member)
                        continue

                    # 符号链接 / 硬链接：**必须保留**（审计 P0）——
                    # 早期版本这里 `continue` 静默丢弃，导致 7z/zip/rar
                    # 三种后端的产物里链接全部消失，用户恢复后链接指向的
                    # 文件不可达，而记录显示 success。
                    if member.issym():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with contextlib.suppress(OSError, NotImplementedError):
                            if target.is_symlink() or target.exists():
                                target.unlink()
                            # 落点校验：相对 linkname 相对 target 的父目录解析
                            link_dest = (target.parent / member.linkname).resolve()
                            try:
                                link_dest.relative_to(base)
                            except ValueError:
                                continue  # 指向 dest 之外：跳过该链接
                            os.symlink(member.linkname, target)
                            SevenZipCompressor._restore_meta(target, member, follow=False)
                        continue

                    if member.islnk():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        src_rel = (dest / member.linkname).resolve()
                        try:
                            src_rel.relative_to(base)
                        except ValueError:
                            continue
                        with contextlib.suppress(OSError, NotImplementedError):
                            if target.exists() or target.is_symlink():
                                target.unlink()
                            os.link(src_rel, target)
                        continue

                    if member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        src = tf.extractfile(member)
                        if src is None:
                            continue
                        with open(target, "wb") as out:
                            shutil.copyfileobj(src, out, 1 << 20)
                        SevenZipCompressor._restore_meta(target, member)
            return True
        except Exception:  # noqa: BLE001 - 任何失败都交给 7-Zip 回退
            return False

    @staticmethod
    def _restore_meta(target: Path, member: tarfile.TarInfo, *, follow: bool = True) -> None:
        """还原权限位与 mtime（审计 P0：不还原会让 0600 私钥变 0644）。

        失败时静默忽略：某些文件系统（FAT/exFAT/部分网络盘）不支持 chmod，
        不能因此中断整次备份。
        """
        mode = member.mode & 0o7777
        if mode:
            with contextlib.suppress(OSError, NotImplementedError):
                if follow:
                    os.chmod(target, mode)
                elif hasattr(os, "lchmod"):
                    os.lchmod(target, mode)
        with contextlib.suppress(OSError, NotImplementedError, ValueError):
            os.utime(target, (member.mtime, member.mtime), follow_symlinks=follow)

    def _pack_from(self, src_dir: Path, out_path: Path, *, extra_args: list[str] | None = None) -> None:
        """从解包目录打包到 out_path（cwd=源目录保证归档内路径与 arcname 一致）。

        **必须 resolve()**：这里把 ``cwd`` 设成 ``src_dir``，7z 会以该 cwd 解析相对路径。
        若 ``out_path`` 是相对路径，产物会写进 ``src_dir`` **内部**，随后 ``_commit()``
        在最终路径上找不到文件 → 抛 CompressionError → 上层 ``force`` 回退到内置引擎，
        但记录里的 ``backend`` 仍写 ``7z:7za.exe``（账实不符，审查 D）。

        **必须加 ``-snl``（S0，元数据专家实测）**：不加时 7-Zip 会把符号链接
        **解引用成内容副本**；更糟的是**断链让整次备份直接失败**（rc=1，产物不生成）。
        元数据专家抓到了产品喂给 7z 的中间 staging.tar，确认链接在 tar 里是对的
        （``type="2", issym=True, linkname="target.txt"``）—— 问题全在这一步。
        Linux 上断链极常见（包管理器残留 / 临时目录被清），从 tar 换 7z 就突然失败。
        """
        if self._cancel is not None and self._cancel():
            raise ArchiveCancelled()
        out_path = Path(out_path).resolve()
        argv = [self.exe, "a", "-snl", *self._args(), *(extra_args or []), str(out_path), "."]
        self._run(argv, cwd=src_dir)

    def _commit(self, tmp_dest: Path) -> None:
        """事务性落地：校验临时产物后原子改名到最终名。"""
        if not _is_valid_archive(tmp_dest):
            raise CompressionError(
                f"7-Zip 未能写入压缩包（{tmp_dest.name} 为空或不可读）",
                backend=self.name,
                fix=(
                    "该目录可能被系统或安全软件限制写入：请换一个备份位置（例如用户目录下的文件夹），"
                    "或勾选「强制压缩」改用内置引擎"
                ),
            )
        os.replace(tmp_dest, self.dest)

    # ---- 压缩阶段
    def close(self) -> None:
        try:
            tar_path = self._finish_staging()
            # 被占用而跳过的文件：写进 warnings，让上层能如实告知用户
            # （不能静默吞掉——用户会以为全部备份成功）
            if self.skipped_busy:
                self._warnings.append(
                    f"有 {len(self.skipped_busy)} 个文件因被占用/无权限而未备份："
                    + "；".join(self.skipped_busy[:5])
                    + ("…" if len(self.skipped_busy) > 5 else "")
                )
            self.progress.describe("compress")
            src_dir = self._unpack_staging(tar_path)
            mode = self.spec.multivolume
            if mode == "native":
                self._run_native_volumes(src_dir)
            else:
                self._run_single(src_dir)
            # 通用分卷：由 create_archive 在收尾时调用 split_volumes
        finally:
            if not force_rmtree(self.tmpdir):
                log.warning(
                    "暂存目录未能删净（可能仍含明文，请手动检查）：%s", self.tmpdir
                )

    def abort(self) -> None:
        with contextlib.suppress(Exception):
            self._tar.close()
        with contextlib.suppress(Exception):
            self._fh.close()
        if self._proc is not None and self._proc.poll() is None:
            with contextlib.suppress(Exception):
                self._proc.kill()
        if not force_rmtree(self.tmpdir):
            log.warning("abort 后暂存目录未能删净（可能仍含明文）：%s", self.tmpdir)

    def _with_scc(self, argv: list[str]) -> list[str]:
        """在子命令后插入 `-sccUTF-8`（仅 7-Zip 系可执行文件）。

        形如 `[exe, "a", ...]` → `[exe, "a", "-sccUTF-8", ...]`。已存在或形态不符
        时原样返回，重复调用幂等。
        """
        if not (self._SCC_UTF8 and sys.platform == "win32"):
            return argv
        if len(argv) < 2 or "-sccUTF-8" in argv:
            return argv
        if not Path(argv[0]).name.lower().startswith("7z"):
            return argv
        return [argv[0], argv[1], "-sccUTF-8", *argv[2:]]

    def _popen(self, argv: list[str], cwd: str | None = None) -> subprocess.Popen:
        """启动外部压缩程序。

        部分受限环境（沙箱、低完整性）下 7z 子进程访问系统临时目录会被拒绝，
        这里给子进程显式设置 TMP/TEMP 到可写的暂存目录。

        7-Zip 后端统一追加 `-sccUTF-8`（放在子命令之后、其它开关之前）：
        否则中文 Windows 的错误输出是 GBK，而解码端是 UTF-8，用户看到的是乱码
        （审查 W-A3-2）。RAR 系不支持该开关，由 `_SCC_UTF8=False` 排除。
        """
        argv = self._with_scc(argv)
        kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        tmpdir = str(self.tmpdir)
        env = dict(os.environ, LC_ALL="C", LANG="C", TMP=tmpdir, TEMP=tmpdir, TMPDIR=tmpdir)
        kwargs["env"] = env
        if cwd is not None:
            kwargs["cwd"] = cwd
        else:
            parent = Path(self.dest).parent
            if parent.is_dir() and same_volume(parent, self.tmpdir):
                kwargs["cwd"] = str(parent)
        self._proc = subprocess.Popen(argv, **kwargs)
        return self._proc

    def _run(self, argv: list[str], cwd: str | None = None) -> str:
        proc = self._popen(argv, cwd=cwd)
        if self._cancel is not None:
            # 边等待边检查取消：外部压缩（也是最慢的一步）必须可中断
            while True:
                try:
                    out, _ = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if self._cancel():
                        with contextlib.suppress(Exception):
                            proc.kill()
                        with contextlib.suppress(Exception):
                            proc.communicate(timeout=5)
                        raise ArchiveCancelled() from None
        else:
            out, _ = proc.communicate()
        text = (out or b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            raise CompressionError(
                f"{Path(argv[0]).name} 退出码 {proc.returncode}：{text.strip()[-800:]}",
                backend=self.name,
            )
        return text

    # ---- 子类实现
    def _run_single(self, tar_path: Path) -> None:
        raise NotImplementedError

    def _run_native_volumes(self, tar_path: Path) -> None:
        self._run_single(tar_path)

    def split_volumes(self) -> None:
        """通用分卷：整包生成后按 volume_size 切片（由 create_archive 在收尾时调用）。"""
        full = self.dest
        if not (self.spec.volume_size and full.exists()):
            return
        temp_full = full.with_name(full.name + ".full-tmp")
        try:
            full.replace(temp_full)
            self.volumes = split_file(temp_full, full, self.spec.volume_size)
            if not self.volumes:
                self.notes.append("内容小于分卷大小，已生成单个完整压缩包（未分卷）")
        finally:
            with contextlib.suppress(OSError):
                temp_full.unlink()


class SevenZipCompressor(ExternalCompressor):
    """7-Zip 后端，同时服务 zip 与 7z。"""

    _SCC_UTF8 = True

    _METHOD = {0: "copy", 1: "lzma2:1", 3: "lzma2:5", 5: "lzma2:9", 7: "lzma2:12", 9: "lzma2:19"}

    def __init__(self, dest_path: Path, spec: ArchiveSpec, progress: Progress, exe: str | None = None) -> None:
        super().__init__(dest_path, spec, progress)
        self.exe = exe or find_7z() or ""
        if not self.exe:
            self.abort()
            raise BackendUnavailable("未找到 7-Zip", fix="安装 7-Zip 或把 7z 加入 PATH")
        self.name = f"7z:{Path(self.exe).name}"

    def _args(self) -> list[str]:
        spec = self.spec
        args: list[str] = ["-bd", "-y"]
        if spec.fmt == "7z":
            method = min(self._METHOD, key=lambda k: abs(k - spec.level))
            args += ["-t7z", f"-m0={self._METHOD[method]}"]
            if spec.password and spec.encrypt_mode == "aes256":
                args += [f"-p{spec.password}", "-mhe=on"]
        else:
            args += ["-tzip", f"-mx={max(0, min(9, spec.level))}"]
            if spec.password:
                if spec.encrypt_mode == "aes256":
                    args += [f"-p{spec.password}", "-mem=AES256"]
                else:
                    args += [f"-p{spec.password}", "-mem=ZipCrypto"]
                    self.notes.append("ZIP 使用 ZipCrypto 传统加密（兼容性好但强度弱）")
        return args

    def _run_single(self, src_dir: Path) -> None:
        tmp_dest = self.dest.with_name(f"{self.dest.name}.bs-tmp-{os.getpid()}")
        try:
            self._pack_from(src_dir, tmp_dest)
            self._commit(tmp_dest)
        finally:
            with contextlib.suppress(OSError):
                tmp_dest.unlink()

    def _run_native_volumes(self, src_dir: Path) -> None:
        # native 分卷维持直接写最终名（组事务留待 manifest 批次）
        stem = self.dest.name
        for n in range(1, 10000):
            p = self.dest.with_name(f"{stem}.{n:03d}")
            if not p.exists():
                break
            p.unlink()
        argv = [self.exe, "a", *self._args(), f"-v{human_volume(self.spec.volume_size)}", str(self.dest), "."]
        self._run(argv, cwd=src_dir)
        idx = 1
        while True:
            p = self.dest.with_name(f"{stem}.{idx:03d}")
            if not p.exists():
                break
            self.volumes.append(VolumeInfo(p, p.stat().st_size))
            idx += 1
        if not self.volumes or not _is_valid_archive(self.volumes[0].path):
            raise CompressionError("7-Zip 分卷失败或首个分卷无效", backend=self.name)
        if len(self.volumes) == 1:
            # 数据量没到分卷大小：7-Zip 只生成 .001，没有意义，改回普通整包
            only = self.volumes[0].path
            self.volumes = []
            with contextlib.suppress(OSError):
                only.replace(self.dest)


def _guess_output_path(argv: list[str]) -> Path | None:
    """从命令行里找出要生成的归档文件（a 命令后第一个非选项参数）。"""
    try:
        idx = argv.index("a")
    except ValueError:
        return None
    for arg in argv[idx + 1 :]:
        if arg.startswith("-"):
            continue
        if arg.lower().endswith((".tar", ".gz", ".bz2", ".xz", ".zst")):
            continue
        return Path(arg)
    return None


_ARCHIVE_MAGIC = (
    b"7z\xbc\xaf\x27\x1c",
    b"PK\x03\x04",
    b"Rar!\x1a\x07",
    b"ustar",
    b"\x1f\x8b",
    b"BZh",
    b"\xfd7zXZ\x00",
    b"\x28\xb5\x2f\xfd",
)


def _is_valid_archive(path: Path) -> bool:
    """判断输出文件是否是一个有效的压缩包（含 7z 分卷 .001 无签名的情形）。"""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False
    if path.suffix[1:].isdigit() and len(path.suffix) == 4:  # .001 分卷：首卷无文件头
        return True
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return False
    return any(head.startswith(m) for m in _ARCHIVE_MAGIC) or b"ustar" in head[:512]


def probe_encrypted(path: Path, fmt: str, password: str | None = None) -> bool | None:
    """事后实测产物是否真的加密（None = 无法判定）。

    * zip：读首条目 flag_bits 的加密位（0x1）；注意：不能按字节搜 AES 标识——
      7z 的文件头本身也含 AES，会误判（见红队报告 E5 §1.2）。
    * 7z：优先 py7zr 打开检查内容 coder chain 是否含 AES filter；
      无 py7zr 时用 7za 列归档扫 7zAES 标记（字节级判定不可靠，此项为辅助）。
    * tar 系：恒 False（无加密实现）。
    """
    try:
        if fmt == "zip":
            import zipfile

            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
                if not infos:
                    return None
                any_enc = any(bool(i.flag_bits & 0x1) for i in infos if not i.is_dir())
                return any_enc
        if fmt == "7z":
            if py7zr is not None:
                try:
                    with py7zr.SevenZipFile(str(path), "r", password=password) as z:
                        folders = z.header.main_streams.unpackinfo.folders
                        aes_bytes = py7zr.FILTER_CRYPTO_AES256_SHA256.to_bytes(4, "big")
                        for fo in folders:
                            for coder in fo.coders:
                                method = coder.get("method")
                                if isinstance(method, bytes) and method == aes_bytes:
                                    return True
                        return False
                except Exception:  # noqa: BLE001 - 打不开则交给外部探针
                    pass
            exe = find_7z()
            if exe:
                argv = [exe, "l", "-slt", str(path)]
                if password:
                    argv.insert(2, f"-p{password}")
                # -sccUTF-8：让 7za 在中文 Windows 上按 UTF-8 输出（否则 GBK 字节被
                # UTF-8 硬解成乱码，审查 W-A3-2）；插在子命令之后、其它参数之前。
                if sys.platform == "win32" and Path(exe).name.lower().startswith("7z"):
                    argv.insert(2, "-sccUTF-8")
                kwargs: dict = {"capture_output": True, "timeout": 60, "stdin": subprocess.DEVNULL}
                if sys.platform == "win32":
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                proc = subprocess.run(argv, **kwargs)
                out = (proc.stdout or b"").decode("utf-8", "replace")
                if proc.returncode != 0:
                    return None
                if "7zAES" in out or "Encrypted = +" in out:
                    return True
                if "Encrypted = -" in out:
                    return False
            return None
        if fmt.startswith("tar"):
            return False
    except Exception:  # noqa: BLE001 - 探测失败不给结论
        return None
    return None


#: 分卷拼接实测的最大体积（超出则不拼接，直接判为"未实测"）——避免为大归档制造
#: 一份完整副本的磁盘峰值。拼接口径与 engine._verify_volumes 一致。
_PROBE_JOIN_MAX_BYTES = 2 * 1024 ** 3


def probe_encrypted_volumes(volumes: list, fmt: str, password: str | None = None) -> bool | None:
    """对（可能是分卷的）产物实测加密状态，返回 None 表示无法判定。

    首卷（`.partNN` / `.001`）不是合法容器，直接 probe 只会得到 None——旧代码因此
    回退"声明值"，于是分卷 + 密码组合能把明文记成已加密（审查 W-A3-3，S0）。
    这里先按卷序拼接成临时整包再 probe，把三态变成"实测"而不是"猜测"。
    """
    paths = [Path(getattr(v, "path", v)) for v in (volumes or [])]
    if not paths:
        return None
    if len(paths) == 1:
        return probe_encrypted(paths[0], fmt, password)
    # 原生 7z 卷（.001 集）：7za 能自动串联，先就地试一次
    first = probe_encrypted(paths[0], fmt, password)
    if first is not None:
        return first
    try:
        total = sum(p.stat().st_size for p in paths)
        if total > _PROBE_JOIN_MAX_BYTES:
            return None
        import tempfile

        with tempfile.TemporaryDirectory(prefix="bs-probe-") as td:
            joined = Path(td) / f"joined{FORMAT_EXTENSIONS.get(fmt, '.bin')}"
            with open(joined, "wb") as out:
                for p in paths:
                    with open(p, "rb") as fh:
                        shutil.copyfileobj(fh, out, 1 << 20)
            return probe_encrypted(joined, fmt, password)
    except OSError:
        return None


class RarCompressor(ExternalCompressor):
    _METHOD = {0: "0", 1: "1", 3: "3", 5: "5", 7: "5", 9: "5"}

    def __init__(self, dest_path: Path, spec: ArchiveSpec, progress: Progress, exe: str | None = None) -> None:
        super().__init__(dest_path, spec, progress)
        self.exe = exe or find_rar() or ""
        if not self.exe:
            self.abort()
            raise BackendUnavailable("未找到 RAR 可执行文件", fix="安装 WinRAR / RAR")
        self.name = f"rar:{Path(self.exe).name}"

    def _args(self) -> list[str]:
        spec = self.spec
        m = min(self._METHOD, key=lambda k: abs(k - spec.level))
        args = ["-idq", "-ep1", "-y", f"-m{self._METHOD[m]}"]
        if spec.password and spec.encrypt_mode == "aes256":
            args += [f"-p{spec.password}", "-hp"]  # -hp 同时加密文件名
        return args

    def _run_single(self, src_dir: Path) -> None:
        tmp_dest = self.dest.with_name(f"{self.dest.name}.bs-tmp-{os.getpid()}")
        try:
            self._pack_from(src_dir, tmp_dest)
            if not tmp_dest.exists():
                raise CompressionError("RAR 未生成压缩包", backend=self.name)
            self._commit(tmp_dest)
        finally:
            with contextlib.suppress(OSError):
                tmp_dest.unlink()

    def _run_native_volumes(self, src_dir: Path) -> None:
        # native 分卷维持直接写最终名（组事务留待 manifest 批次）
        spec = self.spec
        argv = [self.exe, "a", *self._args(), f"-v{human_volume(spec.volume_size, 'b')}", str(self.dest), "."]
        self._run(argv, cwd=src_dir)
        for p in sorted(self.dest.parent.glob(self.dest.with_suffix("").name + ".part*.rar")):
            if p.name.startswith(self.dest.with_suffix("").name + ".part"):
                self.volumes.append(VolumeInfo(p, p.stat().st_size))


# --------------------------------------------------------------------------
# 后端选择
# --------------------------------------------------------------------------

@dataclass
class BackendChoice:
    name: str
    factory: Callable[[Path, ArchiveSpec, Progress], BaseCompressor] | None
    note: str = ""


def rar_available() -> bool:
    rar = find_rar()
    if not rar:
        return False
    try:
        return probe_unrar_trial()[0]
    except Exception:
        return False


def backend_for_format(fmt: str, force: bool = False) -> BackendChoice:
    """按格式挑选后端；`force=True` 时允许降级到内置实现。"""
    info = get_platform_info()
    if fmt == "zip":
        exe = find_7z()
        if exe:
            return BackendChoice(f"7z:{Path(exe).name}", lambda d, s, p, _e=exe: SevenZipCompressor(d, s, p, _e))
        return BackendChoice("python-zipfile", lambda d, s, p: ZipCompressor(d, s, p))
    if fmt == "7z":
        exe = find_7z()
        if exe:
            return BackendChoice(f"7z:{Path(exe).name}", lambda d, s, p, _e=exe: SevenZipCompressor(d, s, p, _e))
        if py7zr is not None:
            if force:
                return BackendChoice(
                    "py7zr-forced",
                    lambda d, s, p: _TarStreamTo7z(d, s, p),
                    note="强制模式：使用内置 py7zr 写入 7z（不支持 7z 原生分卷，将改用通用切片分卷）",
                )
            raise BackendUnavailable(
                "未找到 7-Zip（7z / 7za），无法创建 .7z",
                fix=f"安装 7-Zip：{info.install_command('7zip')}；或勾选「强制压缩」使用内置 py7zr（不支持原生分卷）",
            )
        raise BackendUnavailable(
            "缺少 7-Zip，且未安装 py7zr 后备模块",
            fix="安装 7-Zip；或 pip install py7zr 后使用强制压缩",
        )
    if fmt == "rar":
        if rar_available():
            return BackendChoice("rar-native", lambda d, s, p: RarCompressor(d, s, p))
        if force:
            # 强制 RAR 的产出容器：优先「真 ZIP + .rar 扩展名」（任何解压软件都能打开），
            # 其次才是内置 7z 容器。两者都会在结果里明确提示。
            exe = find_7z()
            if exe:
                return BackendChoice(
                    "rar-as-zip-forced",
                    lambda d, s, p, _e=exe: SevenZipCompressor(d, _as_zip_spec(s), p, _e),
                    note="强制模式：未检测到可写 RAR 的引擎，改用 ZIP 容器（扩展名仍为 .rar，普通解压软件可打开）",
                )
            from .py7zr_backend import ZipFileCompressor

            return BackendChoice(
                "rar-as-zipfile-forced",
                lambda d, s, p: ZipFileCompressor(d, _as_zip_spec(s), p),
                note="强制模式：改用内置 ZIP 引擎（扩展名 .rar，内容是标准 ZIP，任何解压软件都能打开）",
            )
        raise BackendUnavailable(
            "未检测到可创建压缩包的 RAR（WinRAR / RAR 为专有软件，需自行安装并同意其授权）",
            fix=(
                f"安装 WinRAR/RAR：{info.install_command('rar')}；或改用 7z / zip；"
                "也可勾选「强制压缩」降级为 ZIP 容器"
            ),
        )
    if fmt in TAR_FORMATS:
        codec = fmt.split(".", 1)[1] if "." in fmt else ""
        if codec == "zst" and _zstd_mod is None and not shutil.which("zstd"):
            raise BackendUnavailable(
                "创建 tar.zst 需要 zstandard 模块或系统 zstd",
                fix="pip install zstandard 或安装 zstd",
            )
        return BackendChoice("python-tarfile", lambda d, s, p: TarStreamCompressor(d, s, p))
    raise BackendUnavailable(f"不支持的格式: {fmt}")


class _TarStreamTo7z:
    """强制模式的内置后端包装：7z 走 py7zr，ZIP 走标准库 zipfile。

    注意：Py7zrCompressor 对 ``spec.fmt == "zip"`` 会改用 zipfile，
    因此调用方必须传入**正确的 fmt**（不能把 ZIP 请求交给 7z 容器）。
    """

    name = "py7zr"

    def __init__(self, dest_path: Path, spec: ArchiveSpec, progress: Progress) -> None:
        from .py7zr_backend import Py7zrCompressor  # 延迟导入，避免无 py7zr 时报错

        self.dest = dest_path
        self.inner = Py7zrCompressor(dest_path, spec, progress)
        self.name = getattr(self.inner, "name", "py7zr")

    @property
    def warnings(self) -> list[str]:
        return self.inner.warnings

    @property
    def notes(self) -> list[str]:
        return self.inner.notes

    @property
    def volumes(self) -> list:
        return []

    def add_dir(self, arcname: str, mtime: float | None = None) -> None:
        self.inner.add_dir(arcname, mtime)

    def add_file(self, src: Path, arcname: str, mtime: float | None = None) -> int:
        return self.inner.add_file(src, arcname, mtime)

    def add_symlink(self, arcname: str, target: str, mtime: float | None = None) -> None:
        self.inner.add_symlink(arcname, target, mtime)

    def add_bytes(self, data: bytes, arcname: str, mtime: float | None = None) -> int:
        return self.inner.add_bytes(data, arcname, mtime)

    def set_cancel(self, cancel) -> None:  # noqa: ANN001
        setter = getattr(self.inner, "set_cancel", None)
        if callable(setter):
            setter(cancel)

    def close(self) -> None:
        try:
            self.inner.close()
        except Exception as exc:  # noqa: BLE001
            from .py7zr_backend import _Cancelled

            if isinstance(exc, _Cancelled):
                raise ArchiveCancelled() from None
            raise

    def abort(self) -> None:
        self.inner.abort()


def _as_zip_spec(spec: ArchiveSpec) -> ArchiveSpec:
    """强制 RAR 时把配置改成 ZIP 容器（扩展名仍为 .rar）。"""
    import dataclasses

    return dataclasses.replace(spec, fmt="zip")


def _as_py7zr_spec(spec: ArchiveSpec) -> ArchiveSpec:
    """强制 RAR 且没有 7-Zip 时：内置引擎只能写 7z / tar，因此写 7z 容器但保留 .rar 扩展名。"""
    import dataclasses

    return dataclasses.replace(
        spec,
        fmt="7z",
        encrypt_mode="aes256" if spec.password else None,
    )


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

def multivolume_mode_for(fmt: str, multivolume: str | None) -> str | None:
    """把用户选择的分卷模式映射为后端实际模式。"""
    if not multivolume:
        return None
    if multivolume == "7z-split":
        return "native" if fmt == "7z" else "split"
    if multivolume == "rar-native":
        return "native" if fmt == "rar" else "split"
    return "split"


def create_archive(
    spec: ArchiveSpec,
    sources: Iterable[Item],
    progress: Progress | None = None,
    cancel: Callable[[], bool] | None = None,
) -> ArchiveResult:
    """执行一次压缩，返回结果。

    参数：
        spec     : 压缩配置（目标路径、格式、密码、分卷…）
        sources  : 已规划的条目（来自 ``plan_items``）
        progress : 进度回调
        cancel   : 返回 True 时取消
    """
    started = time.monotonic()
    progress = progress or Progress()
    fmt = spec.fmt
    if fmt not in SUPPORTED_FORMATS:
        raise BackendUnavailable(f"不支持的压缩格式：{fmt}")

    # ---- 加密前置守卫：在挑选后端、创建任何文件之前完成。这样 CLI / GUI / JSON 导入
    #      全路径拿到同一个明确文案，而不是各自撞出乱码或"静默产出明文包"。
    encrypt_mode = spec.encrypt_mode if spec.password else None
    if spec.password and not encrypt_mode:
        raise PasswordRequired("设置了密码但未指定加密方式", fix="选择 AES-256 或 ZipCrypto")
    if spec.password and encrypt_mode == "gpg":
        # gpg 加密尚未实现（fail-closed：静默产出明文比报错更危险）
        raise BackendUnavailable(
            "GPG 加密尚未实现：本次未创建归档（不会产出明文包）",
            fix="改用 AES-256（7z/zip/rar）加密，或关闭密码保护",
        )
    # 7z 容器没有 ZipCrypto 概念（那是 ZIP 的传统算法）。此前 `SevenZipCompressor._args`
    # 只在 aes256 时加 -p，zipcrypto 请求被静默丢密码 → 产出**明文**包；分卷形态下
    # probe 打不开首卷返回 None，旧代码又按"声明"回填 encrypted=True，
    # 记录撒谎（用户以为已加密，实际无密码可解）。审查 W-A3-3，S0：创建前直接拒绝。
    if spec.password and fmt == "7z" and encrypt_mode not in (None, "", "aes256"):
        raise PasswordRequired(
            "7z 容器不支持 ZipCrypto 加密（ZipCrypto 是 ZIP 的传统算法），"
            "本次未创建归档（不会产出未加密的冒名包），请使用 AES-256 或改用 zip 格式",
            fix="把加密方式改为 AES-256，或把格式改为 zip 再选 ZipCrypto",
        )
    # 7-Zip 对 ZIP 密码取 ANSI/GBK 单字节口径：任何含非 ASCII 字符的 zip 密码在
    # 创建时必然 rc=2 失败（`é` 单字符即败，与代码页/长度无关），且错误信息是乱码。
    # 与其等到子进程失败抛乱码，不如在选后端之前统一拦截，CLI/GUI/导入全路径同文案。
    # 审查 W-A3-1（M2 真因），S1。
    if spec.password and fmt == "zip" and not spec.password.isascii():
        raise PasswordRequired(
            "ZIP 格式的密码不支持中文/非 ASCII 字符（7-Zip 限制），"
            "请改用 ASCII 密码，或把格式改为 7z（支持任意密码）",
            fix="改用 7z 格式，或使用纯英文数字密码",
        )

    # 密码里的双引号会被 7-Zip 当命令行引号语法消化（用户测试 + 审计双重确认）：
    # `-p{password}` 直接拼进 argv，7za 侧实际密码串与字面量不一致，
    # 导致**产出的加密包在另一套工具里用同一密码打不开**——而记录显示
    # status=success / verification=passed（py7zr 侧能开，7za 侧不能）。
    # 这是「备份实质不可恢复」，必须在创建前拦截。
    # 同类危险字符：`"`（引号）与换行（argv 无法表达）。
    if spec.password and any(ch in spec.password for ch in ('"', "\n", "\r")):
        raise PasswordRequired(
            "密码不能包含双引号或换行符（7-Zip 命令行解析会吃掉它们，"
            "导致产出的加密包用同一密码打不开）",
            fix='请改用不含 `"` 和换行的密码（如把引号换成单引号或去掉）',
        )

    choice = backend_for_format(fmt, force=spec.force)
    if choice.factory is None:  # 理论上不会发生
        raise BackendUnavailable("未选择到可用后端")

    # 原生分卷是否可用（7z 精简版也支持 -v；RAR 必须可创建）
    mode = multivolume_mode_for(fmt, spec.multivolume)
    if mode == "native" and fmt == "rar" and not rar_available():
        mode = "split"
    if spec.force and spec.multivolume and mode == "native" and fmt == "7z":
        # 强制模式优先保证「先能产出」：统一用字节切片分卷，外部程序失败时可回退内置引擎
        mode = "split"

    item_list = list(sources)
    files = sum(1 for it in item_list if not it.is_dir)
    dirs = sum(1 for it in item_list if it.is_dir)
    # 只累加非目录条目：目录 size 是子树合计，加进来会重复计数
    # （实测虚高 目录层数+2 倍，污染空间预检与压缩比）
    source_bytes = sum(it.size for it in item_list if not it.is_dir)

    dest = Path(spec.dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _spec_for(f: str, mode_value: str | None) -> ArchiveSpec:
        return ArchiveSpec(
            dest_path=dest, fmt=f, password=spec.password, encrypt_mode=encrypt_mode,
            level=spec.level, multivolume=mode_value, volume_size=spec.volume_size,
            follow_symlinks=spec.follow_symlinks, force=spec.force,
        )

    from .py7zr_backend import ZipFileCompressor

    fallback_notes: list[str] = []
    factory = choice.factory

    def _sniff_container(path: Path) -> str:
        """按文件头判断真实容器（ZIP 是 ``PK\\x03\\x04`` / ``PK\\x05\\x06``）。

        只用于 ZIP 判定；非 ZIP 的交给 ``fmt`` 本身。
        这样 ``.rar`` 扩展名 + ZIP 内容（强制降级）也能被正确识别，
        校验就不会拿 ``unrar t`` 去打一个 ZIP（那必然报退出码 13）。
        """
        try:
            with open(path, "rb") as fh:
                head = fh.read(4)
        except OSError:
            return ""
        return "zip" if head[:2] == b"PK" else ""

    def _fallback(exc: BaseException, mode_value: str | None):
        """强制模式下外部程序失败：换内置引擎（返回新的 backend）。

        关键：必须按**原格式**选择后备，不能把 ZIP 请求交给 7z 容器。
        """
        reason = (str(exc).splitlines() or [type(exc).__name__])[0][:120]
        name = str(getattr(choice, "name", ""))
        with contextlib.suppress(OSError):
            if dest.exists() and dest.stat().st_size == 0:
                dest.unlink()
        if fmt == "zip":
            # ZIP 请求：内置 zipfile，产出标准 ZIP
            fallback_notes.append(f"外部压缩程序失败（{reason}），已改用内置 ZIP 引擎")
            return ZipFileCompressor(dest, _spec_for("zip", None), progress)
        if fmt == "rar":
            # RAR 请求且 RAR 引擎不可用：内置 ZIP 容器（扩展名保持 .rar，内容为 ZIP）
            fallback_notes.append(f"外部压缩程序失败（{reason}），已改用内置 ZIP 引擎（扩展名 .rar）")
            return ZipFileCompressor(dest, _as_zip_spec(_spec_for("rar", None)), progress)
        if fmt == "7z" and py7zr is not None and not name.startswith("py7zr"):
            fallback_notes.append(f"外部程序失败（{reason}），已改用内置 7z 引擎")
            return _TarStreamTo7z(dest, _spec_for("7z", mode_value), progress)
        raise exc

    try:
        backend = factory(dest, _spec_for(fmt, mode), progress)
    except (CompressionError, OSError, PermissionError) as exc:
        if not spec.force:
            raise
        backend = _fallback(exc, mode)
        if fmt == "zip":
            mode = None
        elif mode == "native":
            mode = None

    result = ArchiveResult(
        path=dest,
        fmt=fmt,
        files=files,
        dirs=dirs,
        source_bytes=source_bytes,
        backend=getattr(backend, "name", choice.name),
        encrypted=bool(spec.password),
        encrypt_mode=encrypt_mode,
        secure=(encrypt_mode != "zipcrypto"),
        multivolume=mode,
        volume_size=spec.volume_size if mode else 0,
        forced=bool(spec.force),
        notes=([choice.note] if choice.note else []) + fallback_notes,
        # 备份身份名由 dest 正向推导（审查 W-A2-2）：engine 不再从产物名反推
        stem=product_stem(dest.name),
    )
    def _register_cancel(b) -> None:  # noqa: ANN001
        setter = getattr(b, "set_cancel", None)
        if callable(setter):
            setter(cancel)

    _register_cancel(backend)
    progress.set_plan(source_bytes, files)
    progress.describe("write")

    try:
        if backend is None:
            raise CompressionError("没有可用的压缩后端")
        for it in item_list:
            if cancel is not None and cancel():
                raise ArchiveCancelled()
            try:
                if it.is_dir:
                    backend.add_dir(it.arcname, it.mtime)
                elif it.is_link:
                    backend.add_symlink(it.arcname, it.link_target or _safe_readlink(it.source), it.mtime)
                elif it.source.exists():
                    backend.add_file(it.source, it.arcname, it.mtime)
                else:
                    # 源文件在「扫描时存在 → 写入时消失」（红队 F2，P0）。
                    # 旧实现只 `result.errors.append`，而 engine 只把 errors 并进
                    # warnings、`partial` 判定只看 `skipped_busy` → 实测
                    # `status=success / passed / committed / errors=[]`，文件根本不在包里。
                    # 登记到 skipped_busy 才会让任务降级成 partial。
                    result.errors.append(f"源文件不存在：{it.source}")
                    _notify_skipped(backend, f"{it.arcname}（扫描后消失）")
            except ArchiveCancelled:
                raise
            except (OSError, PermissionError) as exc:
                result.errors.append(f"{it.arcname}: {exc}")
            progress.item_done()

        try:
            backend.close()
        except ArchiveCancelled:
            # 取消不是失败：直接上抛，绝不触发 fallback 重做（审查 R3-N3）
            raise
        except (CompressionError, OSError) as exc:
            if not spec.force or getattr(backend, "name", "") == "py7zr":
                raise
            # 压缩阶段失败：用内置引擎整体重做一次
            if backend is not None:
                with contextlib.suppress(Exception):
                    backend.abort()
            with contextlib.suppress(OSError):
                if dest.exists():
                    dest.unlink()
            backend = _fallback(exc, mode)
            _register_cancel(backend)
            if fmt == "zip":
                mode = None
            elif mode == "native":
                mode = None
            for it in item_list:
                if it.is_dir:
                    backend.add_dir(it.arcname, it.mtime)
                elif it.is_link:
                    backend.add_symlink(it.arcname, it.link_target, it.mtime)
                elif it.source.exists():
                    backend.add_file(it.source, it.arcname, it.mtime)
            backend.close()
        if cancel is not None and cancel():
            raise ArchiveCancelled()

        # 有文件因「被占用 / 无权限」被跳过时，把清单带到 result 上，
        # 让 engine 把任务标成 partial（而不是假绿 success）。用户测试 P0：
        # Excel/PSD 打开着的文件被跳过、其余照常备份，旧实现仍报「成功/校验通过」。
        _busy = list(getattr(backend, "skipped_busy", []) or [])
        if _busy:
            result.skipped_busy = _busy
            result.skipped = max(result.skipped, len(_busy))

        splitter = getattr(backend, "split_volumes", None)
        if mode != "native" and spec.volume_size and callable(splitter):
            try:
                splitter()
            except TypeError:
                pass
            except ArchiveCancelled:
                raise

        volumes = getattr(backend, "volumes", []) or []
        if volumes:
            result.volumes = volumes
            result.path = volumes[0].path
        else:
            result.path = dest
            if not dest.exists():
                raise CompressionError("压缩包未生成", backend=result.backend)
            result.volumes = [VolumeInfo(dest, dest.stat().st_size)]

        # 通用分卷模式下，主文件其实不存在，需修正 path
        if mode == "split" and result.volumes and not dest.exists():
            result.path = result.volumes[0].path

        # 加密状态事后实测回填（不撒谎）：声明有密码但实测无加密 → 修正 + warning
        if spec.password:
            # 分卷产物必须先拼接后实测：首卷无法解析 → probe 返 None，
            # 旧代码在 None 时保留声明的 encrypted=True，等于让记录替产物撒谎
            # （审查 W-A3-3）。单卷走原路径，多卷走拼接路径。
            if len(result.volumes) > 1:
                actual = probe_encrypted_volumes(result.volumes, fmt, spec.password or None)
            else:
                actual = probe_encrypted(result.path, fmt, spec.password or None)
            if actual is False:
                result.encrypted = False
                result.secure = False
                result.notes.append(
                    "警告：已请求加密，但产物检查未发现加密标志——该归档可能未加密，请核对"
                )
            elif actual is True:
                result.encrypted = True
            else:
                # None = 无法实测。fail-closed：宁可少报加密，绝不把"没验证"写成"已加密"。
                result.encrypted = False
                result.secure = False
                result.notes.append(
                    "警告：未能实测该归档的加密状态（分卷无法拼接或工具不可用）——"
                    "请勿视为已加密，需手动确认（本次记录 encrypted=False）"
                )

        result.notes.extend(getattr(backend, "notes", []))
        for w in getattr(backend, "warnings", []):
            if w not in result.notes:
                result.notes.append(w)
        # 真实容器格式以**产物字节**为准（最可靠的账实相符）：强制模式可能把 RAR
        # 请求降级成 ZIP 容器（rar-as-zip-forced / rar-as-zipfile-forced），而前者的
        # backend.name 仍是 "7z:7za.exe"，靠名字区分不出来；嗅探 magic 一定对。
        result.container_fmt = _sniff_container(result.path) or fmt
        result.skipped = 0
        result.elapsed = time.monotonic() - started
        progress.describe("done")
        progress.emit(force=True)
        return result
    except BaseException as exc:
        with contextlib.suppress(Exception):
            backend.abort()
        # 清理可能留下的分卷与半成品
        for p in dest.parent.glob(dest.name + ".*"):
            if p.is_file() and (p.name.split(".")[-1].isdigit() or ".part" in p.name):
                with contextlib.suppress(OSError):
                    p.unlink()
        with contextlib.suppress(OSError):
            if dest.exists() and dest.name.endswith(FORMAT_EXTENSIONS.get(fmt, "")):
                dest.unlink()
        # 统一兜底：内部取消信号（py7zr_backend._Cancelled 等）必须转成
        # 对外的 ArchiveCancelled，否则被调用方记成"未预期异常 = failure"
        # （审查 R3-N3：取消被记 failure 的根因）。
        try:
            from .py7zr_backend import _Cancelled as _InnerCancelled

            if isinstance(exc, _InnerCancelled):
                raise ArchiveCancelled() from None
        except ImportError:
            pass
        raise
