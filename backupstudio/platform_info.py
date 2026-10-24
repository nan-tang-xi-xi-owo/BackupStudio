"""操作系统探测：Windows / macOS / Linux（含发行版、包管理器、桌面环境）。

设计目标：
* 任何探测失败都不能让程序崩溃，一律返回描述文字而不是抛异常；
* 结果可缓存，GUI 与 CLI 共用；
* Linux 发行版同时解析 ``/etc/os-release`` 与 ``/etc/lsb-release`` 等常见文件。
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_OS_RELEASE_KEYS = (
    "ID",
    "ID_LIKE",
    "NAME",
    "PRETTY_NAME",
    "VERSION",
    "VERSION_ID",
    "VERSION_CODENAME",
    "UBUNTU_CODENAME",
)

# 包管理器 -> (命令, 安装命令模板)
PACKAGE_MANAGERS: dict[str, str] = {
    "apt": "sudo apt install -y {pkg}",
    "apt-get": "sudo apt-get install -y {pkg}",
    "dnf": "sudo dnf install -y {pkg}",
    "yum": "sudo yum install -y {pkg}",
    "pacman": "sudo pacman -S --needed {pkg}",
    "zypper": "sudo zypper install -y {pkg}",
    "apk": "sudo apk add {pkg}",
    "xbps-install": "sudo xbps-install -y {pkg}",
    "emerge": "sudo emerge -av {pkg}",
    "nix-env": "nix-env -iA nixpkgs.{pkg}",
    "pkg": "sudo pkg install -y {pkg}",
    "brew": "brew install {pkg}",
    "snap": "sudo snap install {pkg}",
    "flatpak": "flatpak install -y flathub {pkg}",
}

# 发行版 -> 首选包管理器
DISTRO_PACKAGE_MANAGER: dict[str, str] = {
    "ubuntu": "apt",
    "debian": "apt",
    "linuxmint": "apt",
    "pop": "apt",
    "elementary": "apt",
    "kali": "apt",
    "raspbian": "apt",
    "zorin": "apt",
    "deepin": "apt",
    "uos": "apt",
    "kylin": "apt",
    "neokylin": "dnf",
    "fedora": "dnf",
    "rhel": "dnf",
    "centos": "dnf",
    "rocky": "dnf",
    "almalinux": "dnf",
    "ol": "dnf",
    "openeuler": "dnf",
    "anolis": "dnf",
    "amzn": "dnf",
    "arch": "pacman",
    "manjaro": "pacman",
    "endeavouros": "pacman",
    "garuda": "pacman",
    "opensuse-tumbleweed": "zypper",
    "opensuse-leap": "zypper",
    "sles": "zypper",
    "alpine": "apk",
    "void": "xbps-install",
    "gentoo": "emerge",
    "nixos": "nix-env",
    "freebsd": "pkg",
}


@dataclass
class PlatformInfo:
    system: str  # "Windows" | "Darwin" | "Linux" | 其他
    machine: str
    release: str
    version: str
    python: str
    is_windows: bool = False
    is_macos: bool = False
    is_linux: bool = False
    is_frozen: bool = False
    # Linux 专用
    distro_id: str = ""
    distro_like: tuple[str, ...] = ()
    distro_name: str = ""
    distro_version: str = ""
    pretty_name: str = ""
    desktop: str = ""
    session_type: str = ""
    container: str = ""
    package_manager: str = ""
    package_manager_command: str = ""
    branch: str = ""  # Windows: win10 / win11；macOS: 版本号
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """人类可读的一行描述，例如 ``Linux · Ubuntu 24.04 (x86_64)``。"""
        if self.is_windows:
            name = self.branch or "Windows"
            if not name.startswith("Windows"):
                name = "Windows " + name
            return f"{name} · Build {self.extra.get('build') or self.version} ({self.machine})"
        if self.is_macos:
            return f"macOS {self.release} ({self.machine})"
        if self.is_linux:
            distro = self.pretty_name or self.distro_name or "Linux"
            return f"{distro} ({self.machine})"
        return f"{self.system} {self.release} ({self.machine})"

    def install_command(self, package: str) -> str:
        """给出安装某个软件包的建议命令。"""
        if self.is_windows:
            if package in ("7z", "7za"):
                return "winget install --id 7zip.7zip  # 或 choco install 7zip"
            if package == "rar":
                return "winget install --id RARLab.WinRAR  # 或从 rarlab.com 下载"
            return f"winget search {package}"
        if self.is_macos:
            return f"brew install {package}"
        manager = self.package_manager or "apt"
        tpl = PACKAGE_MANAGERS.get(manager, "sudo {pm} install -y {pkg}")
        return tpl.format(pm=manager, pkg=package)

    def distro_tokens(self) -> set[str]:
        tokens = {self.distro_id.lower()} | {t.lower() for t in self.distro_like}
        return {t for t in tokens if t}


def _read_os_release(paths: tuple[str, ...] = ("/etc/os-release", "/usr/lib/os-release")) -> dict[str, str]:
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        data: dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            data[key.strip()] = value
        if data:
            return data
    return {}


def _read_simple(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _detect_container() -> str:
    if Path("/.dockerenv").exists():
        return "docker"
    if Path("/run/.containerenv").exists():
        return "podman"
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for marker in ("lxc", "docker", "podman", "kubepods", "gvisor"):
        if marker in cgroup:
            return marker
    return ""


def _detect_linux_distro(info: PlatformInfo) -> None:
    osr = _read_os_release()
    if not osr:
        lsb = _read_simple("/etc/lsb-release")
        if lsb:
            for line in lsb.splitlines():
                k, _, v = line.partition("=")
                osr[k.strip()] = v.strip().strip('"')
        if Path("/etc/redhat-release").exists():
            osr.setdefault("PRETTY_NAME", _read_simple("/etc/redhat-release"))
        if Path("/etc/arch-release").exists():
            osr.setdefault("ID", "arch")
            osr.setdefault("PRETTY_NAME", "Arch Linux")

    info.distro_id = (osr.get("ID") or "").strip().lower()
    like = osr.get("ID_LIKE") or ""
    info.distro_like = tuple(x.lower() for x in like.replace(",", " ").split() if x)
    info.distro_name = osr.get("NAME", "")
    info.distro_version = osr.get("VERSION_ID") or osr.get("VERSION") or ""
    info.pretty_name = osr.get("PRETTY_NAME") or info.distro_name
    info.extra = {k: osr.get(k, "") for k in _OS_RELEASE_KEYS if osr.get(k)}

    # 桌面环境
    info.desktop = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or ""
    if not info.desktop:
        try:
            # 部分发行版把桌面写进 /etc/environment，作为最后的兜底
            for line in _read_simple("/etc/environment").splitlines():
                if line.startswith("XDG_CURRENT_DESKTOP="):
                    info.desktop = line.split("=", 1)[1].strip('"')
                    break
        except Exception:  # pragma: no cover
            pass
    info.session_type = os.environ.get("XDG_SESSION_TYPE") or (
        "wayland" if os.environ.get("WAYLAND_DISPLAY") else ""
    )
    info.extra["session.desktop"] = info.desktop
    info.container = _detect_container()

    # 包管理器：先看发行版映射，再看 PATH 中实际可用的
    for token in (info.distro_id, *info.distro_like):
        pm = DISTRO_PACKAGE_MANAGER.get(token)
        if pm and shutil.which(pm):
            info.package_manager = pm
            break
    if not info.package_manager:
        for pm in ("apt", "dnf", "yum", "pacman", "zypper", "apk", "xbps-install", "emerge", "pkg"):
            if shutil.which(pm):
                info.package_manager = pm
                break
    if not info.package_manager:
        for token in (info.distro_id, *info.distro_like):
            pm = DISTRO_PACKAGE_MANAGER.get(token)
            if pm:
                info.package_manager = pm
                break
    if info.package_manager:
        info.package_manager_command = PACKAGE_MANAGERS.get(info.package_manager, "")


def _detect_windows(info: PlatformInfo) -> None:
    try:
        ver = sys.getwindowsversion()
        build = str(ver.build)
        release = f"{ver.major}.{ver.minor}"
    except Exception:  # pragma: no cover - 非 Windows 解释器
        build = info.version or ""
        release = info.release or ""
    info.release = release or info.release
    is_11 = build.isdigit() and int(build) >= 22000
    info.branch = "Windows 11" if is_11 else "Windows 10"
    if release:
        info.branch += f" {release}"
    info.extra["build"] = build
    winget = shutil.which("winget")
    if not winget:
        for cand in (
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "winget.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps" / "winget.exe",
        ):
            if cand.is_file():
                winget = str(cand)
                break
    info.package_manager = "winget" if winget else ""
    info.package_manager_command = "winget install --id {pkg}" if winget else ""


def _detect_macos(info: PlatformInfo) -> None:
    info.branch = info.release
    info.package_manager = "brew" if shutil.which("brew") else ""
    info.package_manager_command = "brew install {pkg}"
    info.extra["product"] = platform.mac_ver()[0]


@lru_cache(maxsize=1)
def get_platform_info() -> PlatformInfo:
    system = platform.system()
    info = PlatformInfo(
        system=system,
        machine=platform.machine() or "unknown",
        release=platform.release(),
        version=platform.version(),
        python=platform.python_version(),
        is_windows=system == "Windows" or os.name == "nt",
        is_macos=system == "Darwin",
        is_linux=system == "Linux",
        is_frozen=bool(getattr(sys, "frozen", False)),
    )
    try:
        if info.is_windows:
            _detect_windows(info)
        elif info.is_macos:
            _detect_macos(info)
        elif info.is_linux:
            _detect_linux_distro(info)
    except Exception as exc:  # 探测失败不能影响主流程
        info.extra["detect.error"] = f"{type(exc).__name__}: {exc}"
    return info


def reset_platform_cache() -> None:
    """测试用：清空缓存。"""
    get_platform_info.cache_clear()


def describe_environment() -> list[tuple[str, str]]:
    """返回 (项目, 值) 形式的环境摘要，供 CLI / GUI 展示。"""
    info = get_platform_info()
    rows: list[tuple[str, str]] = [
        ("系统", info.system),
        ("系统描述", info.label),
        ("架构", info.machine),
        ("内核 / 版本", f"{info.release} · {info.version}"),
        ("Python", info.python),
        ("打包形态", "冻结程序 (exe/dmg/AppImage)" if info.is_frozen else "源码运行"),
    ]
    if info.is_linux:
        rows += [
            ("发行版 ID", info.distro_id or "未知"),
            ("发行版名称", info.pretty_name or "未知"),
            ("版本", info.distro_version or "未知"),
            ("同族 (ID_LIKE)", " ".join(info.distro_like) or "-"),
            ("桌面环境", info.desktop or "-"),
            ("会话类型", info.session_type or "-"),
            ("容器", info.container or "-"),
            ("包管理器", info.package_manager or "未找到"),
        ]
    elif info.is_macos:
        rows += [("包管理器", info.package_manager or "未找到 Homebrew")]
    elif info.is_windows:
        rows += [("版本分支", info.branch), ("构建号", info.extra.get("build", "")), ("包管理器", info.package_manager or "未找到 winget")]
    # N3/N4：运行在 Flatpak 沙箱内时把真实权限边界摆到明面上（否则用户会以为
    # 「开机自启已开启」「定时备份会跑」，而两者在沙箱里都可能不成立）。
    try:
        from . import flatpak  # 延迟导入：非 Linux 平台不必付这个成本

        rows += flatpak.describe()
    except Exception:  # noqa: BLE001 - 环境探测永远不能拖垮 info/界面
        pass
    return rows
