"""跨平台进程与自启动管理。

* 单实例运行：基于 ``psutil`` 检查进程命令行，避免 Windows 上文件锁无法判断任意 pid 的复用问题；
* 开机自启动：Windows 注册表 Run 项、macOS LaunchAgent、Linux XDG autostart / systemd --user；
* 守护进程拉起与停止：``spawn_daemon`` / ``stop_daemon``。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import APP_NAME
from .paths import config_dir

DAEMON_MODULE = "backupstudio.daemon"
DAEMON_MARKER = "--daemon"


# --------------------------------------------------------------------------
# 进程探测
# --------------------------------------------------------------------------

def _psutil():
    try:
        import psutil  # noqa: PLC0415
        return psutil
    except ImportError:  # pragma: no cover - 打包后可选项
        return None


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def daemon_command(*extra: str) -> list[str]:
    """构建守护进程启动命令。"""
    if is_frozen():
        return [sys.executable, DAEMON_MARKER, *extra]
    return [sys.executable, "-m", DAEMON_MODULE, *extra]


def daemon_alive_markers() -> list[str]:
    if is_frozen():
        return [DAEMON_MARKER]
    return [DAEMON_MODULE]


def pid_matches_daemon(pid: int) -> bool:
    """判断某个 pid 是否确实是我们的守护进程（防止 pid 复用误判）。"""
    ps = _psutil()
    if ps is None:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        proc = ps.Process(pid)
        cmdline = proc.cmdline()
    except Exception:
        return False
    joined = " ".join(cmdline)
    return any(marker in joined for marker in daemon_alive_markers())


def read_pid(pid_file: Path) -> int | None:
    try:
        text = pid_file.read_text(encoding="ascii").strip().splitlines()
        if not text:
            return None
        return int(text[0].split()[0])
    except (OSError, ValueError, IndexError):
        return None


_SCAN_CACHE: dict[str, float] = {}


def find_daemon_pid(pid_file: Path, allow_scan: bool = False) -> int | None:
    """查找守护进程 PID。

    优先信任 PID 文件（校验进程身份），只有在 PID 文件缺失/失效时才做一次全进程扫描；
    扫描结果会缓存几秒，避免界面每次刷新都付出上百毫秒~数秒的代价。
    """
    pid = read_pid(pid_file)
    if pid and pid_matches_daemon(pid):
        return pid
    if not allow_scan:
        return None
    import time

    now = time.monotonic()
    cached_at, cached_pid = _SCAN_CACHE.get("at", 0.0), _SCAN_CACHE.get("pid", 0.0)
    if now - cached_at < 5.0:
        cpid = int(cached_pid)
        return cpid if cpid and pid_matches_daemon(cpid) else None
    found: int | None = None
    ps = _psutil()
    if ps is not None:
        for proc in ps.process_iter(["pid"]):
            try:
                cmdline = proc.cmdline()
            except Exception:
                continue
            if any(m in " ".join(cmdline) for m in daemon_alive_markers()):
                found = int(proc.pid)
                break
    _SCAN_CACHE["at"], _SCAN_CACHE["pid"] = now, float(found or 0)
    return found


def app_instance_pid(lock_path: Path) -> int | None:
    """GUI 单实例检测：锁文件 + psutil 校验。"""
    try:
        pid = int(lock_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    ps = _psutil()
    if ps is None:
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            return None
    try:
        proc = ps.Process(pid)
        cmd = " ".join(proc.cmdline())
    except Exception:
        return None
    if DAEMON_MARKER in cmd:
        return None
    return pid


# --------------------------------------------------------------------------
# 守护进程控制
# --------------------------------------------------------------------------

def spawn_daemon(minimized: bool = False) -> subprocess.Popen | None:
    """以分离方式拉起守护进程，返回 Popen。"""
    cmd = daemon_command()
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        flags = 0
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0) if minimized else 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ)
    env.setdefault("BACKUPSTUDIO_DAEMON", "1")
    kwargs["env"] = env
    try:
        return subprocess.Popen(cmd, **kwargs)
    except OSError:
        return None


def stop_daemon(pid_file: Path, timeout: float = 8.0) -> bool:
    """请求守护进程退出；先发温和信号，超时再强制。"""
    pid = find_daemon_pid(pid_file)
    if not pid:
        try:
            pid_file.unlink()
        except OSError:
            pass
        return True
    ps = _psutil()
    try:
        if sys.platform == "win32" and ps is not None:
            proc = ps.Process(pid)
            parents = [proc] + proc.parents()
            for p in parents:
                if p.name().lower().startswith(("python", "backupstudio", "pythonw")):
                    try:
                        p.terminate()
                    except Exception:
                        pass
                    try:
                        p.wait(timeout=timeout)
                    except Exception:
                        p.kill()
                    break
            else:
                os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not pid_matches_daemon(pid):
                return True
            time.sleep(0.15)
        if ps is not None:
            try:
                ps.Process(pid).kill()
            except Exception:
                pass
        else:
            os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
        return True
    except Exception:
        return False
    finally:
        try:
            pid_file.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# 开机自启动
# --------------------------------------------------------------------------

def gui_command() -> list[str]:
    """自启动用的 GUI 启动命令（`--minimized` 由各平台写入点统一追加）。

    统一使用 ``start`` 子命令：``tray --minimized`` 这类旧命令从未被 argparse 接受
    （登录后 rc=2 立即退出），旧注册表项由 ``tray`` 子命令的兼容参数兜底。
    """
    if is_frozen():
        return [sys.executable, "start"]
    return [sys.executable, "-m", "backupstudio", "start"]


def launch_spec() -> tuple[str, list[str], str]:
    """返回 (可执行文件, 参数, 工作目录)。"""
    cmd = gui_command()
    workdir = str(Path(__file__).resolve().parent.parent)
    return cmd[0], cmd[1:], workdir


def _quote_windows(arg: str) -> str:
    if not arg or any(c in arg for c in ' \t"'):
        return '"' + arg.replace('"', r'\"') + '"'
    return arg


_AUTOSTART_NAME = APP_NAME
_LINUX_DESKTOP = "backupstudio.desktop"


def is_autostart_enabled() -> bool:
    try:
        if sys.platform == "win32":
            import winreg  # noqa: PLC0415
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.QueryValueEx(key, _AUTOSTART_NAME)
            return True
        if sys.platform == "darwin":
            return _mac_plist_path().exists()
        return _linux_desktop_path().exists() or _systemd_unit_path().exists()
    except Exception:
        return False


def _autostart_argv() -> list[str]:
    """自启动命令中应用自身收到的参数（即 ``python -m backupstudio`` 之后的 argv）。"""
    args = gui_command()[1:]
    if len(args) >= 2 and args[0] == "-m":
        args = args[2:]  # 源码形态：跳过 "-m backupstudio"
    return [*args, "--minimized"]


def _verify_autostart_command() -> str:
    """自检：生成的自启动命令必须能被 CLI 解析，否则登录后进程会立即退出。"""
    argv = _autostart_argv()
    try:
        from .app import _build_parser  # 延迟导入，避免 autostart <-> app 循环依赖

        _build_parser().parse_args(argv)
    except SystemExit as exc:
        return f"命令 {' '.join(argv)!r} 无法被解析（argparse 退出码 {exc.code}）"
    except Exception as exc:  # noqa: BLE001 - 自检本身绝不能让设置流程崩溃
        return f"自检执行失败：{type(exc).__name__}: {exc}"
    return ""


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """开启/关闭开机自启动，返回 (成功, 说明)。"""
    try:
        if sys.platform == "win32":
            ok, msg = _set_autostart_windows(enabled)
        elif sys.platform == "darwin":
            ok, msg = _set_autostart_macos(enabled)
        else:
            ok, msg = _set_autostart_linux(enabled)
    except Exception as exc:
        return False, f"设置失败: {type(exc).__name__}: {exc}"
    if ok and enabled:
        problem = _verify_autostart_command()
        if problem:
            return False, f"自启动命令自检未通过：{problem}"
    return ok, msg


def _set_autostart_windows(enabled: bool) -> tuple[bool, str]:
    import winreg  # noqa: PLC0415

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    exe, args, workdir = launch_spec()
    command = " ".join(_quote_windows(p) for p in [exe, *args, "--minimized"])
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _AUTOSTART_NAME, 0, winreg.REG_SZ, command)
            return True, "已添加开机自启动（当前用户注册表 Run 项）"
        try:
            winreg.DeleteValue(key, _AUTOSTART_NAME)
        except FileNotFoundError:
            pass
    return True, "已取消开机自启动"


def _mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"io.github.backupstudio.{APP_NAME.lower()}.plist"


def _set_autostart_macos(enabled: bool) -> tuple[bool, str]:
    import plistlib  # noqa: PLC0415

    path = _mac_plist_path()
    if not enabled:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return True, "已取消开机自启动"
    exe, args, workdir = launch_spec()
    log = config_dir() / "logs" / "autostart.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "Label": f"io.github.backupstudio.{APP_NAME.lower()}",
        "ProgramArguments": [exe, *args, "--minimized"],
        "RunAtLoad": True,
        "WorkingDirectory": workdir,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "LimitLoadToSessionType": "Aqua",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(data, fh)
    return True, f"已写入 LaunchAgent：{path}（下次登录生效）"


def _linux_desktop_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "autostart"
    return base / _LINUX_DESKTOP


def _systemd_unit_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "systemd" / "user" / "backupstudio-autostart.service"


def _desktop_quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _set_autostart_linux(enabled: bool) -> tuple[bool, str]:
    desktop = _linux_desktop_path()
    unit = _systemd_unit_path()
    if not enabled:
        removed = []
        for p in (desktop, unit):
            try:
                p.unlink()
                removed.append(p.name)
            except FileNotFoundError:
                pass
        return True, "已取消开机自启动" + (f"（移除 {', '.join(removed)}）" if removed else "")
    exe, args, workdir = launch_spec()
    exec_line = " ".join([_desktop_quote(exe), *[_desktop_quote(a) for a in args], "--minimized"])
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        f"Name={APP_NAME}\n"
        "Comment=Backup Studio helper (starts in tray)\n"
        f"Exec={exec_line}\n"
        f"Path={workdir}\n"
        "Icon=backupstudio\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    try:
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(content, encoding="utf-8")
        return True, f"已写入 {desktop}（下次登录生效）"
    except OSError as exc:
        # 某些桌面环境禁用 autostart 目录写入权限时，退化为 systemd 用户服务
        unit.parent.mkdir(parents=True, exist_ok=True)
        cmd = " ".join([exe, *args, "--minimized"])
        unit.write_text(
            "[Unit]\nDescription=BackupStudio tray helper\n\n"
            "[Service]\nType=simple\n"
            f"ExecStart={cmd}\nRestart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n",
            encoding="utf-8",
        )
        return True, f"写入 autostart 失败（{exc}），已改为 systemd 用户服务 {unit.name}；请执行 systemctl --user enable backupstudio-autostart"
