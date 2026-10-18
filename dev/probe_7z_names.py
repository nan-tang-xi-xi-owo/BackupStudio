"""验证 7-Zip 对 tar 容器内中文文件名的处理。"""

from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEVEN = ROOT / "tools" / "win32" / "7z" / "7za.exe"
WORK = ROOT / "build" / "probe-7z-names"
import shutil

shutil.rmtree(WORK, ignore_errors=True)
src = WORK / "src"
(src / "子").mkdir(parents=True)
(src / "子" / "深层.txt").write_text("数据\n" * 50, encoding="utf-8")
(src / "报告😀.txt").write_text("报告\n" * 50, encoding="utf-8")

for fmt_name, tf_fmt in (("PAX", tarfile.PAX_FORMAT), ("GNU", tarfile.GNU_FORMAT)):
    staging = WORK / f"staging-{fmt_name}.tar"
    with tarfile.open(staging, "w", format=tf_fmt) as tf:
        tf.add(src, arcname="src")
    out = WORK / f"out-{fmt_name}.7z"
    p = subprocess.run([str(SEVEN), "a", "-bd", "-y", "-t7z", str(out), str(staging)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    l = subprocess.run([str(SEVEN), "l", "-slt", str(out)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    paths = [ln.split("=", 1)[1].strip() for ln in l.stdout.splitlines() if ln.startswith("Path = ")]
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"[{fmt_name}] rc={p.returncode} entries={len(paths)}")
    for pth in paths[:8]:
        print("   ", repr(pth))
    # 直接对 tar 文件本身列目录（7z 能否识别 tar）
    l2 = subprocess.run([str(SEVEN), "l", "-slt", str(staging)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    paths2 = [ln.split("=", 1)[1].strip() for ln in l2.stdout.splitlines() if ln.startswith("Path = ")]
    print(f"   tar 直接列目录 rc={l2.returncode} entries={len(paths2)}")
    for pth in paths2[:8]:
        print("     ", repr(pth))
