"""验证打包后的 exe 真正创建了托盘图标。

检查方式：
1. 进程内是否存在 Qt 的托盘消息窗口（``TrayIconMessageWindowClass``）——只有
   ``QSystemTrayIcon.show()`` 被调用并成功注册才会出现；
2. 主窗口存在且可见；
3. 日志里没有「未找到随包图标资源」等警告。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "BackupStudio" / "BackupStudio.exe"
user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def windows_of(pid: int) -> list[tuple[str, str, bool]]:
    found: list[tuple[str, str, bool]] = []

    def cb(hwnd, lparam):  # noqa: ANN001
        wp = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wp))
        if wp.value != pid:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        n = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, title, n + 1)
        found.append((cls.value, title.value, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def main() -> int:
    if not EXE.exists():
        print("缺少 exe，请先打包")
        return 2
    cfg = ROOT / "build" / "tray-final"
    logdir = cfg / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    for f in logdir.glob("*.log"):
        f.unlink()
    env = {**os.environ, "BACKUPSTUDIO_CONFIG_DIR": str(cfg)}
    env.pop("QT_QPA_PLATFORM", None)
    proc = subprocess.Popen(
        [str(EXE)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, env=env,
    )
    try:
        deadline = time.time() + 12
        tray_cls = None
        main_win = None
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"✗ 进程已退出，退出码 {proc.returncode}")
                return 1
            wins = windows_of(proc.pid)
            tray_cls = [w for w in wins if "TrayIcon" in w[0]]
            main_win = [w for w in wins if w[2] and "备份工坊" in w[1]]
            if tray_cls and main_win:
                break
            time.sleep(0.5)
        print("托盘消息窗口:", tray_cls or "（未找到）")
        print("主窗口(可见):", main_win or "（未找到）")
        logs = logdir / "app.log"
        text = logs.read_text(encoding="utf-8", errors="replace") if logs.exists() else ""
        bad = [ln for ln in text.splitlines() if "ERROR" in ln or "未找到随包图标资源" in ln or "托盘不可用" in ln]
        print("日志异常行:", bad or "无")
        ok = bool(tray_cls) and bool(main_win) and not bad
        print("RESULT:", "PASS 托盘图标已创建" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        # 结束可能启动的守护进程
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process BackupStudio -ErrorAction SilentlyContinue | Stop-Process -Force"],
            capture_output=True,
        )


if __name__ == "__main__":
    sys.exit(main())
