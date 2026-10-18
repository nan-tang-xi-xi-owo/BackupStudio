"""对比「python 进程」与「dist 里的程序进程」对用户目录的写权限差异。"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TARGET = Path(os.environ["APPDATA"]) / "BackupStudio"

PROBE = r'''
import ctypes, os, sys
from pathlib import Path
target = Path(os.environ["APPDATA"]) / "BackupStudio"
probe = target / ".probe"
result = {}
try:
    probe.write_text("ok"); probe.unlink(); result["write"] = "OK"
except Exception as exc:
    result["write"] = f"FAIL {type(exc).__name__}: {exc}"
# 取当前进程令牌的完整性级别
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32
TOKEN_QUERY = 0x0008
handle = ctypes.c_void_p()
if advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(handle)):
    size = ctypes.c_ulong(0)
    advapi32.GetTokenInformation(handle, 18, None, 0, ctypes.byref(size))  # TokenIntegrityLevel=18
    buf = ctypes.create_string_buffer(size.value)
    if advapi32.GetTokenInformation(handle, 18, buf, size.value, ctypes.byref(size)):
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
        strp = ctypes.c_wchar_p()
        advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr), ctypes.byref(strp))
        result["integrity"] = strp.value
print(result)
'''


def run(cmd: list[str], label: str) -> None:
    env = {**os.environ}
    out = subprocess.run(cmd, capture_output=True, timeout=180, env=env)
    text = (out.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    print(f"[{label}] {text[-1] if text else out.returncode}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(dir=str(Path(__file__).resolve().parent.parent / "build")))
    script = tmp / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    print("目标目录:", TARGET)
    run([sys.executable, str(script)], "python 子进程")
    exe = Path(__file__).resolve().parent.parent / "dist" / "BackupStudio" / "BackupStudio.exe"
    if exe.exists():
        # 用 exe 的 python 解释器执行同样脚本不可行；改用 selftest 的结论
        out = subprocess.run([str(exe), "selftest"], capture_output=True, timeout=180)
        for line in (out.stdout or b"").decode("utf-8", "replace").splitlines():
            if "配置目录" in line or "诊断" in line or "不可写" in line:
                print("[exe] " + line.strip())
    # 用 PowerShell 直接写一个文件（交互式用户身份）
    ps = 'Set-Content -Path (Join-Path $env:APPDATA "BackupStudio\\.probe-ps") -Value ok -ErrorAction Stop; Remove-Item (Join-Path $env:APPDATA "BackupStudio\\.probe-ps")'
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=120)
    print("[powershell] 写入", "OK" if out.returncode == 0 else "FAIL " + (out.stderr or b"").decode("gbk", "replace")[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
