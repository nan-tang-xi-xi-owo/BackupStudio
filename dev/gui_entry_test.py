"""从源码验证 GUI 入口：启动、计时退出、检查日志无异常。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(ROOT / "build" / "gui-run3")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import backupstudio.app as appmod  # noqa: E402


def main() -> int:
    real_exec = QApplication.exec

    def fake_exec():
        QTimer.singleShot(1500, lambda: QApplication.instance().quit())
        return real_exec()

    QApplication.exec = staticmethod(fake_exec)  # type: ignore[assignment]
    code = appmod.run_gui()
    print("run_gui returned", code)
    log = Path(os.environ["BACKUPSTUDIO_CONFIG_DIR"]) / "logs" / "app.log"
    if log.exists():
        text = log.read_text(encoding="utf-8", errors="replace")
        bad = [ln for ln in text.splitlines() if "ERROR" in ln or "Traceback" in ln]
        print("log errors:", len(bad))
        for ln in bad[-8:]:
            print("  ", ln)
        return 1 if bad else 0
    print("no log file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
