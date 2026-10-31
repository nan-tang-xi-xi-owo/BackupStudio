"""后台守护进程：无 GUI 运行调度器，通过本地 IPC 应答 GUI / CLI 的查询。

默认无头运行；若缺少 GUI 支持（未安装 PySide6），CLI 的 ``daemon`` 命令仍可用。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime

from . import __version__
from .config import BackupTask, RunRecord
from .engine import TaskEngine
from .paths import config_dir, ipc_endpoint, log_dir, pid_file_path, state_file_path
from .schedule import describe_next, due, parse_ts
from .store import TaskStore

log = logging.getLogger("backupstudio.daemon")

IPC_PORT = 49371
TICK_SECONDS = 15


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

def setup_logging(verbose: bool = False, filename: str = "daemon.log") -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            d / filename, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as exc:  # noqa: BLE001 - 日志不可用也不能让守护进程起不来
        root.addHandler(logging.NullHandler())
        print(f"守护进程日志不可用：{exc}", file=sys.stderr)
    if sys.stdout is not None and sys.stdout.isatty() and not os.environ.get("BACKUPSTUDIO_DAEMON"):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


# --------------------------------------------------------------------------
# IPC
# --------------------------------------------------------------------------

def _make_zmq_server(endpoint: str):
    import zmq  # type: ignore[import-not-found]

    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REP)
    sock.linger = 0
    sock.bind(endpoint)
    return ctx, sock


def daemon_snapshot(store: TaskStore, engine: TaskEngine) -> dict:
    tasks = []
    for t in store.list_tasks():
        tasks.append(
            {
                "id": t.id,
                "name": t.name,
                "enabled": t.enabled,
                "status": "running" if engine.is_running(t.id) else t.last_status,
                "last_run": t.last_run,
                "next": describe_next(t),
                "destination": t.destination,
                "format": t.options.fmt,
            }
        )
    return {
        "ok": True,
        "running": True,
        "pid": os.getpid(),
        "version": __version__,
        "started_at": _STARTED_AT,
        "tasks": tasks,
    }


_STARTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------

class DaemonService:
    def __init__(self, port: int = IPC_PORT) -> None:
        self.port = port
        self.endpoint = ipc_endpoint(port)
        self.store = TaskStore(config_dir() / "tasks.json")
        self.engine = TaskEngine(log_fn=lambda m: log.info(m))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._guard_lock = None

    # ------------------------------------------------------------------
    def run(self) -> int:
        # 单实例守卫（审查 E1-1.5）：跨进程文件锁（pid 存活判定 + 陈旧自愈），
        # 不再依赖"端口占用"这一有竞态且 pyzmq 缺失时失效的判据。
        from .locks import DirLock, LockTimeout

        try:
            self._guard_lock = DirLock(config_dir(), "daemon", timeout=0.0).acquire()
        except LockTimeout:
            log.error("已有另一个守护进程在运行（单实例锁被持有），退出")
            return 2
        self._write_pid()
        server = None
        try:
            if port_in_use(self.port):
                log.error("守护进程 IPC 端口 %s 已被占用（可能已有实例），退出", self.port)
                with contextlib.suppress(Exception):
                    self._guard_lock.release()
                return 2
            server = _make_zmq_server(self.endpoint)
        except ImportError:
            log.warning("未安装 pyzmq：守护进程继续运行，但 GUI 无法查询状态")
        except Exception as exc:  # noqa: BLE001
            log.warning("无法启动 IPC 服务 %s：%s", self.endpoint, exc)

        for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, sig):
                try:
                    signal.signal(getattr(signal, sig), self._on_signal)
                except (ValueError, OSError):  # 非主线程
                    pass
        if hasattr(signal, "SIGHUP"):
            try:
                signal.signal(signal.SIGHUP, self._on_signal)
            except (ValueError, OSError):
                pass

        log.info("守护进程启动 pid=%s 版本=%s 数据目录=%s", os.getpid(), __version__, config_dir())
        log.info("命令行：%s", sys.argv)
        # ---- 崩溃残包清扫（审查 M23）：对本配置所有任务的备份目录各扫一次，
        #      清掉崩溃/强杀留下的 .bs-tmp-* 与 bs-stage-*。失败绝不阻塞启动。
        try:
            from .engine import sweep_orphans

            gone = sweep_orphans([t.destination for t in self.store.list_tasks()])
            if gone:
                log.info("启动清扫：移除 %d 个崩溃残包：%s", len(gone), ", ".join(p.name for p in gone[:8]))
        except Exception:  # noqa: BLE001
            log.debug("启动清扫崩溃残包失败（忽略）", exc_info=True)
        ipc_thread = None
        if server is not None:
            ipc_thread = threading.Thread(target=self._ipc_loop, args=(server,), daemon=True, name="bs-ipc")
            ipc_thread.start()
        try:
            while not self._stop.is_set():
                try:
                    self._tick()
                except Exception:
                    log.exception("调度轮询异常")
                self._wake.wait(TICK_SECONDS)
                self._wake.clear()
        finally:
            log.info("守护进程退出")
            self._cleanup(server)
        return 0

    def _on_signal(self, signum, frame) -> None:  # noqa: ANN001
        log.info("收到信号 %s，准备退出", signum)
        self._stop.set()
        self._wake.set()

    def _tick(self) -> None:
        # 重新读取配置（GUI/CLI 保存后守护进程自动生效）。
        # 改为"三方合并式重载"：保留内存里尚未落盘的改动（例如回写失败后的
        # last_run 兜底更新），同时吸收外部新增/修改的任务。
        if self.store.reload_if_changed():
            log.info("已同步配置：%d 个任务", len(self.store.list_tasks()))
        now = datetime.now()
        for task in self.store.list_tasks():
            if not task.enabled or task.schedule.type == "manual":
                continue
            if self.engine.is_running(task.id):
                continue
            if due(task, now, parse_ts(task.last_run)):
                log.info("计划触发：%s（%s）", task.name, task.schedule.describe())
                threading.Thread(target=self._execute, args=(task,), daemon=True, name=f"bs-task-{task.id}").start()
        _write_state(self.store, self.engine)

    def _execute(self, task: BackupTask) -> None:
        try:
            result = self.engine.run(task, trigger="schedule")
            rec = result.record
        except Exception:
            log.exception("任务执行异常：%s", task.name)
            return
        # 回写任务状态（磁盘优先；失败则退回内存更新，保证 due() 判定立即失效）
        try:
            fresh = TaskStore(self.store.path)
            t = fresh.get(task.id)
            if t is None:
                # 任务运行期间已被删除：同步删除内存中的副本，避免 15s 后再次触发
                log.warning("任务 %s 在运行期间已被删除，不再回写状态", task.id)
                self.store.delete(task.id)
            else:
                _apply_record(t, rec, result.message)
                fresh.upsert(t)
                fresh.save()
        except Exception:
            log.exception("回写任务状态失败，改用内存更新（防止重复触发）")
            mem = self.store.get(task.id)
            if mem is not None:
                _apply_record(mem, rec, result.message)
        if rec.status == "failure":
            _native_notify("BackupStudio 备份失败", f"{task.name}：{(rec.errors or ['未知错误'])[0][:150]}", "critical")
        elif rec.status == "success":
            log.info("任务成功：%s", task.name)

    # ------------------------------------------------------------------
    def _ipc_loop(self, server) -> None:  # noqa: ANN001
        ctx, sock = server
        while not self._stop.is_set():
            try:
                if not sock.poll(500):
                    continue
                raw = sock.recv()
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                log.debug("IPC 收包异常：%s", exc)
                time.sleep(0.2)
                continue
            # 已经收下一个请求：此后**必须**回一次包，否则 REP 状态机停在
            # "已收未回"，该插座永久楔死（W-A5-1 实测：之后所有 ping/stop 无应答）。
            # _reply 自身保证返回 dict，不抛异常。
            try:
                reply = self._reply(raw)
            except Exception as exc:  # noqa: BLE001 - 兜底，仍要回包
                log.exception("IPC 生成回复异常")
                reply = {"ok": False, "error": f"内部错误：{type(exc).__name__}"}
            try:
                sock.send(json.dumps(reply, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                # 回包失败（进程退出/插座损坏）→ 重建，避免留下"已收未回"的死插座
                log.warning("IPC 回包失败（%s），重建插座", exc)
                try:
                    ctx.close(0)
                except Exception:
                    pass
                try:
                    ctx, sock = _make_zmq_server(self.endpoint)
                except Exception as exc2:  # noqa: BLE001
                    log.error("IPC 插座重建失败：%s", exc2)
                    break
        try:
            sock.close(0)
            ctx.term()
        except Exception:
            pass

    def _reply(self, raw) -> dict:  # noqa: ANN001
        """把原始字节变成回复字典：任何畸形载荷都得到结构化错误回复，绝不抛出。"""
        try:
            data = json.loads(bytes(raw).decode("utf-8"))
        except Exception:  # noqa: BLE001 - 非法 JSON（无法解码/语法错）
            return {"ok": False, "error": "畸形请求：不是合法 JSON"}
        if not isinstance(data, dict):
            # json.loads("[]") / b'"hi"' / b"123" 都会成功但不是对象 ——
            # 旧实现在此 data.get(...) → AttributeError → 该轮无 send → 插座永久楔死
            return {"ok": False, "error": "畸形请求：期望 JSON 对象"}
        try:
            return self._handle(data)
        except Exception as exc:  # noqa: BLE001 - 处理异常也要回包
            log.exception("IPC 处理命令失败")
            return {"ok": False, "error": f"处理失败：{type(exc).__name__}"}

    def _handle(self, req: dict) -> dict:
        # 入参必须是 dict：调用方（_reply）已校验，这里再兜一层防御直接调用者
        # （审查 W-A5-1：非 dict 载荷曾让 cmd 未定义/AttributeError 楔死插座）。
        if not isinstance(req, dict):
            return {"ok": False, "error": f"畸形请求：期望 JSON 对象，实得 {type(req).__name__}"}
        cmd = req.get("cmd", "status")
        if not isinstance(cmd, str):
            return {"ok": False, "error": "畸形请求：cmd 必须是字符串"}
        if cmd == "status":
            return daemon_snapshot(self.store, self.engine)
        if cmd == "run":
            tid = req.get("task_id")
            if not isinstance(tid, str):
                return {"ok": False, "error": "畸形请求：task_id 必须是字符串"}
            task = self.store.get(tid)
            if task is None:
                return {"ok": False, "error": "任务不存在"}
            if self.engine.is_running(tid):
                return {"ok": False, "error": "任务正在运行"}
            threading.Thread(target=self._execute, args=(task,), daemon=True).start()
            return {"ok": True}
        if cmd == "reload":
            # 立即合并磁盘改动（原先只是把 mtime 缓存清空等下一 tick，最长滞后 15s）
            self.store.reload_if_changed()
            self._wake.set()
            return {"ok": True, "tasks": len(self.store.list_tasks())}
        if cmd == "stop":
            self._stop.set()
            self._wake.set()
            return {"ok": True, "message": "正在退出"}
        if cmd == "ping":
            return {"ok": True, "pid": os.getpid(), "version": __version__}
        return {"ok": False, "error": f"未知命令：{cmd}"}

    # ------------------------------------------------------------------
    def _write_pid(self) -> None:
        target = pid_file_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{os.getpid()}\n{self.port}\n", encoding="ascii")
        except OSError as exc:
            log.warning("写入 PID 文件失败：%s", exc)

    def _cleanup(self, server) -> None:  # noqa: ANN001
        if self._guard_lock is not None:
            with contextlib.suppress(Exception):
                self._guard_lock.release()
        try:
            pid = pid_file_path()
            if pid.exists() and pid.read_text(encoding="ascii").splitlines()[0].strip() == str(os.getpid()):
                pid.unlink()
        except (OSError, IndexError):
            pass
        try:
            state_file_path().write_text(
                json.dumps({"running": False, "pid": None, "updated_at": _now()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass


def _apply_record(task: BackupTask, rec: RunRecord, fallback_message: str) -> None:
    """把一次执行结果写进任务记录字段（磁盘与内存两条路径共用）。"""
    task.last_run = rec.started_at
    task.last_status = rec.status
    task.last_message = rec.errors[0] if rec.errors else fallback_message
    task.last_archive = rec.archive
    task.last_duration = rec.duration


def _write_state(store: TaskStore, engine: TaskEngine) -> None:
    try:
        data = {
            "running": True,
            "pid": os.getpid(),
            "updated_at": _now(),
            "tasks": [
                {"id": t.id, "name": t.name, "next": describe_next(t), "status": t.last_status}
                for t in store.list_tasks()
            ],
        }
        tmp = state_file_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, state_file_path())
    except OSError:
        pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _native_notify(title: str, message: str, urgency: str = "normal") -> None:
    try:
        from .notify import native_notification

        native_notification(title, message, urgency)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 客户端辅助（供 GUI / CLI 使用）
# --------------------------------------------------------------------------

def port_in_use(port: int = IPC_PORT) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def read_pid_port() -> tuple[int | None, int]:
    try:
        lines = pid_file_path().read_text(encoding="ascii").splitlines()
        pid = int(lines[0]) if lines else None
        port = int(lines[1]) if len(lines) > 1 else IPC_PORT
        return pid, port
    except (OSError, ValueError, IndexError):
        return None, IPC_PORT


def ipc_request(payload: dict, timeout_ms: int = 1500) -> dict | None:
    """发送一条 IPC 请求；失败返回 None。"""
    try:
        import zmq  # type: ignore[import-not-found]
    except ImportError:
        return None
    _, port = read_pid_port()
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.linger = 0
    sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
    sock.setsockopt(zmq.SNDTIMEO, timeout_ms)
    try:
        sock.connect(ipc_endpoint(port))
        sock.send(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        raw = sock.recv()
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    finally:
        try:
            sock.close(0)
        except Exception:
            pass


def daemon_status(timeout_ms: int = 500) -> dict:
    """查询守护进程状态（优先 IPC，其次 PID 文件 + 状态文件）。

    `timeout_ms` 故意较短：界面在启动时调用，不能被慢速进程枚举拖住。
    """
    from .autostart import find_daemon_pid

    resp = ipc_request({"cmd": "status"}, timeout_ms=timeout_ms)
    if resp is not None:
        return resp
    pid = find_daemon_pid(pid_file_path())
    if pid:
        return {"running": True, "pid": pid, "tasks": [], "note": "IPC 无响应，可能正在启动"}
    return {"running": False, "pid": None, "tasks": []}


def ensure_daemon() -> tuple[bool, str]:
    """确保守护进程在运行。

    N4：Flatpak 沙箱内**不启动**独立守护进程，改为明确告知原因。
    门户（xdg-desktop-portal）的授权绑定在发起请求的沙箱实例上，
    子进程守护进程拿不到 → home 之外的备份在后台静默失败
    （用户看到的现象是「手动备份成功、定时备份从不执行」）。
    GUI 自身有内置调度器（``AppState.start_scheduler``），关窗到托盘即可继续定时备份，
    因此这里拒绝是有替代路径的，不是功能缺失。
    """
    from . import flatpak

    if not flatpak.daemon_supported():
        return False, flatpak.daemon_hint()
    from .autostart import find_daemon_pid, spawn_daemon

    if find_daemon_pid(pid_file_path(), allow_scan=True):
        return True, "守护进程已在运行"
    proc = spawn_daemon(minimized=True)
    if proc is None:
        return False, "无法启动守护进程（请检查 Python/程序路径）"
    deadline = time.time() + 8
    while time.time() < deadline:
        resp = ipc_request({"cmd": "ping"}, timeout_ms=400)
        if resp is not None:
            return True, f"守护进程已启动（PID {resp.get('pid')}）"
        if find_daemon_pid(pid_file_path(), allow_scan=True):
            time.sleep(0.5)
            return True, "守护进程已启动"
        time.sleep(0.25)
    return False, "守护进程启动超时，请查看日志目录中的 daemon.log"


def stop_daemon_safe() -> tuple[bool, str]:
    """先通过 IPC 请求优雅退出，超时再强制结束。"""
    from .autostart import find_daemon_pid, stop_daemon

    if not find_daemon_pid(pid_file_path(), allow_scan=True):
        return True, "守护进程未在运行"
    resp = ipc_request({"cmd": "stop"}, timeout_ms=1500)
    if resp:
        log.info("已通过 IPC 请求守护进程退出：%s", resp)
    deadline = time.time() + 6
    while time.time() < deadline:
        if not find_daemon_pid(pid_file_path(), allow_scan=True):
            return True, "守护进程已退出"
        time.sleep(0.2)
    ok = stop_daemon(pid_file_path())
    if ok and not find_daemon_pid(pid_file_path(), allow_scan=True):
        return True, "守护进程已强制停止"
    return False, "停止守护进程失败：请查看日志目录中的 daemon.log"


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    # 冻结程序的 windowed 模式：stdout/stderr 可能为 None，中文写日志会失败
    for _name in ("stdout", "stderr"):
        _stream = getattr(sys, _name, None)
        if _stream is not None:
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")  # noqa: SLF001
            except (AttributeError, ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(prog="backupstudio.daemon", description="BackupStudio 后台守护进程")
    parser.add_argument("--port", type=int, default=IPC_PORT)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--once", action="store_true", help="只检查一次是否有到期任务（调试用）")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    service = DaemonService(args.port)
    if args.once:
        service._tick()
        return 0
    return service.run()


if __name__ == "__main__":
    sys.exit(main())
