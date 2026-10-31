"""按本机实际能力计算「压缩格式 / 分卷 / 密码」矩阵。

所有判断都来自运行时探测：
* 7-Zip / RAR 是否存在、版本、是否精简版、RAR 是否处于未注册试用状态；
* tar 后端与各编解码器（gz/bz2/xz/zst）是否可用；
* GPG 是否存在。

GUI 的格式下拉框、分卷与密码选项、以及「缺少哪些软件」页签都使用这里的结果。
"""

from __future__ import annotations

import shutil
import sys
from functools import lru_cache
from pathlib import Path

from .platform_info import get_platform_info
from .tools import find_7z, find_rar, find_unrar, probe_7z, probe_rar, probe_unrar_trial

try:  # 可选依赖
    import zstandard as _zstd_mod  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    _zstd_mod = None


FORMAT_LABELS: dict[str, str] = {
    "zip": "ZIP (.zip)",
    "7z": "7-Zip (.7z)",
    "rar": "RAR (.rar)",
    "tar": "tar 归档 (.tar)",
    "tar.gz": "tar + gzip (.tar.gz)",
    "tar.bz2": "tar + bzip2 (.tar.bz2)",
    "tar.xz": "tar + xz (.tar.xz)",
    "tar.zst": "tar + zstd (.tar.zst)",
}

TAR_CODECS = {"tar.gz": "gz", "tar.bz2": "bz2", "tar.xz": "xz", "tar.zst": "zst"}

MULTIVOLUME_LABELS: dict[str, str] = {
    "7z-split": "7-Zip 原生分卷（.001/.002…）",
    "rar-native": "RAR 原生分卷（part1.rar…）",
    "split": "通用字节切片（.part01/.part02…）",
}

ENCRYPTION_LABELS: dict[str, str] = {
    "aes256": "AES-256（强加密）",
    "zipcrypto": "ZipCrypto（兼容性好、强度弱）",
    "gpg": "GPG 对称加密（需要 gpg）",
}

_MULTIVOLUME_NOTES = {
    "7z-split": "7-Zip 原生分卷，解压时只需选择第一个 .001 文件",
    "rar-native": "RAR 原生分卷（part1.rar / part2.rar…）",
    "split": "通用分卷：整包生成后按字节切块（.part01、.part02…），拼接后即可解压",
}

_ENCRYPTION_NOTES = {
    "aes256": "AES-256 强加密",
    "zipcrypto": "ZipCrypto 传统加密：兼容性最好但强度弱，仅建议存放非敏感数据",
    "gpg": "通过 GPG 对称加密整包（需要系统安装 gpg）",
}

# 每种格式的基线能力
_BASELINE: dict[str, dict] = {
    "zip": {"multivolume": ["7z-split"], "password": ["aes256", "zipcrypto"]},
    "7z": {"multivolume": ["7z-split"], "password": ["aes256"]},
    "rar": {"multivolume": ["rar-native"], "password": ["aes256"]},
    "tar": {"multivolume": ["split"], "password": ["gpg"]},
    "tar.gz": {"multivolume": ["split"], "password": ["gpg"]},
    "tar.bz2": {"multivolume": ["split"], "password": ["gpg"]},
    "tar.xz": {"multivolume": ["split"], "password": ["gpg"]},
    "tar.zst": {"multivolume": ["split"], "password": ["gpg"]},
}

# 需要什么软件（用户可见）
_REQUIREMENTS: dict[str, list[tuple[str, str]]] = {
    # (软件包名, 说明)
    "zip": [("7zip", "7-Zip（ZIP AES-256 需要完整版 7z.exe；7za 精简版只能 ZipCrypto）")],
    "7z": [("7zip", "7-Zip（推荐用 7z.exe 完整版）")],
    "rar": [("rar", "WinRAR / RAR（专有软件，需自行安装并同意授权）")],
    "tar": [("tar", "tar（Windows 10+ 自带 bsdtar；Linux/macOS 一般已安装）")],
    "tar.gz": [("tar", "tar（Windows 10+ 自带 bsdtar）")],
    "tar.bz2": [("tar", "tar（需要 bzip2 支持）")],
    "tar.xz": [("xz", "GNU tar 或带 liblzma 的 bsdtar")],
    "tar.zst": [("zstd", "zstd（或 Python 的 zstandard 模块）")],
}

_PKG_HINT = {
    "7zip": {"win32": ("7zip", "7-Zip"), "darwin": ("p7zip", "p7zip"), "linux": ("p7zip-full", "p7zip-full")},
    "rar": {"win32": ("winrar", "WinRAR"), "darwin": ("rar", "RAR"), "linux": ("rar", "rar")},
    "tar": {"win32": ("tar", "tar（已内置）"), "darwin": ("tar", "tar"), "linux": ("tar", "tar")},
    "xz": {"win32": ("xz", "xz-utils / bsdtar"), "darwin": ("xz", "xz"), "linux": ("xz-utils", "xz-utils")},
    "zstd": {"win32": ("zstd", "zstd"), "darwin": ("zstd", "zstd"), "linux": ("zstd", "zstd")},
}


def archive_engine_for(fmt: str) -> str:
    """当前会由哪个引擎处理该格式：external-7z / python / py7zr / rar-native。"""
    report = environment_report()
    seven_any = bool(report["seven_zip"])
    rar_ok = bool(report["rar"]) and report["rar_can_write"]
    if fmt in ("zip", "7z"):
        if seven_any:
            return "external-7z"
        if fmt == "7z" and _py7zr_available():
            return "py7zr"
        return "python" if fmt == "zip" else "none"
    if fmt == "rar":
        if rar_ok:
            return "rar-native"
        return "forced" if (seven_any or _py7zr_available()) else "none"
    return "python"


def platform_key() -> str:
    info = get_platform_info()
    if info.is_windows:
        return "win32"
    if info.is_macos:
        return "darwin"
    return "linux"


def _is_full_7zip(exe: str | None) -> bool:
    if not exe:
        return False
    name = Path(exe).name.lower()
    if name in {"7za.exe", "7za", "7zr.exe", "7zr"}:
        return False
    return True


def _tar_backend() -> tuple[str, bool]:
    """返回 (后端名, 是否 GNU tar)。"""
    exe = shutil.which("tar")
    if not exe and sys.platform == "win32":
        import os

        cand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
        if cand.is_file():
            exe = str(cand)
    if not exe:
        return "", False
    try:
        import subprocess

        out = subprocess.run(
            [exe, "--version"], capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        ).stdout.decode("utf-8", "replace").lower()
    except Exception:
        return exe, False
    if "gnu tar" in out:
        return exe, True
    if "bsdtar" in out or "libarchive" in out:
        return exe, False
    return exe, False


@lru_cache(maxsize=2)
def _codec_support() -> dict[str, bool]:
    exe, gnu = _tar_backend()
    support = {"gz": bool(exe), "bz2": bool(exe), "xz": False, "zst": False}
    if not exe:
        return support
    if gnu:
        support["xz"] = True
    else:
        # Windows 自带的 bsdtar 链接了 liblzma，可直接用 -J 生成 xz；
        # 其它平台的 bsdtar 则需要系统 xz 程序。
        support["xz"] = sys.platform == "win32" or shutil.which("xz") is not None
    support["zst"] = support["zst"] or shutil.which("zstd") is not None or _zstd_mod is not None
    support["zst"] = support["zst"] or _zstd_mod is not None or shutil.which("zstd") is not None
    return support


@lru_cache(maxsize=2)
def environment_report() -> dict:
    """扫描本机环境，返回原始探测结果。"""
    info = get_platform_info()
    seven = find_7z()
    rar = find_rar()
    unrar = find_unrar()
    seven_info = probe_7z(seven) if seven else {}
    rar_info = probe_rar(rar) if rar else {}
    rar_can_write = False
    if rar:
        try:
            rar_can_write = probe_unrar_trial(rar)[0]
        except Exception:
            rar_can_write = False
    tar_exe, gnu_tar = _tar_backend()
    return {
        "platform": info,
        "seven_zip": seven,
        "seven_zip_full": _is_full_7zip(seven),
        "seven_zip_version": seven_info.get("version", ""),
        "rar": rar,
        "rar_can_write": rar_can_write,
        "rar_version": rar_info.get("version", ""),
        "unrar": unrar,
        "gpg": shutil.which("gpg"),
        "tar": tar_exe,
        "gnu_tar": gnu_tar,
        "codecs": dict(_codec_support()),
        "zstd_module": _zstd_mod is not None,
    }


def reset_cache() -> None:
    environment_report.cache_clear()
    _codec_support.cache_clear()


def package_hint(requirement: str) -> str:
    """某个需求对应的软件名与安装命令。"""
    info = get_platform_info()
    pkg_map = _PKG_HINT.get(requirement, {"linux": (requirement, requirement)})
    pkg, display = pkg_map.get(platform_key(), (requirement, requirement))
    if info.is_windows and requirement == "7zip":
        command = "winget install --id 7zip.7zip -e"
    elif info.is_windows and requirement == "rar":
        command = "安装 WinRAR：https://www.rarlab.com/download.htm"
    else:
        command = info.install_command(pkg)
    return f"{display}  →  {command}"


def format_capabilities(force: bool = True) -> dict[str, dict]:
    """返回每种格式的可用性、分卷、密码与缺失项说明。

    ``force`` 语义（审查 P0-13）：
    * True （默认，引擎/CLI 视角）：允许把"可用但需强制"的格式计为可用；
    * False（GUI 展示视角）：如实反映"不勾强制压缩时能否直接成功"，
      并在 entry 上附加 ``usable_with_force`` 与 ``needs_force`` 供界面三态展示。
    """
    report = environment_report()
    seven_full = bool(report["seven_zip"]) and report["seven_zip_full"]
    seven_any = bool(report["seven_zip"])
    rar_ok = bool(report["rar"]) and report["rar_can_write"]
    codecs = report["codecs"]
    tar_ok = bool(report["tar"])
    force_path_zip = seven_any or _py7zr_available()  # rar 强制降级为 ZIP 容器的前提

    result: dict[str, dict] = {}
    for fmt, base in _BASELINE.items():
        entry: dict = {
            "available": True,
            "usable_with_force": True,
            "needs_force": False,
            "reason": "",
            "warnings": [],
            "missing": [],
            "multivolume": {},
            "password": {},
            "engine": "",
            "engine_version": "",
        }
        codec = TAR_CODECS.get(fmt, "")
        if fmt in ("zip", "7z"):
            if seven_any:
                entry["available"] = True
                entry["engine"] = report["seven_zip"]
                entry["engine_version"] = report["seven_zip_version"]
            elif fmt == "7z":
                try:
                    import py7zr  # noqa: F401, PLC0415

                    entry["available"] = force
                    entry["usable_with_force"] = True
                    entry["needs_force"] = not force
                    entry["engine_version"] = "py7zr（内置）"
                    if not force:
                        entry["reason"] = "仅安装内置 py7zr；未安装 7-Zip，需启用「强制压缩」"
                except ImportError:
                    entry["available"] = False
                    entry["reason"] = "未安装 7-Zip"
            if fmt == "zip" and not seven_any:
                entry["engine"] = "python-zipfile"
        elif fmt == "rar":
            entry["available"] = rar_ok or (force and force_path_zip)
            entry["usable_with_force"] = force_path_zip
            entry["needs_force"] = (not rar_ok) and force_path_zip
            if not rar_ok:
                if report["rar"] and not report["rar_can_write"]:
                    entry["reason"] = "检测到 RAR，但当前是未注册试用版：只能解压，不能创建压缩包"
                elif report["rar"]:
                    entry["reason"] = "RAR 不可用"
                else:
                    entry["reason"] = "未安装 WinRAR / RAR（专有软件）"
                if force and force_path_zip:
                    entry["reason"] += "；将强制使用 ZIP 容器（扩展名为 .rar）"
            entry["engine"] = report["rar"] or ""
            entry["engine_version"] = report["rar_version"]
        else:
            # plain tar 无编解码器（codec == ""）——只需 tar 后端本身可用
            codec_ok = (not codec) or codecs.get(codec, False)
            entry["available"] = tar_ok and codec_ok
            if not tar_ok:
                entry["reason"] = "系统中未找到 tar"
            elif not codec_ok:
                entry["reason"] = f"当前 tar 后端不支持 {codec} 压缩"
            entry["engine"] = report["tar"] or ""
            entry["engine_version"] = "GNU tar" if report["gnu_tar"] else "bsdtar"

        if not entry["available"]:
            for need, _desc in _REQUIREMENTS[fmt]:
                if need == "7zip" and not seven_any:
                    entry["missing"].append(need)
                elif need == "rar" and not rar_ok:
                    entry["missing"].append(need)
                elif need in ("xz", "zstd") and not codecs.get(TAR_CODECS.get(fmt, ""), False):
                    entry["missing"].append(need)

        # 分卷能力
        for mode in base["multivolume"]:
            if mode == "7z-split":
                ok = seven_any and fmt == "7z"
                reason = "" if ok else "需要 7-Zip"
            elif mode == "rar-native":
                ok = rar_ok and fmt == "rar"
                reason = "" if ok else "需要可创建压缩包的 RAR"
            else:
                ok = entry["available"]
                reason = "" if ok else "主格式不可用"
            entry["multivolume"][mode] = {"available": ok, "reason": reason, "note": _MULTIVOLUME_NOTES[mode]}
        entry["default_multivolume"] = next((m for m in base["multivolume"] if entry["multivolume"][m]["available"]), "")

        # 密码能力
        for mode in base["password"]:
            if mode == "aes256":
                if fmt == "zip":
                    ok = seven_full and fmt == "zip"
                    reason = "" if ok else "ZIP AES-256 需要完整版 7z.exe（当前为 7za 精简版或未安装 7-Zip）"
                elif fmt == "rar":
                    ok = rar_ok
                    reason = "" if ok else "需要可创建压缩包的 RAR"
                else:
                    ok = seven_any
                    reason = "" if ok else "需要 7-Zip"
            elif mode == "zipcrypto":
                ok = seven_any
                reason = "" if seven_any else "需要 7-Zip 写入加密 ZIP"
            else:  # gpg
                # gpg 加密在引擎中尚未实现（archives.py 无任何 gpg 代码）：
                # 标记为不可用，避免"记录已加密而实际明文"的静默失败。
                ok = False
                reason = "GPG 加密尚未实现（后续版本提供）"
            entry["password"][mode] = {
                "available": ok,
                "reason": reason,
                "note": _ENCRYPTION_NOTES[mode],
                "safe": mode != "zipcrypto",
            }
        entry["password_default"] = next((m for m in base["password"] if entry["password"][m]["available"]), "")
        result[fmt] = entry
    return result


def _py7zr_available() -> bool:
    try:
        import py7zr  # noqa: F401, PLC0415
        return True
    except ImportError:
        return False


def format_status_line(fmt: str, entry: dict) -> str:
    if entry["available"]:
        if fmt == "rar" and not entry.get("engine"):
            return "可用（强制：ZIP 容器）"
        if fmt == "7z" and not entry.get("engine"):
            return "可用（内置 py7zr）"
        return "可用"
    # 三态展示（审查 P0-13）：不勾强制不可用、但强制可降级 → 明确引导
    if entry.get("needs_force") or entry.get("usable_with_force"):
        return f"需要勾选「强制压缩」— {entry['reason']}"
    return f"不可用 — {entry['reason']}"


def missing_requirements(caps: dict[str, dict] | None = None) -> list[tuple[str, str, str]]:
    """汇总缺失软件：[(格式, 需求名, 说明)]。"""
    caps = caps or format_capabilities()
    rows: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for fmt, entry in caps.items():
        for need in entry.get("missing", []):
            key = (need,)
            if key in seen:
                continue
            seen.add(key)
            desc = dict(_REQUIREMENTS[fmt]).get(need, "")
            rows.append((fmt, package_hint(need), desc))
    return rows


def capability_report_text(caps: dict[str, dict] | None = None) -> str:
    caps = caps or format_capabilities()
    info = get_platform_info()
    lines = [f"系统：{info.label}", f"Python：{info.python}", ""]
    for fmt, entry in caps.items():
        label = FORMAT_LABELS.get(fmt, fmt)
        lines.append(f"{label:<28} {format_status_line(fmt, entry)}")
        if entry["engine"]:
            lines.append(f"    引擎：{entry['engine']}  {entry['engine_version']}")
        for mode, info_m in entry["multivolume"].items():
            mark = "✓" if info_m["available"] else "✗"
            lines.append(f"    分卷 {MULTIVOLUME_LABELS.get(mode, mode)}：{mark}" + ("" if info_m["available"] else f"（{info_m['reason']}）"))
        for mode, info_e in entry["password"].items():
            mark = "✓" if info_e["available"] else "✗"
            lines.append(f"    密码 {ENCRYPTION_LABELS.get(mode, mode)}：{mark}" + ("" if info_e["available"] else f"（{info_e['reason']}）"))
        for w in entry["warnings"]:
            lines.append(f"    ⚠ {w}")
        lines.append("")
    missing = missing_requirements(caps)
    if missing:
        lines.append("缺失的软件：")
        for fmt, hint, desc in missing:
            lines.append(f"  [{FORMAT_LABELS.get(fmt, fmt)}] {hint}")
            if desc:
                lines.append(f"        {desc}")
    else:
        lines.append("缺失的软件：无")
    return "\n".join(lines)
