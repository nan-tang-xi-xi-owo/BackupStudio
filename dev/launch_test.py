"""启动自检：用**独立子进程**真实启动 dist 里的 GUI，确认窗口/托盘确实存活。

与 dev/repro_double_click.py 不同，这里用文件重定向（不用管道），不会因为
windowed 程序继承管道句柄而卡住，最后用 psutil 判断进程与窗口状态。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
EXE = ROOT / "dist" / "BackupStudio" / "BackupStudio.exe"


def _psutil():
    import psutil  # noqa: PLC0415

    return psutil


def check(pid: int) -> tuple[bool, str]:
    ps = _psutil()
    try:
        proc = ps.Process(pid)
    except ps.NoSuchProcess:
        return False, "进程已退出"
    try:
        threads = proc.num_threads()
        rss = proc.memory_info().rss / (1024 * 1024)
    except ps.Error:
        return False, "无法读取进程信息"
    windows = []
    try:
        for w in proc.children(recursive=False):
            pass
    except ps.Error:
        pass
    return True, f"线程数={threads} 内存={rss:.0f}MB 窗口={windows or '（主窗口由 Qt 管理）'}"


def main() -> int:
    if not EXE.exists():
        print(f"缺少 {EXE}，请先打包：python packaging/build.py")
        return 2
    logdir = ROOT / "build" / "launch-test"
    logdir.mkdir(parents=True, exist_ok=True)
    out = logdir / "stdout.txt"
    err = logdir / "stderr.txt"
    env = {**os.environ}
    env.pop("QT_QPA_PLATFORM", None)  # 用真实平台插件
    env["BACKUPSTUDIO_CONFIG_DIR"] = str(logdir / "config")
    started = time.time()
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = subprocess.Popen(
            [str(EXE)],
            stdin=subprocess.DEVNULL,
            stdout=fo,
            stderr=fe,
            close_fds=True,
            env=env,
            cwd=str(ROOT),
        )
        deadline = time.time() + 25
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        elapsed = time.time() - started
        if proc.poll() is not None:
            print(f"✗ 程序在 {elapsed:.1f}s 内退出（退出码 {proc.returncode}），异常！")
            print_evidence(out, err, logdir)
            return 1
        ok, detail = check(proc.pid)
        print(f"✓ 程序运行中（{elapsed:.1f}s）pid={proc.pid} {detail}")
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print_evidence(out, err, logdir, brief=True)
    return 0 if ok else 1


def print_evidence(out: Path, err: Path, logdir: Path, brief: bool = False) -> None:
    for name, path in (("stdout", out), ("stderr", err)):
        text = path.read_text(encoding="utf-8", errors="replace").strip() if path.exists() else ""
        if text:
            print(f"--- {name} ---")
            print("\n".join(text.splitlines()[-25:]) if not brief else text.splitlines()[-3:])
    startup = logdir / "config" / "logs" / "startup.log"
    if startup.exists():
        print("--- startup.log ---")
        print("\n".join(startup.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]))


if __name__ == "__main__":
    sys.exit(main())
