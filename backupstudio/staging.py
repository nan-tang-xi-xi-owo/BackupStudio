"""把临时目录放到备份目标所在的磁盘上。

原因：
* 某些 Windows 受限环境（沙箱 / 低完整性 / 重定向的 %TEMP%）下，7z 子进程
  无法写入系统临时目录，会报 "cannot open file / 拒绝访问"；
* 同卷暂存还能避免把几十 GB 数据先写到系统盘再复制过去。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path


def same_volume(a: Path, b: Path) -> bool:
    try:
        return os.path.splitdrive(os.path.abspath(str(a)))[0].lower() == os.path.splitdrive(os.path.abspath(str(b)))[0].lower()
    except (OSError, ValueError):
        return False


def staging_parent(near: Path) -> Path:
    """为 ``near`` 选一个可写的暂存父目录。

    顺序：目标目录 → 目标目录的 ``.bs-staging`` 子目录 → 系统临时目录。
    """
    near = Path(near)
    bases: list[Path] = []
    if near.is_dir():
        bases.append(near)
    elif near.parent.is_dir():
        bases.append(near.parent)
    for base in bases:
        for probe in (base, base / ".bs-staging"):
            try:
                probe.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            if not os.access(str(probe), os.W_OK):
                continue
            try:
                with tempfile.NamedTemporaryFile(dir=str(probe), prefix=".w-", delete=True):
                    pass
            except OSError:
                continue
            return probe
    return Path(tempfile.gettempdir())


def make_staging_dir(near: Path, prefix: str = "bs-stage-") -> Path:
    parent = staging_parent(near)
    try:
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
    except OSError:
        return Path(tempfile.mkdtemp(prefix=prefix))


@contextlib.contextmanager
def staging_dir(near: Path, prefix: str = "bs-stage-"):
    path = make_staging_dir(near, prefix)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
