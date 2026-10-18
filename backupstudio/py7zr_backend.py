"""纯 Python 的 7z 写入后端（强制压缩 / 7-Zip 不可用时使用）。

7-Zip 官方精简版与自解压安装在部分受限 Windows 环境下会报
「cannot open file / 拒绝访问」，此时用 py7zr 作为后备，保证「强制压缩」可用。
为处理大文件，输入先写成未压缩 tar 流并按大小切段，再逐段交给 py7zr，
避免把整个备份读进内存（py7zr 的写接口会对单个对象做缓冲）。
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import shutil
import tarfile
from pathlib import Path

try:
    import py7zr  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    py7zr = None

from .staging import make_staging_dir


class _Cancelled(Exception):
    """取消信号（由 archives.create_archive 转成 ArchiveCancelled）。"""

# 单段暂存上限（源字节）。
STREAM_LIMIT = 512 * 1024 * 1024

# 压缩级别 → py7zr 过滤器（id 33 = LZMA2）


def _filters_for(level: int):  # noqa: ANN001
    """压缩级别 → py7zr 过滤器。id 33 = LZMA2，51 = Copy。"""
    if py7zr is None:
        return None
    if level <= 0:
        return [{"id": py7zr.FILTER_COPY}]
    return [{"id": py7zr.FILTER_LZMA2, "preset": max(0, min(9, int(level)))}]


class _Reader:
    def __init__(self, fh, cb) -> None:  # noqa: ANN001
        self.fh = fh
        self.cb = cb

    def read(self, n: int = -1) -> bytes:
        data = self.fh.read(n if n and n > 0 else (1 << 20))
        if data:
            self.cb(len(data))
        return data


class Py7zrCompressor:
    """纯 Python 写入后端。

    * 7z：直接以原生方式写（中文/emoji 文件名、AES-256、文件名加密都支持）；
    * zip：py7zr **不能写 ZIP**，因此改用标准库 zipfile 写真正的 ZIP 容器
      （中文文件名按 UTF-8 标志位保存），避免「伪装成 .zip 的 tar」。
    """

    name = "py7zr"

    def __init__(self, dest_path: Path, spec, progress) -> None:  # noqa: ANN001
        if py7zr is None:
            raise RuntimeError("py7zr 未安装：pip install py7zr")
        self.dest = Path(dest_path)
        self.progress = progress
        self._warnings: list[str] = []
        self.notes: list[str] = []
        if spec.multivolume == "native":
            # 原生分卷由 create_archive 前置改成 split，这里只是兜底
            spec = dataclasses.replace(spec, multivolume="split")
        self.spec = spec
        self.tmpdir = make_staging_dir(self.dest.parent, prefix="bs-py7zr-")
        self._streams: list[Path] = []
        self._fh = None
        self._tar: tarfile.TarFile | None = None
        self._stream_bytes = 0
        self._n = 0
        self._zip = None
        self._cancel = None
        self._zip_mode = getattr(spec, "fmt", "7z") == "zip"
        if self._zip_mode:
            import zipfile

            try:
                level = max(1, min(9, int(spec.level or 5)))
                self._zip = zipfile.ZipFile(
                    self.dest, "w",
                    compression=zipfile.ZIP_STORED if spec.level == 0 else zipfile.ZIP_DEFLATED,
                    compresslevel=level,
                    allowZip64=True,
                )
            except (OSError, ValueError) as exc:
                # 目标目录不可写：交给上层回退逻辑
                raise RuntimeError(f"无法创建 ZIP：{exc}") from exc
            self.name = "zipfile-forced"
            if spec.password:
                self._warnings.append("内置引擎写 ZIP 不支持加密，本次未设置密码（需要 7-Zip 才能加密 ZIP）")
        else:
            self._open_stream()

    # ---- 流管理 -------------------------------------------------------
    def set_cancel(self, cancel) -> None:  # noqa: ANN001
        self._cancel = cancel

    @property
    def warnings(self) -> list[str]:
        return self._warnings

    def _open_stream(self) -> None:
        self._n += 1
        path = self.tmpdir / f"stream-{self._n:03d}.tar"
        self._fh = open(path, "wb")
        self._tar = tarfile.open(fileobj=self._fh, mode="w|", format=tarfile.PAX_FORMAT, bufsize=1 << 20)
        self._streams.append(path)
        self._stream_bytes = 0

    def _ensure_stream(self, size: int = 0) -> None:
        if self._tar is None or self._stream_bytes + size > STREAM_LIMIT:
            self._close_stream()
            self._open_stream()

    def _close_stream(self) -> None:
        if self._tar is not None:
            with contextlib.suppress(Exception):
                self._tar.close()
            self._tar = None
        if self._fh is not None:
            with contextlib.suppress(Exception):
                self._fh.close()
            self._fh = None

    def _ti(self, arcname: str, mtime: float | None, size: int) -> tarfile.TarInfo:
        ti = tarfile.TarInfo(arcname)
        ti.size = size
        ti.mtime = int(mtime or 0) or 1
        return ti

    # ---- 写入 ---------------------------------------------------------
    def _zip_info(self, arcname: str, mtime: float | None, is_dir: bool):  # noqa: ANN201 - 返回 zipfile.ZipInfo
        import time as _time
        import zipfile

        ts = _time.localtime(mtime or _time.time())[:6]
        if ts[0] < 1980:
            ts = (1980, 1, 1, 0, 0, 0)
        zi = zipfile.ZipInfo(arcname + ("/" if is_dir else ""), date_time=ts)
        zi.external_attr = (0o40755 if is_dir else 0o100644) << 16
        try:
            zi.flag_bits |= 0x800  # UTF-8 文件名标志位（中文/emoji 正确显示）
        except Exception:  # noqa: BLE001
            pass
        return zi

    def add_dir(self, arcname: str, mtime: float | None = None) -> None:
        if self._zip is not None:
            self._zip.writestr(self._zip_info(arcname, mtime, True), b"")
            return
        self._ensure_stream(1024)
        ti = self._ti(arcname.rstrip("/") + "/", mtime, 0)
        ti.type = tarfile.DIRTYPE
        ti.mode = 0o755
        self._tar.addfile(ti)
        self._stream_bytes += 1024

    def add_symlink(self, arcname: str, target: str, mtime: float | None = None) -> None:
        data = target.encode("utf-8", "surrogateescape")
        if self._zip is not None:
            self._zip.writestr(self._zip_info(arcname, mtime, False), data)
            return
        self._ensure_stream(len(data) + 1024)
        ti = self._ti(arcname, mtime, len(data))
        ti.mode = 0o644
        self._tar.addfile(ti, io.BytesIO(data))
        self._stream_bytes += len(data) + 1024

    def add_file(self, src: Path, arcname: str, mtime: float | None = None) -> int:
        if src.is_symlink():
            try:
                target = os.readlink(src)
            except OSError:
                target = ""
            self.add_symlink(arcname, target, mtime)
            return 0
        st = src.stat()
        if self._zip is not None:
            with open(src, "rb") as fh, self._zip.open(self._zip_info(arcname, mtime or st.st_mtime, False), "w") as out:
                shutil.copyfileobj(fh, out, 1 << 20)
            self.progress.add(st.st_size)
            return st.st_size
        self._ensure_stream(st.st_size)
        ti = self._ti(arcname, mtime or st.st_mtime, st.st_size)
        ti.mode = st.st_mode & 0o7777 or 0o644
        with open(src, "rb") as fh:
            self._tar.addfile(ti, _Reader(fh, self.progress.add))
        self._stream_bytes += st.st_size + 1024
        return st.st_size

    def add_bytes(self, data: bytes, arcname: str, mtime: float | None = None) -> int:
        if self._zip is not None:
            self._zip.writestr(self._zip_info(arcname, mtime, False), data)
            self.progress.add(len(data))
            return len(data)
        self._ensure_stream(len(data))
        ti = self._ti(arcname, mtime, len(data))
        ti.mode = 0o644
        self._tar.addfile(ti, io.BytesIO(data))
        self._stream_bytes += len(data) + 1024
        self.progress.add(len(data))
        return len(data)

    # ---- 收尾 ---------------------------------------------------------
    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            return
        self._close_stream()
        self.progress.describe("compress")
        password = self.spec.password or None
        header_enc = bool(password and self.spec.encrypt_mode == "aes256")
        try:
            with py7zr.SevenZipFile(
                str(self.dest), "w", password=password, header_encryption=header_enc,
                filters=_filters_for(int(self.spec.level)),
            ) as z:
                for stream in self._streams:
                    with tarfile.open(stream, "r:") as tf:
                        for member in tf:
                            if self._cancel is not None and self._cancel():
                                raise _Cancelled()
                            if member.isdir():
                                continue
                            fh = tf.extractfile(member)
                            if fh is None:
                                continue
                            with fh:
                                z.writef(fh, member.name)
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def abort(self) -> None:
        if self._zip is not None:
            with contextlib.suppress(Exception):
                self._zip.close()
            self._zip = None
        self._close_stream()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        with contextlib.suppress(OSError):
            if self.dest.exists() and self.dest.stat().st_size == 0:
                self.dest.unlink()
