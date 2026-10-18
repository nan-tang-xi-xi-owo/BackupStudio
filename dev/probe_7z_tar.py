"""检查 7-Zip 能否正确识别包含中文/emoji 的暂存 tar 文件（Windows）。"""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEVEN = ROOT / "tools" / "win32" / "7z" / "7za.exe"
WORK = ROOT / "build" / "probe-7z-tar"

shutil_rm = __import__("shutil").rmtree
shutil_rm(WORK, ignore_errors=True)
src = WORK / "src"
(src / "子目录").mkdir(parents=True)
(src / "子目录" / "深层.txt").write_text("深层数据\n" * 50, encoding="utf-8")
(src / "报告 2026😀.txt").write_text("报告\n" * 50, encoding="utf-8")
(src / ".hidden").write_text("h", encoding="utf-8")

staging = WORK / "staging.tar"
with tarfile.open(staging, "w") as tf:
    tf.add(src, arcname="src")
print("staging tar:", staging, staging.stat().st_size)

for fmt in ("7z", "zip"):
    out = WORK / f"out.{fmt}"
    p = subprocess.run([str(SEVEN), "a", "-bd", "-y", f"-t{fmt}", str(out), str(staging)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(f"\n== -t{fmt} => rc={p.returncode}")
    print(p.stdout[-800:])
    if out.exists():
        l = subprocess.run([str(SEVEN), "l", "-ba", str(out)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        print("listing:", [ln for ln in l.stdout.splitlines() if ln.strip()][:4])
