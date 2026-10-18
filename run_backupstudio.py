"""GUI 启动脚本（PyInstaller 入口）。"""

from __future__ import annotations

import multiprocessing
import sys

from backupstudio.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main(sys.argv[1:]))
