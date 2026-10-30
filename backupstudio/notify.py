"""桌面通知：托盘气泡、系统通知与声音（跨平台）。

优先使用 Qt 托盘气泡（无额外依赖），其次是系统命令：
* Windows : PowerShell BurntToast 不可靠，改用 Qt 托盘 + 任务栏闪烁
* macOS   : ``osascript -e 'display notification ...'``
* Linux   : ``notify-send``（需要 libnotify；GNOME/KDE 默认可用）
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

log = logging.getLogger("backupstudio.notify")


def ensure_windows_aumid(app_id: str, display_name: str = "BackupStudio") -> bool:
    """把应用的 AppUserModelID 注册到当前用户，并绑定显示名。

    ## 为什么必须做（实测缺陷）

    Windows 10/11 的 Toast 通知靠 AUMID 识别"这条通知是谁发的"。系统会去
    ``HKCU\\SOFTWARE\\Classes\\AppUserModelId\\<AUMID>`` 读 ``DisplayName``。

    * 只调 ``SetCurrentProcessExplicitAppUserModelID`` **不够** —— 那只影响
      任务栏分组，通知仍然找不到来源；
    * AUMID 没注册时，Windows 会回退到一个匿名的内部标识，
      通知标题显示成 ``AppId锕鈷?锕鈷...`` 这样的乱码（用户实际遇到的就是它）。

    这里注册到 HKCU（不需要管理员权限），并写入 ``DisplayName``。
    每次启动都写一遍是幂等的，也能覆盖用户重装/换用户名后的缺失。
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key_path = rf"SOFTWARE\Classes\AppUserModelId\{app_id}"
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
        # 同时把进程 AUMID 设上，确保任务栏/通知都归到这个 ID
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("注册 AppUserModelID 失败（通知可能显示为乱码）：%s", exc)
        return False



def _popen(argv: list[str]) -> bool:
    try:
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, **kwargs)
        return True
    except Exception as exc:
        log.debug("通知命令失败 %s: %s", argv[0], exc)
        return False


def native_notification(title: str, message: str, urgency: str = "normal") -> bool:
    """通过系统能力弹出通知。"""
    if sys.platform == "darwin":
        script = (
            f'display notification {_applescript_quote(message)} '
            f'with title "BackupStudio" subtitle {_applescript_quote(title)}'
        )
        return _popen(["osascript", "-e", script])
    if sys.platform.startswith("linux"):
        notify = shutil.which("notify-send")
        if notify:
            argv = [notify, "-a", "BackupStudio", "-u", urgency, "-i", "backupstudio", title, message]
            return _popen(argv)
        kdialog = shutil.which("kdialog")
        if kdialog:
            return _popen([kdialog, "--title", "BackupStudio", "--passivepopup", message, "8"])
        return False
    if sys.platform == "win32":
        # Windows 10/11 的 Toast 需要 AppUserModelID + 打包，PowerShell 方案不稳定；
        # 由 GUI 层用托盘气泡代替，这里只在无 GUI 时尝试 msg.exe（仅限本地会话）
        msg = shutil.which("msg") or (
            str(shutil.which("msg.exe")) if shutil.which("msg.exe") else None
        )
        if msg:
            return _popen([msg, "*", "/TIME:10", f"{title}: {message}"])
        return False
    return False


def _applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def play_sound() -> bool:
    """播放提示音（失败则静默）。"""
    try:

        if sys.platform == "win32":
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return True
        if sys.platform == "darwin":
            return _popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
        return _popen(["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"]) or _popen(
            ["canberra-gtk-play", "-i", "complete"]
        )
    except Exception:
        return False


def in_quiet_hours(quiet_hours: bool, start: str, end: str) -> bool:
    """判断当前是否处于免打扰时段（支持跨午夜）。"""
    if not quiet_hours:
        return False
    from datetime import datetime

    def parse(t: str) -> int:
        try:
            h, m = str(t).split(":")
            return (int(h) % 24) * 60 + int(m) % 60
        except (ValueError, AttributeError):
            return 0

    now = datetime.now()
    cur = now.hour * 60 + now.minute
    s, e = parse(start), parse(end)
    if s == e:
        return False
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e


def describe_backend() -> str:
    if sys.platform == "darwin":
        return "osascript"
    if sys.platform.startswith("linux"):
        if shutil.which("notify-send"):
            return "notify-send"
        if shutil.which("kdialog"):
            return "kdialog"
        return "无（请安装 libnotify-bin / notify-osd）"
    if sys.platform == "win32":
        return "托盘气泡 + 任务栏闪烁（无需额外组件）"
    return "不支持"
