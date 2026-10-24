"""自启动命令的 CLI 契约校验（R-fix4）。

历史缺陷
--------
Windows 安装包（``packaging/installer.iss.in``）曾把自启动参数写成
``"--tray --minimized"``。但 ``tray`` 是 argparse 的**子命令**，不是选项，
``--minimized`` 是 ``tray``/``start`` 子命令自己的参数。于是
``BackupStudio.exe --tray --minimized`` 会被判为
``error: unrecognized arguments`` → 进程 rc=2 立即退出，
用户勾了「开机自启」后定时备份一次都不执行，且没有任何提示。

本脚本把「哪些写法合法」固化成可执行契约，供 CI 与 pytest 共同复用：

* ``start --minimized``  —— 合法（autostart.py 各写入点使用的写法）
* ``tray --minimized``   —— 合法（旧注册表项兼容，tray 本来就最小化）
* ``tray``               —— 合法
* ``--tray --minimized`` —— **非法**（正是那个 bug）

用法::

    python packaging/check_autostart_args.py        # 退出码 0 = 全部符合预期
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 合法写法 → 期望解析出的 (command, minimized)
VALID: dict[tuple[str, ...], tuple[str, bool]] = {
    ("start", "--minimized"): ("start", True),
    ("start",): ("start", False),
    ("tray", "--minimized"): ("tray", True),
    ("tray",): ("tray", False),
}

# 非法写法：必须被 argparse 拒绝
INVALID: tuple[tuple[str, ...], ...] = (
    ("--tray", "--minimized"),
    ("--tray",),
    ("--minimized",),
)


def main() -> int:
    from backupstudio.app import _build_parser

    parser = _build_parser()
    failures: list[str] = []

    for argv, (want_cmd, want_min) in VALID.items():
        try:
            ns = parser.parse_args(list(argv))
        except SystemExit:
            failures.append(f"合法写法被拒绝：{list(argv)}")
            continue
        got_cmd = getattr(ns, "command", None)
        got_min = bool(getattr(ns, "minimized", False))
        if got_cmd != want_cmd or got_min != want_min:
            failures.append(
                f"{list(argv)} 解析结果不符：command={got_cmd!r} minimized={got_min}，"
                f"期望 command={want_cmd!r} minimized={want_min}"
            )

    for argv in INVALID:
        try:
            parser.parse_args(list(argv))
        except SystemExit:
            continue
        failures.append(f"非法写法竟然被接受：{list(argv)}（旧的 --tray bug 会复活）")

    # 安装包模板与 autostart 写入点必须使用同一份合法写法
    iss = ROOT / "packaging" / "installer.iss.in"
    try:
        text = iss.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(f"读不到 {iss}：{exc}")
    else:
        lines = [ln for ln in text.splitlines() if "Parameters:" in ln]
        if not lines:
            failures.append("installer.iss.in 里没有找到 Parameters: 行（自启动项丢了？）")
        for ln in lines:
            if '"start --minimized"' not in ln:
                failures.append(f"installer.iss.in 自启动参数不是 \"start --minimized\"：{ln.strip()}")
            if '"--tray' in ln:
                failures.append(f"installer.iss.in 仍在使用旧的 --tray 写法：{ln.strip()}")

    if failures:
        for f in failures:
            print(f"✗ {f}", file=sys.stderr)
        return 1
    print(f"✓ 自启动 CLI 契约 OK（{len(VALID)} 个合法写法 / {len(INVALID)} 个非法写法全部符合预期）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
