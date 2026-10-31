"""外部压缩工具的查找与探测（7-Zip / RAR / 系统 tar 等）。

所有后端都通过子进程调用，因此：
* 需要能在 PyInstaller 冻结环境中正确找到 exe；
* 需要把用户自定义路径放在 PATH 之外也能用；
* 探测必须快速且可缓存（``which`` + ``--help`` 级检查）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from .platform_info import get_platform_info


def _exe_names(base: str) -> list[str]:
    if sys.platform == "win32":
        return [base + ".exe", base]
    return [base]


# 7-Zip 常见安装位置（Windows / macOS / Linux）
_SEVEN_ZIP_CANDIDATES: dict[str, list[str]] = {
    "win32": [
        r"%ProgramFiles%\7-Zip\7z.exe",
        r"%ProgramFiles(x86)%\7-Zip\7z.exe",
        r"%LOCALAPPDATA%\Programs\7-Zip\7z.exe",
        r"%ProgramW6432%\7-Zip\7z.exe",
        r"%SystemDrive%\Program Files\7-Zip\7z.exe",
    ],
    "darwin": [
        "/opt/homebrew/bin/7zz",
        "/usr/local/bin/7zz",
        "/opt/homebrew/bin/7z",
        "/usr/local/bin/7z",
        "/Applications/Keka.app/Contents/Resources/keka7z",
    ],
    "linux": [
        "/usr/bin/7z",
        "/usr/bin/7zz",
        "/usr/bin/7za",
        "/usr/local/bin/7z",
        "/snap/bin/7z",
        "/var/lib/flatpak/exports/bin/org.7-zip.SevenZIP",
    ],
}

_RAR_CANDIDATES: dict[str, list[str]] = {
    "win32": [
        r"%ProgramFiles%\WinRAR\Rar.exe",
        r"%ProgramFiles%\WinRAR\WinRAR.exe",
        r"%ProgramFiles(x86)%\WinRAR\Rar.exe",
        r"%ProgramFiles%\WinRAR\UnRAR.exe",
    ],
    "darwin": [
        "/usr/local/bin/rar",
        "/opt/homebrew/bin/rar",
        "/Applications/WinRAR.app/Contents/MacOS/rar",
    ],
    "linux": ["/usr/bin/rar", "/usr/local/bin/rar", "/opt/rar/rar"],
}


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def _which_many(names: list[str]) -> str | None:
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def _tool_dir() -> Path:
    """应用自带的二进制目录（开发时为项目内 tools/，打包后为 _MEIPASS/tools）。"""
    base = getattr(sys, "_MEIPASS", None)
    here = Path(__file__).resolve().parent.parent
    candidates = []
    if base:
        candidates.append(Path(base) / "tools")
    candidates += [here / "tools", here / "vendor" / "tools"]
    for c in candidates:
        if c.is_dir():
            return c
    return here / "tools"


def _disabled_by_env(name: str) -> bool:
    """显式禁用某个外部工具（B8：CI 要能真正模拟"纯净机器"）。

    只靠改目录名/清 PATH 都不可靠 —— 7-Zip 还可能在 Program Files、Chocolatey、
    snap、flatpak、Keka 内置等多处（见 _SEVEN_ZIP_CANDIDATES）。这里在查找**入口**
    直接短路，才是可测试、可复现的"无 7z 环境"。
    """
    val = os.environ.get(f"BACKUPSTUDIO_DISABLE_{name.upper()}", "").strip().lower()
    return val not in ("", "0", "false", "no", "off")


def find_7z() -> str | None:
    if _disabled_by_env("7z"):
        return None
    info = get_platform_info()
    key = "win32" if info.is_windows else ("darwin" if info.is_macos else "linux")
    tool_dir = _tool_dir()
    # 应用自带优先（保证开箱即用）。注意 「tools/7z」既是子目录名也可能被当作文件名，
    # 因此显式子目录要在裸目录之前检查。
    for name in ("7za.exe", "7za", "7z.exe", "7zz.exe", "7zz", "7zr.exe", "7z"):
        for sub in (tool_dir / key / "7z", tool_dir / "7z", tool_dir / key, tool_dir):
            p = sub / name
            if p.is_file() and not p.is_dir():
                return str(p)
    found = _which_many(["7zz", "7z", "7za"])
    if found:
        return found
    for cand in _SEVEN_ZIP_CANDIDATES.get(key, []):
        p = Path(_expand(cand))
        if p.is_file():
            return str(p)
    return None


def find_rar() -> str | None:
    if _disabled_by_env("rar"):
        return None
    info = get_platform_info()
    key = "win32" if info.is_windows else ("darwin" if info.is_macos else "linux")
    tool_dir = _tool_dir()
    for sub in (tool_dir / key / "rar", tool_dir / "rar", tool_dir / key, tool_dir):
        for name in ("rar.exe", "rar"):
            p = sub / name
            if p.is_file() and not p.is_dir():
                return str(p)
    found = _which_many(["rar"])
    if found:
        return found
    for cand in _RAR_CANDIDATES.get(key, []):
        p = Path(_expand(cand))
        if p.is_file():
            return str(p)
    return None


def find_unrar() -> str | None:
    if _disabled_by_env("unrar"):
        return None
    found = _which_many(["unrar", "UnRAR", "UNRAR"])
    if found:
        return found
    info = get_platform_info()
    key = "win32" if info.is_windows else ("darwin" if info.is_macos else "linux")
    tool_dir = _tool_dir()
    # 应用自带的 UnRAR 优先（与 find_7z/find_rar 一致）。此前这里只查 PATH 与 Program
    # Files，导致打包版明明带了 tools/win32/rar/UnRAR.exe 却 find_unrar()->None，
    # 于是 .rar 解压落到 7za.exe（无 rar 解码器）→ 报 "Cannot open the file as archive"，
    # 且 rar 炸弹预检（需要 unrar lt）永远不生效。
    for sub in (tool_dir / key / "rar", tool_dir / "rar", tool_dir / key, tool_dir):
        for name in ("UnRAR.exe", "unrar.exe", "unrar"):
            p = sub / name
            if p.is_file() and not p.is_dir():
                return str(p)
    if info.is_windows:
        for cand in (
            r"%ProgramFiles%\WinRAR\UnRAR.exe",
            r"%ProgramFiles%\WinRAR\Rar.exe",
            r"%ProgramFiles%\7-Zip\7z.exe",
        ):
            p = Path(_expand(cand))
            if p.is_file():
                return str(p)
    return None


@lru_cache(maxsize=8)
def find_7z_gui() -> str | None:
    info = get_platform_info()
    if info.is_windows:
        for cand in (r"%ProgramFiles%\7-Zip\7zG.exe", r"%ProgramFiles%\7-Zip\7zFM.exe"):
            p = Path(_expand(cand))
            if p.is_file():
                return str(p)
    return None


def _run_quick(cmd: list[str], timeout: float = 8.0) -> tuple[int, str]:
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "timeout": timeout,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = dict(os.environ, LC_ALL="C", LANG="C")
    kwargs["env"] = env
    try:
        proc = subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        return -1, "not found"
    except subprocess.TimeoutExpired:
        return -2, "timeout"
    except OSError as exc:
        return -3, f"{type(exc).__name__}: {exc}"
    out = (proc.stdout or b"").decode("utf-8", "replace")
    return proc.returncode, out


@lru_cache(maxsize=8)
def probe_7z(path: str | None = None) -> dict[str, str]:
    """返回 7-Zip 版本信息；不可用时返回空 dict。"""
    exe = path or find_7z()
    if not exe:
        return {}
    rc, out = _run_quick([exe, "i"])
    if rc != 0:
        return {}
    versions = {"exe": exe}
    for line in out.splitlines():
        low = line.lower()
        if low.startswith("7-zip") or low.startswith("7zz") or low.startswith("p7zip"):
            versions["version"] = line.strip()
            break
    return versions


@lru_cache(maxsize=8)
def probe_rar(path: str | None = None) -> dict[str, str]:
    exe = path or find_rar()
    if not exe:
        return {}
    rc, out = _run_quick([exe])
    text = out.lower()
    if rc != 0 or "rar" not in text:
        return {}
    versions = {"exe": exe}
    for line in out.splitlines():
        s = line.strip()
        if s.lower().startswith("rar") and any(ch.isdigit() for ch in s):
            versions["version"] = s
            break
    return versions


@lru_cache(maxsize=8)
def probe_unrar_trial(path: str | None = None) -> tuple[bool, bool]:
    """检查 rar 是否可创建压缩包（WinRAR 未注册为试用版），以及是否有 unrar。"""
    exe = path or find_rar()
    if not exe:
        return False, bool(find_unrar())
    is_unrar = Path(exe).name.lower().startswith("unrar")
    if is_unrar:
        return False, True
    try:
        with tempfile.TemporaryDirectory(prefix="bs-rar-") as tmp:
            sample = Path(tmp) / "probe.txt"
            sample.write_text("backupstudio probe\n", encoding="utf-8")
            archive = Path(tmp) / "probe.rar"
            rc, _ = _run_quick(
                [exe, "a", "-idq", "-ep1", "-m0", "-y", str(archive), str(sample)]
            )
        return rc == 0, bool(find_unrar())
    except OSError:
        return False, bool(find_unrar())


def detect_bsdtar_backend() -> tuple[str, str] | None:
    """查找可用的 libarchive 后端：bsdtar / tar.exe / gtar。"""
    for name in ("bsdtar", "gtar", "tar"):
        exe = shutil.which(name)
        if not exe:
            if sys.platform == "win32":
                win_tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
                if name == "tar" and win_tar.is_file():
                    exe = str(win_tar)
            else:
                continue
        try:
            rc, out = _run_quick([exe, "--version"], timeout=5)
        except OSError:
            continue
        low = out.lower()
        if "bsdtar" in low:
            return exe, "bsdtar"
        if "gnu tar" in low:
            return exe, "gnu-tar"
        if "libarchive" in low:
            return exe, "bsdtar"
    return None
