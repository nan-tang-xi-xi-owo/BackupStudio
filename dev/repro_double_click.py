"""复现「双击打不开」：用与资源管理器双击完全相同的调用方式启动 dist 里的 exe。"""

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

kernel32 = ctypes.windll.kernel32
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.restype = wintypes.DWORD


def double_click_launch(exe: Path) -> tuple[int, float]:
    """ShellExecuteW（open）—— 与双击资源管理器图标等价。"""
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    started = time.time()
    ret = shell32.ShellExecuteW(None, "open", str(exe), None, str(exe.parent), 1)
    # 等它把主窗口/托盘创建出来
    time.sleep(6)
    code = shell32.ShellExecuteW.__self__ if False else int(ret or 0)
    return code, time.time() - started


def wait_exit(pid: int, timeout: float) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        kernel32.CloseHandle(h)
        if code.value != 259:  # STILL_ACTIVE
            return int(code.value)
        time.sleep(0.2)
    return -1


def main() -> int:
    if not EXE.exists():
        print(f"缺少 {EXE}")
        return 2
    print("exe:", EXE, EXE.stat().st_size, "bytes")

    # A. 先直接 CreateProcess 看退出码（可观测）
    proc = subprocess.Popen([str(EXE)], stdin=subprocess.DEVNULL, close_fds=True)
    time.sleep(8)
    alive = proc.poll() is None
    print(f"[A] CreateProcess 启动 8 秒后仍在运行: {alive}")
    if not alive:
        print(f"[A] 立即退出，退出码 = {proc.returncode}（0xC0000005 等表示崩溃）")
    else:
        proc.kill()

    # B. ShellExecuteW（等同双击）
    code, elapsed = double_click_launch(EXE)
    print(f"[B] ShellExecuteW 返回 {code}（>32 成功），用时 {elapsed:.1f}s")

    # C. 枚举是否有 BackupStudio 进程 + 可见窗口
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process BackupStudio -ErrorAction SilentlyContinue | "
         "Select-Object Id,MainWindowTitle,StartTime | ConvertTo-Json -Compress"],
        capture_output=True, text=True,
    ).stdout.strip()
    print("[C] 当前 BackupStudio 进程:", out or "（无）")

    # D. 检查配置目录是否提示「已有实例」
    cfg = Path(os.environ["APPDATA"]) / "BackupStudio"
    lock = cfg / "run" / "gui.lock"
    print("[D] gui.lock 存在:", lock.exists(), "内容:", lock.read_text(encoding="ascii", errors="replace").strip() if lock.exists() else "-")
    print("[D] run 目录:", [p.name for p in (cfg / "run").iterdir()] if (cfg / "run").is_dir() else "不存在")
    return 0


if __name__ == "__main__":
    sys.exit(main())
