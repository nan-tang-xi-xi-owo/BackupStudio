"""新增 Windows 长路径支持（S0：>260 字符路径备份时静默丢文件）。

## 缺陷

`LongPathsEnabled=0` 的 Windows 上，传统 Win32 API 限制路径 **260 字符**（MAX_PATH）。
超过之后 `stat()` / `open()` 直接失败（`FileNotFoundError [WinError 3]`）。

产品全程未使用 `\\\\?\\` 长路径前缀，于是：
  * 备份端：读不到属性 → 静默计入 skipped → **文件不进包，仍报 success/passed**
  * 解压端：zip/tar 整包失败（`WinError 206`）、**7z 假成功**（ok=True 但落地 0）

实测（271 字符深路径）：
    status=success  files=1  verification=passed  errors=[]   ← 包内没有 deep.txt

## 修法

新增 `long_path()`：在 Windows 上给绝对路径加 `\\\\?\\` 前缀（UNC 用
`\\\\?\\UNC\\`），让 `stat`/`open`/`mkdir` 突破 260 限制。

**只影响 Python 侧的路径操作**，不改归档内的成员名（归档名恒用相对路径）。

> 注：`\\\\?\\` 前缀有严格规则 —— 必须是**绝对路径**、用**反斜杠**、且不做 `.`/`..`
> 归一。所以只在「路径长度接近或超过 260」时才加，避免影响正常路径的行为
> （例如 `\\\\?\\` 下 `..` 不会被解析）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 触发加前缀的阈值。留余量：Windows 在 260 处失败，我们从 240 就开始加，
#: 因为拼接 staging 目录后可能再涨几十字符。
_LONG_PATH_THRESHOLD = 240


def long_path(p: Path | str) -> str:
    """返回可用于文件操作的路径字符串（Windows 上必要时带 ``\\\\?\\`` 前缀）。

    非 Windows 平台原样返回。路径不长时原样返回 —— 避免 ``\\\\?\\`` 带来的
    语义差异（不解析 ``..``、不剥尾部空格点）。
    """
    s = str(p)
    if sys.platform != "win32":
        return s
    if len(s) < _LONG_PATH_THRESHOLD:
        return s
    # 已经是扩展路径
    if s.startswith("\\\\?\\"):
        return s
    # UNC：\\server\share\... → \\?\UNC\server\share\...
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    # 必须是绝对路径（带盘符）
    if len(s) > 1 and s[1] == ":":
        return "\\\\?\\" + s
    # 相对路径先绝对化
    try:
        return "\\\\?\\" + os.path.abspath(s)
    except (OSError, ValueError):
        return s


def stat_long(p: Path | str):  # noqa: ANN201
    """``os.stat`` 的长路径版本。"""
    return os.stat(long_path(p))


def lstat_long(p: Path | str):  # noqa: ANN201
    """``os.lstat`` 的长路径版本。"""
    return os.lstat(long_path(p))


def open_long(p: Path | str, mode: str = "rb", **kw):  # noqa: ANN201
    """``open`` 的长路径版本。"""
    return open(long_path(p), mode, **kw)


def mkdir_long(p: Path | str, parents: bool = True, exist_ok: bool = True) -> None:
    """``mkdir`` 的长路径版本。"""
    Path(long_path(p)).mkdir(parents=parents, exist_ok=exist_ok)


def exists_long(p: Path | str) -> bool:
    """``exists`` 的长路径版本。"""
    try:
        return os.path.exists(long_path(p))
    except (OSError, ValueError):
        return False


def is_dir_long(p: Path | str) -> bool:
    """``is_dir`` 的长路径版本。"""
    try:
        return os.path.isdir(long_path(p))
    except (OSError, ValueError):
        return False


def scandir_long(p: Path | str):  # noqa: ANN201
    """``os.scandir`` 的长路径版本。"""
    return os.scandir(long_path(p))


def unlink_long(p: Path | str) -> None:
    """``unlink`` 的长路径版本。"""
    os.unlink(long_path(p))


def rmtree_long(p: Path | str) -> None:
    """``rmtree`` 的长路径版本（处理只读 + 超长路径）。"""
    import shutil

    target = long_path(p)

    def _on_error(func, path, _exc):  # noqa: ANN001
        import contextlib

        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
            func(path)

    shutil.rmtree(target, onerror=_on_error)
