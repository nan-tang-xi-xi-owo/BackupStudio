"""程序入口：GUI（默认）、托盘模式、CLI、守护进程。

启动健壮性设计（双击场景）：
1. windowed 版 exe 没有控制台，任何未捕获异常都会导致「没有任何反应」；
   因此 ``main()`` 全程包在防护里，出事一定写日志、并尽量弹窗告知用户；
2. 启动早期使用 ``emergency.log``（按可执行文件旁、临时目录、APPDATA 依次尝试），
   保证日志目录坏掉时依然留得下证据；
3. 配置/运行目录不可写时自动降级换位置（见 ``paths.py``），并在界面顶部提示。
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path

from . import APP_NAME, APP_NAME_ZH, APP_ID, __version__
from .paths import STARTUP_DIAGNOSTICS, config_dir, log_dir

log = logging.getLogger("backupstudio.app")

_EMERGENCY_CANDIDATES: list[Path] = []


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

def _emergency_dirs() -> list[Path]:
    dirs: list[Path] = []
    try:
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir.is_dir():
            dirs.append(exe_dir / "logs")
    except (OSError, ValueError):
        pass
    try:
        dirs.append(log_dir())
    except Exception:  # noqa: BLE001
        pass
    try:
        import tempfile

        dirs.append(Path(tempfile.gettempdir()) / f"{APP_NAME}-logs")
    except Exception:  # noqa: BLE001
        pass
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / APP_NAME / "logs")
    return dirs


def write_emergency(text: str, filename: str = "emergency.log") -> Path | None:
    """把关键信息写到第一个可写位置，返回实际路径。"""
    for d in _emergency_dirs():
        try:
            d.mkdir(parents=True, exist_ok=True)
            target = d / filename
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(text.rstrip() + "\n")
            if target not in _EMERGENCY_CANDIDATES:
                _EMERGENCY_CANDIDATES.append(target)
            return target
        except OSError:
            continue
    return None


def _setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # 1) 常规日志文件
    try:
        d = log_dir()
        handler = logging.handlers.RotatingFileHandler(
            d / "app.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except Exception as exc:  # noqa: BLE001
        write_emergency(f"无法创建日志文件：{type(exc).__name__}: {exc}")
    # 2) 有控制台时才输出到控制台（守护进程子进程不输出）
    if sys.stdout is not None and sys.stdout.isatty() and not os.environ.get("BACKUPSTUDIO_DAEMON"):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


# --------------------------------------------------------------------------
# 致命错误展示（windowed 版没有控制台，必须弹窗 + 写文件）
# --------------------------------------------------------------------------

def _show_fatal(title: str, message: str, details: str = "") -> None:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv[:1])
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(f"{APP_NAME_ZH} · {title}")
        box.setText(title)
        box.setInformativeText(message)
        if details:
            box.setDetailedText(details)
        box.exec()
        del app
    except Exception:
        pass


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def run_gui(start_minimized: bool = False, open_new: bool = False) -> int:
    """启动 Qt 界面（含托盘）；任何异常都记日志并弹窗，绝不静默退出。"""
    gui_log = write_emergency(
        f"--- {__version__} GUI 启动 {os.getpid()} 参数={_mask_argv(sys.argv[1:])} frozen={getattr(sys, 'frozen', False)}",
        filename="startup.log",
    )
    try:
        return _run_gui_inner(start_minimized=start_minimized, open_new=open_new)
    except BaseException as exc:  # noqa: BLE001 - 包括异常 Exit
        tb = traceback.format_exc()
        log.exception("GUI 启动失败")
        where = write_emergency(f"GUI 启动失败 {type(exc).__name__}: {exc}\n{tb}")
        if STARTUP_DIAGNOSTICS:
            write_emergency("启动诊断：\n" + "\n".join("  - " + d for d in STARTUP_DIAGNOSTICS))
        _show_fatal(
            "程序启动失败",
            f"{type(exc).__name__}: {exc}\n\n详细信息已写入日志：\n{where or gui_log or '（无法写日志）'}",
            tb[-3000:],
        )
        return 1


def _run_gui_inner(start_minimized: bool = False, open_new: bool = False) -> int:
    from PySide6.QtCore import QCoreApplication, Qt, QTimer
    from PySide6.QtWidgets import QApplication

    # ---- 数据目录：失败也不抛异常
    from .autostart import app_instance_pid
    from .gui import app_icon, stylesheet
    from .gui.appstate import AppState
    from .gui.main_window import MainWindow
    from .paths import runtime_dir, safe_file_write
    from .store import TaskStore

    log.info("GUI 启动：配置目录=%s 运行目录=%s", config_dir(), runtime_dir())

    lock_path = runtime_dir() / "gui.lock"
    other = None
    try:
        other = app_instance_pid(lock_path)
    except Exception:  # noqa: BLE001
        log.debug("单实例检测失败", exc_info=True)
    if other and other != os.getpid():
        log.warning("已有实例在运行（PID %s），改为唤醒它", other)
        _wake_existing()
        return 0
    safe_file_write(lock_path, str(os.getpid()), encoding="ascii")

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME_ZH)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("BackupStudio")
    app.setDesktopFileName("io.github.backupstudio.BackupStudio")
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    # ---- 配置：损坏/不可写都不应阻止启动
    store = TaskStore(config_dir() / "tasks.json")
    state = AppState(store)
    window = MainWindow(state)
    app.setStyleSheet(stylesheet(state.palette))
    log.info("主窗口已创建，托盘可用=%s", getattr(window, "tray_available", True))

    show_window = not (start_minimized or store.settings.start_minimized) or open_new
    if show_window:
        window.show()
        window.raise_()
        window.activateWindow()
    else:
        window.hide()

    state.start_scheduler()
    if store.settings.auto_run_backup_daemon:
        from .daemon import ensure_daemon

        def _ensure() -> None:
            try:
                ensure_daemon()
            except Exception:  # noqa: BLE001
                log.exception("守护进程启动失败")

        QTimer.singleShot(1200, _ensure)

    # 每隔一段时间心跳写一次，便于确认「程序确实在运行」
    def _heartbeat() -> None:
        write_emergency(
            f"{time_str()} 心跳 pid={os.getpid()} 窗口可见={window.isVisible()} 托盘={getattr(window, 'tray_available', True)}",
            filename="startup.log",
        )

    heartbeat = QTimer()
    heartbeat.setInterval(60_000)
    heartbeat.timeout.connect(_heartbeat)
    heartbeat.start()
    _heartbeat()

    def _cleanup() -> None:
        state.stop_scheduler()
        try:
            if lock_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                lock_path.unlink()
        except OSError:
            pass

    app.aboutToQuit.connect(_cleanup)
    return app.exec()


def time_str() -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S")


def _wake_existing() -> None:
    """尝试把已运行实例的窗口调到前台。"""
    try:
        import getpass

        from PySide6.QtNetwork import QLocalSocket

        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001 - 无控制台等环境下可能失败
            user = "user"
        sock = QLocalSocket()
        sock.connectToServer(f"backupstudio-{user}")
        if sock.waitForConnected(300):
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(300)
            sock.disconnectFromServer()
        else:
            # 注意：这里仍处于「GUI 应用对象创建之前」（见 main() 调用处），
            # 一旦使用 Qt 弹窗组件会触发 Qt qFatal 硬崩溃
            # （Windows 退出码 0xC0000409，Python 层不可捕获）。
            # 因此提示必须走系统原生通道。
            msg = "程序已在运行，请在系统托盘找到它。"
            if sys.platform == "win32":
                import ctypes

                ctypes.windll.user32.MessageBoxW(0, msg, APP_NAME_ZH, 0x40)
            else:
                write_emergency(f"{APP_NAME_ZH}：{msg}")
                if sys.stderr is not None:
                    print(f"{APP_NAME_ZH}：{msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backupstudio",
        description=f"{APP_NAME_ZH} {APP_NAME} {__version__} — 跨平台文件夹备份与多格式压缩",
    )
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("gui", help="打开图形界面")
    _sp = sub.add_parser("start", help="在后台启动图形界面（用于开机自启）")
    _sp.add_argument("--minimized", action="store_true", help="启动后不显示主窗口，只驻留托盘")
    _tp = sub.add_parser("tray", help="以托盘模式启动（不显示主窗口）")
    # tray 本来就是最小化启动；--minimized 仅用于兼容旧版写入注册表/plist/desktop 的自启命令
    _tp.add_argument("--minimized", action="store_true", help="兼容旧版自启动项（无操作：tray 本来就最小化启动）")

    task = sub.add_parser("task", help="管理备份任务（list / add / rm / enable）")
    tsub = task.add_subparsers(dest="action")
    tsub.add_parser("list", help="列出任务")
    sh = tsub.add_parser("show", help="查看任务详情")
    sh.add_argument("task_id")
    add = tsub.add_parser("add", help="新建任务")
    add.add_argument("--name", required=True)
    add.add_argument("--source", required=True, help="要备份的文件夹")
    add.add_argument("--dest", required=True, help="备份存放目录")
    add.add_argument("--format", default="7z", help="压缩格式：zip/7z/rar/tar/tar.gz/tar.bz2/tar.xz/tar.zst")
    add.add_argument("--password", default="", help="压缩密码")
    add.add_argument("--encrypt", default="aes256", choices=["aes256", "zipcrypto", "gpg"])
    add.add_argument("--level", type=int, default=5, help="压缩级别 0-9")
    add.add_argument("--schedule", default="manual", choices=["manual", "interval", "daily", "weekly", "monthly"])
    add.add_argument("--time", default="02:00", help="daily/weekly/monthly 的执行时间 HH:MM")
    add.add_argument("--days", default="0,1,2,3,4", help="weekly：0=周一")
    add.add_argument("--day", type=int, default=1, help="monthly：几号")
    add.add_argument("--minutes", type=int, default=60, help="interval：间隔分钟")
    add.add_argument("--volume-size", type=int, default=0, help="分卷大小（MB），0=不分卷")
    add.add_argument("--volume-mode", default="7z-split", choices=["7z-split", "rar-native", "split"])
    add.add_argument("--exclude", default="", help="排除规则，逗号分隔，如 '*.tmp,node_modules'")
    add.add_argument("--force", action="store_true", help="强制压缩")
    rm = tsub.add_parser("rm", help="删除任务")
    rm.add_argument("task_id")
    en = tsub.add_parser("enable", help="启用/停用任务")
    en.add_argument("task_id")
    en.add_argument("state", choices=["on", "off"])

    run = sub.add_parser("run", help="立即执行任务（task_id 或 --all）")
    run.add_argument("task_id", nargs="?", default="")
    run.add_argument("--all", action="store_true")

    info = sub.add_parser("info", help="显示系统环境与压缩支持（会检测 Linux 发行版与缺失软件）")
    info.add_argument("--json", action="store_true")

    sub.add_parser("doctor", help="诊断缺失的压缩软件并给出安装命令")
    sub.add_parser("selftest", help="自检：目录、日志、压缩引擎与界面能否正常启动")

    extract = sub.add_parser("extract", help="解压备份（需安装对应工具）")
    extract.add_argument("archive")
    extract.add_argument("--to", required=True)
    extract.add_argument("--password", default="")
    # 炸弹防护逃生阀（审查 A4）：默认 100 GiB 绝对上限，灾难恢复时可显式放宽
    extract.add_argument(
        "--max-unpacked-bytes",
        type=int,
        default=None,
        help="解压总量绝对上限（字节）；0 = 不限制，默认 100 GiB / 环境变量 BACKUPSTUDIO_MAX_UNPACKED_BYTES",
    )
    extract.add_argument(
        "--max-unpack-ratio",
        type=int,
        default=None,
        help="解压总量 / 包大小 的比例上限；0 = 不限制，默认 10000 / 环境变量 BACKUPSTUDIO_MAX_UNPACK_RATIO",
    )

    daemon = sub.add_parser("daemon", help="后台守护进程")
    daemon.add_argument("--port", type=int, default=None)
    daemon.add_argument("--status", action="store_true")
    daemon.add_argument("--stop", action="store_true")
    daemon.add_argument("--foreground", action="store_true")
    return p


def _cli(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd = args.command or ("gui" if not argv else None)
    store = None
    if cmd in ("task", "run"):
        from .store import TaskStore

        store = TaskStore(config_dir() / "tasks.json")

    if cmd in (None, "gui"):
        return run_gui()
    if cmd == "tray":
        return run_gui(start_minimized=True)
    if cmd == "start":
        return run_gui(start_minimized=getattr(args, "minimized", False))
    if cmd == "info":
        return _cmd_info(args)
    if cmd == "doctor":
        return _cmd_doctor()
    if cmd == "selftest":
        return _cmd_selftest()
    if cmd == "daemon":
        return _cmd_daemon(args)
    if cmd == "task":
        return _cmd_task(store, args)
    if cmd == "run":
        return _cmd_run(store, args)
    if cmd == "extract":
        return _cmd_extract(args)
    parser.print_help()
    return 1


def _display_width(text: str) -> int:
    """估算终端显示宽度（中日韩字符按 2 列计）。"""
    import unicodedata

    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _cmd_info(args) -> int:  # noqa: ANN001
    import json

    from .capabilities import format_capabilities, missing_requirements
    from .platform_info import describe_environment, get_platform_info

    info = get_platform_info()
    caps = format_capabilities()
    if args.json:
        payload = {
            "platform": {
                "system": info.system,
                "release": info.release,
                "machine": info.machine,
                "label": info.label,
                "distro_id": info.distro_id,
                "distro_like": list(info.distro_like),
                "package_manager": info.package_manager,
                "desktop": info.desktop,
                "python": info.python,
                "frozen": info.is_frozen,
            },
            "formats": caps,
            "missing": [{"format": f, "install": h, "why": d} for f, h, d in missing_requirements(caps)],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"{APP_NAME_ZH} {APP_NAME} {__version__}")
    for k, v in describe_environment():
        pad = 10 - _display_width(k)
        print(f"  {k}{' ' * pad}: {v}")
    print()
    from .capabilities import capability_report_text

    print(capability_report_text(caps))
    return 0


def _cmd_doctor() -> int:
    from .capabilities import format_capabilities, missing_requirements

    caps = format_capabilities()
    missing = missing_requirements(caps)
    if not missing:
        print("✓ 所有压缩格式所需的组件都已就绪。")
        return 0
    print("缺少以下组件：\n")
    seen = set()
    for fmt, hint, desc in missing:  # noqa: B007 - fmt 用于上下文
        if hint in seen:
            continue
        seen.add(hint)
        print(f"• {hint}")
        if desc:
            print(f"    {desc}")
    print("\n提示：也可以勾选「强制压缩」，用内置引擎生成等效容器。")
    return 0


def _cmd_selftest() -> int:
    """自检：确认目录、日志、压缩引擎与界面都能正常工作。"""
    from .paths import STARTUP_DIAGNOSTICS, config_dir, log_dir, runtime_dir

    problems: list[str] = []
    print(f"{APP_NAME_ZH} {APP_NAME} {__version__} 自检\n")

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
        if not ok:
            problems.append(label)

    cfg = config_dir()
    check("配置目录可写", cfg.is_dir() and os.access(cfg, os.W_OK), str(cfg))
    rdir = runtime_dir()
    check("运行目录可写", rdir.is_dir(), str(rdir))
    ldir = log_dir()
    check("日志目录可写", ldir.is_dir(), str(ldir))
    marker = ldir / "selftest.txt"
    try:
        marker.write_text("ok", encoding="utf-8")
        marker.unlink()
        check("日志目录写入测试", True)
    except OSError as exc:
        check("日志目录写入测试", False, str(exc))

    from .capabilities import format_capabilities

    caps = format_capabilities()
    usable = [f for f, e in caps.items() if e.get("available")]
    check("至少一种压缩格式可用", bool(usable), ", ".join(usable))

    try:
        from PySide6.QtCore import QCoreApplication  # noqa: F401
        from PySide6.QtWidgets import QApplication  # noqa: F401

        check("PySide6 可用", True)
    except ImportError as exc:
        check("PySide6 可用", False, str(exc))

    check("紧急日志可写", bool(write_emergency("selftest", filename="selftest.log")))

    if STARTUP_DIAGNOSTICS:
        print("\n启动期间的诊断信息：")
        for line in STARTUP_DIAGNOSTICS:
            print("  -", line)

    print("\n结果：" + ("全部通过 ✓" if not problems else f"{len(problems)} 项异常 ✗ → {', '.join(problems)}"))
    return 0 if not problems else 1


def _cmd_daemon(args) -> int:  # noqa: ANN001
    from .daemon import IPC_PORT, daemon_status, ensure_daemon, main as daemon_main, stop_daemon_safe

    if args.status:
        st = daemon_status()
        if st.get("running"):
            print(f"守护进程运行中（PID {st.get('pid')}）")
            for t in st.get("tasks", []):
                print(f"  • {t.get('name')}: {t.get('status') or '等待'}，下次 {t.get('next')}")
        else:
            print("守护进程未运行")
        return 0 if st.get("running") else 1
    if args.stop:
        ok, msg = stop_daemon_safe()
        print(msg)
        return 0 if ok else 1
    if args.foreground:
        port = args.port or IPC_PORT
        return daemon_main(["--port", str(port)] + (["--verbose"] if args.verbose else []))
    ok, msg = ensure_daemon()
    print(msg)
    return 0 if ok else 1


def _cmd_task(store, args) -> int:  # noqa: ANN001
    from .config import ArchiveOptions, BackupTask, ScheduleConfig, SourceConfig
    from .schedule import describe_next

    if args.action in (None, "list"):
        tasks = store.list_tasks()
        if not tasks:
            print("还没有任务。用 `backupstudio task add --name ... --source ... --dest ...` 新建。")
            return 0
        print(f"{'ID':<14} {'名称':<16} {'格式':<10} {'计划':<20} {'上次':<6} 位置")
        print("-" * 96)
        for t in tasks:
            status = {"success": "成功", "failure": "失败", "skipped": "跳过", "running": "运行中", "": "-"}.get(t.last_status, t.last_status)
            flag = "" if t.enabled else "（已停用）"
            print(f"{t.id:<14} {t.name[:16]:<16} {t.options.fmt:<10} {t.schedule.describe():<20} {status:<6} {t.destination}{flag}")
        return 0
    if args.action == "show":
        t = store.get(args.task_id) or next((x for x in store.list_tasks() if x.name == args.task_id), None)
        if t is None:
            print("任务不存在", file=sys.stderr)
            return 1
        import json

        data = t.to_dict()
        if data.get("options", {}).get("password"):
            # 密码不回显（审查 P0-14）；需要密码请用「导出任务（含密码）」
            data["options"]["password"] = "***（已隐藏；如需包含密码请用导出功能）"
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if args.action == "add":
        days = [int(x) for x in args.days.split(",") if x.strip().isdigit()]
        task = BackupTask(
            name=args.name,
            sources=[SourceConfig(path=args.source)],
            destination=args.dest,
            options=ArchiveOptions(
                fmt=args.format,
                level=args.level,
                password=args.password,
                encrypt_mode=args.encrypt,
                multivolume=args.volume_mode if args.volume_size else "",
                volume_value=args.volume_size or 100,
                volume_unit="MB",
                exclude_globs=[g for g in args.exclude.split(",") if g.strip()],
                force=args.force,
            ),
            schedule=ScheduleConfig(type=args.schedule, time=args.time, days=days, day=args.day, minutes=args.minutes),
        )
        if not Path(task.destination).exists():
            try:
                Path(task.destination).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"无法创建备份目录：{exc}", file=sys.stderr)
                return 2
        store.upsert(task)
        store.save()
        print(f"已创建任务 {task.id}：{task.name}（{task.schedule.describe()}）")
        return 0
    if args.action == "rm":
        if store.delete(args.task_id):
            store.save()
            print(f"已删除任务 {args.task_id}")
            return 0
        print("任务不存在", file=sys.stderr)
        return 1
    if args.action == "enable":
        t = store.get(args.task_id)
        if t is None:
            print("任务不存在", file=sys.stderr)
            return 1
        t.enabled = args.state == "on"
        store.upsert(t)
        store.save()
        print(f"{'已启用' if t.enabled else '已停用'}：{t.name}（下次 {describe_next(t)}）")
        return 0
    return 1


def _cmd_run(store, args) -> int:  # noqa: ANN001
    from .engine import TaskEngine
    from .util import human_size

    engine = TaskEngine(log_fn=lambda m: print(m, file=sys.stderr))
    targets = []
    if args.all:
        targets = [t for t in store.list_tasks() if t.enabled]
    elif args.task_id:
        t = store.get(args.task_id)
        if t is None:
            matches = [x for x in store.list_tasks() if x.name == args.task_id]
            t = matches[0] if matches else None
        if t is None:
            print("任务不存在", file=sys.stderr)
            return 1
        targets = [t]
    if not targets:
        print("没有可执行的任务", file=sys.stderr)
        return 1
    failures = 0
    for t in targets:

        def _prog(p: dict, _name: str = t.name) -> None:
            if sys.stdout is not None and sys.stdout.isatty() and not os.environ.get("BACKUPSTUDIO_DAEMON"):
                total = max(1, p.get("total_bytes") or 0)
                pct = int((p.get("done_bytes") or 0) * 100 / total)
                sys.stderr.write(f"\r[{_name}] {p.get('phase')} {pct}%   ")
                sys.stderr.flush()

        result = engine.run(t, trigger="manual", progress_cb=_prog)
        rec = result.record
        fresh = store.get(t.id)
        if fresh is not None:
            fresh.last_run = rec.started_at
            fresh.last_status = rec.status
            fresh.last_message = rec.errors[0] if rec.errors else result.message
            fresh.last_archive = rec.archive
            fresh.last_duration = rec.duration
            store.upsert(fresh)
        store.save()
        print()
        if rec.status == "success":
            print(f"✓ {t.name}：{rec.archive}（{human_size(rec.archive_bytes)}，{rec.files} 个文件，{rec.duration:.1f}s）")
            for w in rec.warnings:
                print(f"  ⚠ {w}")
            if rec.verification == "passed":
                print(f"  校验通过：{rec.verification_message}")
        else:
            failures += 1
            print(f"✗ {t.name}：{rec.status}")
            for e in rec.errors:
                print(f"  {e}")
    return 0 if failures == 0 else 1


def _cmd_extract(args) -> int:  # noqa: ANN001
    from . import extract as ex

    # 命令行阈值覆盖环境变量（审查 A4：此前无任何逃生阀，灾难恢复会被自己的防护挡住）
    if getattr(args, "max_unpacked_bytes", None) is not None:
        ex._MAX_TOTAL_BYTES = max(0, int(args.max_unpacked_bytes))
        os.environ[ex.ENV_MAX_UNPACKED_BYTES] = str(ex._MAX_TOTAL_BYTES)
    if getattr(args, "max_unpack_ratio", None) is not None:
        ex._MAX_EXPANSION_RATIO = max(0, int(args.max_unpack_ratio))
        os.environ[ex.ENV_MAX_UNPACK_RATIO] = str(ex._MAX_EXPANSION_RATIO)

    ok, msg = ex.extract_archive(Path(args.archive), Path(args.to), args.password or None)
    print(msg)
    return 0 if ok else 1


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _make_streams_utf8_safe() -> None:
    """保证向 stdout/stderr 写中文不会因编码/为 None 而抛异常。

    冻结的 windowed 程序里 sys.stdout 可能为 None；重定向到管道时编码可能是 GBK。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    if sys.stdout is None:
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            pass
    if sys.stderr is None:
        try:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            pass


def _mask_argv(argv: list[str]) -> list[str]:
    """日志脱敏：--password / --encrypt 等敏感参数值一律替换（审查 P0-14）。"""
    out: list[str] = []
    hide_next = False
    for a in argv:
        if hide_next:
            out.append("***")
            hide_next = False
            continue
        low = a.lower()
        if low in ("--password", "--pass", "-p"):
            out.append(a)
            hide_next = True
            continue
        if low.startswith("--password=") or low.startswith("--pass="):
            out.append(a.split("=", 1)[0] + "=***")
            continue
        out.append(a)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _make_streams_utf8_safe()
    # 守护进程子进程启动参数（--daemon）在前端先处理
    if "--daemon" in argv:
        from .daemon import main as daemon_main

        rest = [a for a in argv if a != "--daemon"]
        return daemon_main(rest)
    try:
        _setup_logging("--verbose" in argv or "-v" in argv)
        log.info("启动 %s %s（frozen=%s）参数=%s", APP_NAME, __version__, getattr(sys, "frozen", False), _mask_argv(argv))
        try:
            return _cli(argv)
        except KeyboardInterrupt:
            return 130
        except SystemExit as exc:
            # --help / --version 等由 argparse 触发，属正常退出
            return int(exc.code or 0)
        except BaseException as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error("命令行执行失败：%s\n%s", exc, tb)
            if sys.stderr is not None:
                print(tb, file=sys.stderr)
            return 1
    except SyntaxError:
        raise
    except BaseException:  # noqa: BLE001 - 最后的防护
        tb = traceback.format_exc()
        write_emergency(f"启动致命错误\n{tb}")
        try:
            if sys.stderr is not None:
                print(tb, file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
