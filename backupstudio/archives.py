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
import gzip
import io
import lzma
import os
import shutil
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
from .staging import make_staging_dir, same_volume
from .tools import find_7z, find_rar, probe_unrar_trial

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
    volumes: list[VolumeInfo] = field(default_factory=list)
    files: int = 0
    dirs: int = 0
    source_bytes: int = 0
    skipped: int = 0
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
    import fnmatch
    import re

    pats = [p.strip().replace("\\", "/") for p in patterns if p and p.strip()]
    regexes = []
    for p in pats:
        if p.startswith("re:"):
            try:
                regexes.append(re.compile(p[3:]))
            except re.error:
                continue

    def match(name: str, rel: str) -> bool:
        rel_norm = rel.replace("\\", "/")
        for rgx in regexes:
            if rgx.search(rel_norm) or rgx.search(name):
                return True
        for p in pats:
            if p.startswith("re:"):
                continue
            if fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(rel_norm, p):
                return True
            if p.startswith("**/") and fnmatch.fnmatchcase(rel_norm, p[3:]):
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
    if name.startswith(".bs-staging") or name.endswith(".full-tmp") or ".part-tmp" in name:
        return True
    return False


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
            size = 0 if (is_dir or is_link) else st.st_size
            collected.append(Item(path, arc, size, st.st_mtime, is_dir, is_link, target))

        # 目录字节数 = 子树内文件字节之和，让进度条按真实数据量推进
        dir_bytes: dict[str, int] = {}
        for it in collected:
            if it.size and "/" in it.arcname:
                parent = it.arcname.rsplit("/", 1)[0]
                while True:
                    dir_bytes[parent] = dir_bytes.get(parent, 0) + it.size
                    if "/" not in parent:
                        break
                    parent = parent.rsplit("/", 1)[0]
        for it in collected:
            if it.is_dir and it.arcname in dir_bytes:
                it.size = dir_bytes[it.arcname]
            items.append(it)
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


def _chunks_of(
    src: Path,
    progress: Progress,
    chunk: int = 1 << 20,
    cancel: Callable[[], bool] | None = None,
) -> Iterator[bytes]:
    with open(src, "rb") as fh:
        while True:
            if cancel is not None and cancel():
                raise ArchiveCancelled()
            data = fh.read(chunk)
            if not data:
                break
            progress.add(len(data))
            yield data


# --------------------------------------------------------------------------
# ZIP
# --------------------------------------------------------------------------

class ZipCompressor:
    """标准库 zipfile 后端（不支持加密）。

    若任务设置了密码，本后端会直接拒绝（PasswordRequired）——fail-closed：
    宁可明确报错，也不产出"记录为已加密、实际明文"的归档。
    需要加密 ZIP 请安装 7-Zip（走 SevenZipCompressor）或改用 7z 格式。
    """

    name = "python-zipfile"

    def __init__(self, dest_path: Path, spec: ArchiveSpec, progress: Progress) -> None:
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

    @property
    def warnings(self) -> list[str]:
        return self._warnings

    def _info(self, arcname: str, mtime: float | None, is_dir: bool) -> zipfile.ZipInfo:
        ts = time.localtime(mtime if mtime else time.time())[:6]
        if ts[0] < 1980:
            ts = (1980, 1, 1, 0, 0, 0)
        zi = zipfile.ZipInfo(arcname + ("/" if is_dir else ""), date_time=ts)
        zi.external_attr = (0o40755 if is_dir else 0o100644) << 16
        zi.create_system = 3
        return zi

    def add_dir(self, arcname: str, mtime: float | None = None) -> None:
        self._zip.writestr(self._info(arcname, mtime, True), b"")

    def add_symlink(self, arcname: str, target: str, mtime: float | None = None) -> None:
        self.add_bytes(target.encode("utf-8", "surrogateescape"), arcname, mtime)

    def add_file(self, src: Path, arcname: str, mtime: float | None = None) -> int:
        mtime = mtime or src.stat().st_mtime
        with open(src, "rb") as fh, self._zip.open(self._info(arcname, mtime, False), "w") as out:
            shutil.copyfileobj(fh, out, 1 << 20)
            self.progress.add(src.stat().st_size)
        return src.stat().st_size

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
        self._raw = open(dest_path, "wb")
        self._proc: subprocess.Popen | None = None
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
                return _zstd_mod.ZstdCompressor(level=level).stream_writer(self._raw, closefd=False)
            zstd = shutil.which("zstd")
            if not zstd:
                raise BackendUnavailable(
                    "创建 tar.zst 需要 zstandard 模块或系统 zstd 程序",
                    fix="pip install zstandard 或安装 zstd（apt install zstd / brew install zstd）",
                )
            self._proc = subprocess.Popen([zstd, "-q", f"-{level}", "-c"], stdin=subprocess.PIPE, stdout=self._raw)
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

    def add_file(self, src: Path, arcname: str, mtime: float | None = None) -> int:
        if src.is_symlink():
            self.add_symlink(arcname, _safe_readlink(src), mtime)
            return 0
        st = src.stat()
        ti = self._ti(arcname, mtime or st.st_mtime, (st.st_mode & 0o7777) or 0o644, tarfile.REGTYPE, st.st_size)
        self._tar.addfile(ti, _Reader(_chunks_of(src, self.progress)))
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
        with contextlib.suppress(OSError):
            self.dest.unlink()

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

    def __init__(self, dest_path: Path, spec: ArchiveSpec, progress: Progress) -> None:
        self.dest = dest_path
        self.spec = spec
        self.progress = progress
        self._warnings: list[str] = []
        self.notes: list[str] = []
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

    def add_file(self, src: Path, arcname: str, mtime: float | None = None) -> int:
        if self._cancel is not None and self._cancel():
            raise ArchiveCancelled()
        if src.is_symlink():
            self.add_symlink(arcname, _safe_readlink(src), mtime)
            return 0
        st = src.stat()
        ti = self._ti(arcname, mtime or st.st_mtime, st.st_size)
        ti.mode = st.st_mode & 0o7777
        self._tar.addfile(ti, _Reader(_chunks_of(src, self.progress, cancel=self._cancel)))
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
        """
        unpacked = self.tmpdir / "unpacked"
        unpacked.mkdir(parents=True, exist_ok=True)
        self._run([self.exe, "x", str(tar_path), f"-o{unpacked}", "-y", "-bso0", "-bsp0"])
        with contextlib.suppress(OSError):
            tar_path.unlink()
        return unpacked

    def _pack_from(self, src_dir: Path, out_path: Path, *, extra_args: list[str] | None = None) -> None:
        """从解包目录打包到 out_path（cwd=源目录保证归档内路径与 arcname 一致）。"""
        if self._cancel is not None and self._cancel():
            raise ArchiveCancelled()
        argv = [self.exe, "a", *self._args(), *(extra_args or []), str(out_path), "."]
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
            self.progress.describe("compress")
            src_dir = self._unpack_staging(tar_path)
            mode = self.spec.multivolume
            if mode == "native":
                self._run_native_volumes(src_dir)
            else:
                self._run_single(src_dir)
            # 通用分卷：由 create_archive 在收尾时调用 split_volumes
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def abort(self) -> None:
        with contextlib.suppress(Exception):
            self._tar.close()
        with contextlib.suppress(Exception):
            self._fh.close()
        if self._proc is not None and self._proc.poll() is None:
            with contextlib.suppress(Exception):
                self._proc.kill()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _popen(self, argv: list[str], cwd: str | None = None) -> subprocess.Popen:
        """启动外部压缩程序。

        部分受限环境（沙箱、低完整性）下 7z 子进程访问系统临时目录会被拒绝，
        这里给子进程显式设置 TMP/TEMP 到可写的暂存目录。
        """
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
                import subprocess
                import sys

                argv = [exe, "l", "-slt", str(path)]
                if password:
                    argv.insert(2, f"-p{password}")
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
    source_bytes = sum(it.size for it in item_list)

    dest = Path(spec.dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    encrypt_mode = spec.encrypt_mode if spec.password else None
    if spec.password and not encrypt_mode:
        raise PasswordRequired("设置了密码但未指定加密方式", fix="选择 AES-256 或 ZipCrypto")
    if spec.password and encrypt_mode == "gpg":
        # gpg 加密尚未实现（fail-closed：静默产出明文比报错更危险）
        raise BackendUnavailable(
            "GPG 加密尚未实现：本次未创建归档（不会产出明文包）",
            fix="改用 AES-256（7z/zip/rar）加密，或关闭密码保护",
        )

    def _spec_for(f: str, mode_value: str | None) -> ArchiveSpec:
        return ArchiveSpec(
            dest_path=dest, fmt=f, password=spec.password, encrypt_mode=encrypt_mode,
            level=spec.level, multivolume=mode_value, volume_size=spec.volume_size,
            follow_symlinks=spec.follow_symlinks, force=spec.force,
        )

    from .py7zr_backend import ZipFileCompressor

    fallback_notes: list[str] = []
    factory = choice.factory

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
                    result.errors.append(f"源文件不存在：{it.source}")
            except ArchiveCancelled:
                raise
            except (OSError, PermissionError) as exc:
                result.errors.append(f"{it.arcname}: {exc}")
            progress.item_done()

        try:
            backend.close()
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
                result.notes.append("提示：无法自动验证该归档的加密状态（工具不可用）")

        result.notes.extend(getattr(backend, "notes", []))
        for w in getattr(backend, "warnings", []):
            if w not in result.notes:
                result.notes.append(w)
        result.skipped = 0
        result.elapsed = time.monotonic() - started
        progress.describe("done")
        progress.emit(force=True)
        return result
    except BaseException:
        with contextlib.suppress(Exception):
            backend.abort()
        # 清理可能留下的分卷
        for p in dest.parent.glob(dest.name + ".*"):
            if p.is_file() and (p.name.split(".")[-1].isdigit() or ".part" in p.name):
                with contextlib.suppress(OSError):
                    p.unlink()
        with contextlib.suppress(OSError):
            if dest.exists() and dest.name.endswith(FORMAT_EXTENSIONS.get(fmt, "")):
                dest.unlink()
        raise
