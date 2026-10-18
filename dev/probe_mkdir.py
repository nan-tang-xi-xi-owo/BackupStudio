"""逐项验证：受限环境下哪种「建目录」写法会失败、哪种成功。

结论将决定 runtime_dir() 的实现方式。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
from pathlib import Path

base = Path(tempfile.mkdtemp(prefix="mkdir-probe-", dir=str(Path.cwd() / "build")))
print("base:", base)


def attempt(label: str, fn) -> None:  # noqa: ANN001
    try:
        result = fn()
        print(f"  {label:<34} OK   {result}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:<34} FAIL {type(exc).__name__}: {exc}")


print("[1] 父目录已存在，用 pathlib 建子目录")
attempt("Path.mkdir()", lambda: (base / "a1").mkdir() or (base / "a1"))
attempt("Path.mkdir(parents=True, exist_ok=True)", lambda: (base / "a2").mkdir(parents=True, exist_ok=True) or (base / "a2"))

print("[2] 父目录也还不存在，一次建两层")
attempt("Path.mkdir(parents=True)", lambda: (base / "p1" / "c1").mkdir(parents=True) or (base / "p1" / "c1"))

print("[3] os.makedirs")
attempt("os.makedirs()", lambda: (os.makedirs(base / "m1"), base / "m1")[1])

print("[4] Win32 CreateDirectoryW")
kernel32 = ctypes.windll.kernel32


def create_directory_w(p: Path) -> Path:
    ok = kernel32.CreateDirectoryW(str(p), None)
    if not ok:
        err = ctypes.get_last_error() or kernel32.GetLastError()
        if err not in (183,):  # 183 = ERROR_ALREADY_EXISTS
            raise OSError(err, f"CreateDirectoryW 失败：{p}")
    return p


attempt("CreateDirectoryW", lambda: create_directory_w(base / "w1"))

print("[5] 带重试的 mkdir（模拟杀毒/索引器短暂占用）")
import time


def mkdir_with_retry(p: Path, tries: int = 3) -> Path:
    last: Exception | None = None
    for _ in range(tries):
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError as exc:
            last = exc
            time.sleep(0.2)
    raise last  # type: ignore[misc]


attempt("mkdir + retry", lambda: mkdir_with_retry(base / "r1"))

print("[6] 临时目录兜底")
attempt("tempfile.gettempdir()", lambda: Path(tempfile.gettempdir()))

shutil.rmtree(base, ignore_errors=True)
