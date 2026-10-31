import py_compile
import sys

for f in ("backupstudio/gui/main_window.py", "backupstudio/gui/titlebar.py"):
    try:
        py_compile.compile(f, doraise=True)
        print(f"  OK  {f}")
    except Exception as e:
        print(f"  ERR {f}: {e}")
        sys.exit(1)

from pathlib import Path

t = Path("backupstudio/gui/main_window.py").read_text(encoding="utf-8")
print("  RESIZE_MARGIN 已导入:", "RESIZE_MARGIN" in t.split("def _edge_at")[0])
for i, l in enumerate(t.splitlines(), 1):
    if "RESIZE_MARGIN" in l:
        print(f"    L{i}: {l.strip()[:70]}")
