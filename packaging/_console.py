"""控制台编码安全化（打包脚本专用）。

背景（CI 红项 B1）：`packaging/build.py` 在 Windows runner 上以 cp1252 作为 stdout 编码时，
任何中文 `print` 都会抛 `UnicodeEncodeError: 'charmap' codec can't encode ...`（实测日志：
`File "packaging/build.py", line 105`）。运行时入口 `backupstudio/app.py` 早就用
`_make_streams_utf8_safe()` 做过同样的事，但**打包脚本被漏掉了** —— 这正是 Windows exe /
macOS dmg / AppImage 三个 job 全红的直接原因。

本模块把那段逻辑抽出来复用，并且：
* ``except Exception`` 兜底 —— 冻结/重定向/被替换成伪流（pytest capsys、IDE 控制台）时
  一律不抛，绝不让"为了打印日志"而拖垮打包；
* 顺带把 ``sys.stdout is None``（windowed 冻结程序）换成 devnull，避免后续 print 崩。
"""

from __future__ import annotations

import os
import sys


def make_console_utf8_safe() -> None:
    """让 stdout/stderr 能安全输出中文；任何失败都静默忽略。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 伪流/已关闭/不支持 reconfigure：都不该阻断打包
            pass
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
