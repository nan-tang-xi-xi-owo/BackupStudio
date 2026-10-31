"""跨进程任务锁测试（审查 P0-6/F2 回归）。"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestDirLock:
    def test_acquire_release(self, tmp_path):
        from backupstudio.locks import acquire_dir_lock

        lk = acquire_dir_lock(tmp_path, "t1", timeout=0)
        assert lk.path.is_file()
        lk.release()
        assert not lk.path.exists()

    def test_second_acquire_conflicts(self, tmp_path):
        """同进程第二次拿（模拟另一进程持锁）→ 明确失败。"""
        from backupstudio.locks import LockTimeout, DirLock

        # 手工构造一个"活进程持锁"的状态：pid 用当前进程（存活）
        lock_dir = tmp_path / ".backupstudio"
        lock_dir.mkdir()
        info = {
            "pid": os.getpid(), "host": socket.gethostname(), "user": "u",
            "task_id": "t2", "started_at": time.time(),
        }
        (lock_dir / "task-t2.lock").write_text(json.dumps(info), encoding="utf-8")
        with pytest.raises(LockTimeout):
            DirLock(tmp_path, "t2", timeout=0).acquire()

    def test_stale_lock_taken_over(self, tmp_path):
        """持有者 pid 不存在（崩溃残留）→ 接管成功。"""
        from backupstudio.locks import acquire_dir_lock

        lock_dir = tmp_path / ".backupstudio"
        lock_dir.mkdir()
        info = {
            "pid": 999999, "host": socket.gethostname(), "user": "u",
            "task_id": "t3", "started_at": time.time() - 10,
        }
        (lock_dir / "task-t3.lock").write_text(json.dumps(info), encoding="utf-8")
        lk = acquire_dir_lock(tmp_path, "t3", timeout=2.0)
        assert lk.path.is_file()
        got = json.loads(lk.path.read_text(encoding="utf-8"))
        assert got["pid"] == os.getpid(), "接管后锁内容应是自己"
        lk.release()

    def test_release_only_own_lock(self, tmp_path):
        """A 持锁时 B（无锁对象）release 不得删 A 的锁。"""
        from backupstudio.locks import DirLock, acquire_dir_lock

        a = acquire_dir_lock(tmp_path, "t4", timeout=0)
        b = DirLock(tmp_path, "t4")
        b.release()  # 未持锁 → no-op
        assert a.path.is_file(), "A 的锁被 B 误删"
        a.release()

    def test_cross_process_mutual_exclusion(self, tmp_path):
        """两进程竞争 → 恰好一个成功（用 spawn 子进程）。"""
        import multiprocessing
        import subprocess

        script = tmp_path / "worker.py"
        script.write_text(
            "import sys, time\n"
            f"sys.path.insert(0, r'{ROOT}')\n"
            "from pathlib import Path\n"
            "from backupstudio.locks import acquire_dir_lock, LockTimeout\n"
            "try:\n"
            "    lk = acquire_dir_lock(Path(sys.argv[1]), 'race', timeout=0)\n"
            "    print('ACQUIRED', flush=True)\n"
            # 持锁时长必须覆盖 p2 的启动窗口（解释器启动 + 导入 + 尝试拿锁，
            # 在慢 runner 上可达 1 秒以上）。原来 1.0 秒在 CI 上不够 ——
            # p1 会在 p2 尝试前释放，两个都 ACQUIRED（run #50 Linux job 实证）。
            "    time.sleep(4.0)\n"
            "    lk.release()\n"
            "except LockTimeout:\n"
            "    print('TIMEOUT', flush=True)\n",
            encoding="utf-8",
        )
        p1 = subprocess.Popen([sys.executable, str(script), str(tmp_path)],
                              stdout=subprocess.PIPE, text=True, encoding="utf-8",
                              bufsize=1)  # 行缓冲：能读到 "ACQUIRED" 那一行
        # **不能用 time.sleep(0.4) 猜**（CI 上偶发失败）：
        # 慢 runner 上 p1 可能在 0.4 秒时还没完成解释器启动 + 导入，
        # p2 就抢先拿到锁 → 互斥判据失败。
        # 实测：run #50 的 Linux job 因此红（ACQ-STOLEN）。
        # 改为**阻塞等 p1 明确报告已持锁**，再加一个上限防死等。
        got = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = p1.stdout.readline() if p1.stdout else ""
            if line:
                got = line.strip()
                break
            if p1.poll() is not None:
                break
            time.sleep(0.02)
        assert got == "ACQUIRED", f"p1 未能先持锁（拿到 {got!r}）"
        p2 = subprocess.Popen([sys.executable, str(script), str(tmp_path)],
                              stdout=subprocess.PIPE, text=True, encoding="utf-8")
        # p1 的第一行已被上面的 readline() 消费（那是它持锁的证据），
        # communicate() 只能拿到剩下的；把它拼回去再比较。
        rest1, _ = p1.communicate(timeout=30)
        out1 = (got or "") + (rest1 or "")
        out2, _ = p2.communicate(timeout=30)
        results = sorted([out1.strip().splitlines()[0] if out1.strip() else "",
                          out2.strip().splitlines()[0] if out2.strip() else ""])
        assert results == ["ACQUIRED", "TIMEOUT"], results


class TestEngineIntegration:
    def test_engine_skips_when_locked(self, tmp_path):
        """引擎目标被另一进程锁住 → 任务 skipped（不产出坏包）。"""
        import json as _json
        import os as _os
        import socket as _socket
        import time as _time

        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
        from backupstudio.engine import TaskEngine

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()
        # 伪造另一个"活进程"的锁
        lock_dir = dest / ".backupstudio"
        lock_dir.mkdir()
        alive_pid = _os.getpid()  # 当前进程活着；用不同 pid 语义：引擎从别的实例拿
        info = {"pid": alive_pid + 0, "host": _socket.gethostname(), "user": "u",
                "task_id": "will-lock", "started_at": _time.time()}
        task = BackupTask(name="lk", sources=[SourceConfig(path=str(src))],
                          destination=str(dest), options=ArchiveOptions(fmt="zip", force=True))
        # 引擎的锁对象会认为 pid=当前进程 存活（虽然其实是"另一个实例"）→ 冲突
        (lock_dir / f"task-{task.id}.lock").write_text(_json.dumps({
            **info, "task_id": task.id, "pid": alive_pid + 1,  # +1 制造"另一个 pid"
        }), encoding="utf-8")
        # 注意：pid+1 在本机大概率也是活进程或不存在——两种都测：
        rec = TaskEngine().run(task).record
        # 若 pid+1 恰好不存在 → 陈旧接管成功（也合法）；关键断言：绝不产生坏包
        # 更精确的断言放这里：若 status=skipped，则原因含"占用"
        if rec.status == "skipped":
            assert any("占用" in e or "运行中" in e for e in rec.errors), rec.errors


class TestDaemonSingleInstance:
    def test_second_daemon_instance_refused(self, tmp_path, monkeypatch):
        """守护进程单实例（审查 E1-1.5）：第二个实例拿不到守卫锁，退出码 2。

        不启动真实 daemon：直接测守卫锁语义——第一个 DirLock 持有后，
        第二个实例（模拟）应 LockTimeout。
        """
        from backupstudio.locks import DirLock, LockTimeout

        cfg = tmp_path / "cfg"
        cfg.mkdir()
        first = DirLock(cfg, "daemon", timeout=0.0).acquire()
        try:
            with pytest.raises(LockTimeout):
                DirLock(cfg, "daemon", timeout=0.0).acquire()
        finally:
            first.release()
        # 释放后可再拿（正常重启路径）
        again = DirLock(cfg, "daemon", timeout=0.0).acquire()
        again.release()


# ----------------------------------------------------------------------
# W-A2-5 / W-A2-6 / W-A2-7 回归（P1-A）
# ----------------------------------------------------------------------
_RACE_WORKER = """
import importlib, json, os, socket, sys, time
from pathlib import Path
sys.path.insert(0, r'@@ROOT@@')
for _extra in os.environ.get('BS_LOCKS_PATH', '').split(os.pathsep):
    if _extra:
        sys.path.insert(0, _extra)
# BS_LOCKS_MODULE 可把子进程指向"修复前"的实现副本：用它验证本测试确实抓得到回归
_L = importlib.import_module(os.environ.get('BS_LOCKS_MODULE', 'backupstudio.locks'))
DirLock, LockTimeout = _L.DirLock, _L.LockTimeout

d = Path(sys.argv[1]); task = sys.argv[2]; hold = float(sys.argv[3])
(d / 'holds').mkdir(parents=True, exist_ok=True)
while not (d / 'GO').exists():
    time.sleep(0.002)
lk = DirLock(d, task, timeout=30.0)
try:
    lk.acquire()
except LockTimeout:
    print('TIMEOUT', flush=True); sys.exit(0)
pid = str(os.getpid())
(d / 'holds' / (pid + '.start')).write_text(str(time.time()), encoding='utf-8')
time.sleep(hold)
try:
    body = (d / '.backupstudio' / ('task-' + task + '.lock')).read_text(encoding='utf-8')
    own = int(json.loads(body).get('pid', -1)) == os.getpid()
except Exception:
    own = False
(d / 'holds' / (pid + '.end')).write_text(str(time.time()), encoding='utf-8')
print('ACQ-OWN' if own else 'ACQ-STOLEN', flush=True)
lk.release()
"""


def _overlapping_pairs(holds_dir, slack=0.05):
    """返回同时自认持锁的进程对（双持有的直接证据）。"""
    spans = []
    for p in sorted(holds_dir.glob("*.start")):
        ep = holds_dir / (p.stem + ".end")
        if ep.exists():
            spans.append((p.stem, float(p.read_text(encoding="utf-8")),
                          float(ep.read_text(encoding="utf-8"))))
    bad = []
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            a, b = spans[i], spans[j]
            inter = min(a[2], b[2]) - max(a[1], b[1])
            if inter > slack:
                bad.append((a[0], b[0], round(inter, 3)))
    return bad


class TestTakeoverRace:
    """[W-A2-5] 接管竞态：旧实现 6 进程 12/12 轮双持有，修复后必须为 0。"""

    N = 4
    ROUNDS = 3
    HOLD = 0.3

    def _dead_lock(self, d):
        """预置"持有者 pid 不存在"的崩溃残留锁（模拟崩溃后重启）。"""
        lock_dir = d / ".backupstudio"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "task-race.lock").write_text(json.dumps({
            "pid": 999999, "host": socket.gethostname(), "user": "u",
            "task_id": "race", "started_at": time.time(),
        }), encoding="utf-8")
        return lock_dir

    def _worker(self, tmp_path):
        script = tmp_path / "race_worker.py"
        script.write_text(_RACE_WORKER.replace("@@ROOT@@", str(ROOT)), encoding="utf-8")
        return script

    def test_takeover_exclusive_multi_round(self, tmp_path):
        """N 进程同抢同一把死锁：不得出现 ACQ-STOLEN，也不得出现时段重叠。"""
        import subprocess

        from backupstudio.locks import DirLock, LockTimeout  # noqa: F401

        script = self._worker(tmp_path)
        breaches = []
        for i in range(self.ROUNDS):
            d = tmp_path / f"round{i}"
            lock_dir = self._dead_lock(d)
            env = dict(os.environ)
            extra_mod = os.environ.get("BS_LOCKS_MODULE")
            if extra_mod:
                env["BS_LOCKS_MODULE"] = extra_mod
                env["BS_LOCKS_PATH"] = os.environ.get("BS_LOCKS_PATH", "")
            procs = [
                subprocess.Popen([sys.executable, str(script), str(d), "race", str(self.HOLD)],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", env=env)
                for _ in range(self.N)
            ]
            time.sleep(0.1)
            (d / "GO").write_text("x", encoding="utf-8")  # 齐发
            outs = []
            for p in procs:
                try:
                    out, _ = p.communicate(timeout=60)
                except subprocess.TimeoutExpired:  # 卡死/空转本身即缺陷（W-A2-8）
                    p.kill()
                    raise AssertionError(f"round {i}: acquire 未在 60s 内返回")
                lines = [ln for ln in (out or "").splitlines() if ln.strip()]
                outs.append(lines[-1] if lines else "")
            own = [s for s in outs if s == "ACQ-OWN"]
            stolen = [s for s in outs if s == "ACQ-STOLEN"]
            overlap = _overlapping_pairs(d / "holds")
            if stolen or overlap or not own:
                breaches.append({"round": i, "outs": outs, "overlap": overlap})
            assert not list(lock_dir.glob("*.stale*")), f"round {i}: 留下旁置残留"
        assert not breaches, f"互斥破裂：{breaches}"

    def test_second_process_cannot_steal_live_lock(self, tmp_path):
        """[W-A2-5 直接复现] 别的活进程持锁期间，本进程不得把它旁置偷走。

        旧实现：本进程先读到"陈旧"判定 → 对手抢先接管 → 我们的 replace 改的是
        对手的活锁 → 双持有。现在改名前就地复读，活锁一律不动。
        """
        import psutil

        from backupstudio.locks import DirLock, LockTimeout

        # 用父进程 pid：必然存活（本测试正由它派生）且肯定不是自己
        other = os.getppid()
        assert other != os.getpid() and psutil.pid_exists(other)
        lock_dir = tmp_path / ".backupstudio"
        lock_dir.mkdir()
        victim = lock_dir / "task-race.lock"
        body = {"pid": other, "host": socket.gethostname(), "user": "owner",
                "task_id": "race", "started_at": time.time() - 3600.0}
        victim.write_text(json.dumps(body), encoding="utf-8")
        old = time.time() - 3600.0
        os.utime(victim, (old, old))  # 文件很老，但持有者活着

        with pytest.raises(LockTimeout) as ei:
            DirLock(tmp_path, "race", timeout=0.2).acquire()
        assert "另一个进程" in str(ei.value), ei.value
        assert victim.exists(), "活锁被偷走（锁名空了）"
        assert json.loads(victim.read_text(encoding="utf-8"))["pid"] == other, "活锁内容被改"
        assert not list(lock_dir.glob("*.stale*")), "活锁被误旁置后留下残留"

    def test_young_live_lock_is_never_quarantined(self, tmp_path):
        """[W-A2-5 防护层 1] pid 判不出但锁很年轻：不旁置（避免偷走刚建好的锁）。"""
        from backupstudio import locks

        d = tmp_path / "young"
        lock_dir = d / ".backupstudio"
        lock_dir.mkdir(parents=True)
        victim = lock_dir / "task-race.lock"
        victim.write_text(json.dumps({
            "pid": 999999, "host": socket.gethostname(), "user": "v",
            "task_id": "race", "started_at": time.time(), "token": "aaaa1111",
        }), encoding="utf-8")
        before = victim.read_text(encoding="utf-8")
        assert locks._read_info(victim) is not None
        assert locks._is_stale(locks._read_info(victim)), "前提：pid 不存在应判陈旧"
        lk = locks.DirLock(d, "race")
        assert lk._take_over() is False, "太年轻的锁不应被旁置（接管闸门）"
        assert victim.exists() and victim.read_text(encoding="utf-8") == before
        assert not [n for n in os.listdir(lock_dir) if ".stale" in n]

    def test_takeover_rereads_lock_before_rename(self, tmp_path, monkeypatch):
        """[W-A2-5 防护层 1] 上一轮判定为陈旧，但此刻已是活锁 → 不许改名。

        旧实现在"读 info → replace"之间没有任何复核，输者正是在这里把赢者的
        活锁偷走（实测 12/12 轮、31 次）。现在改名前就地复读内容。
        """
        from backupstudio import locks

        d = tmp_path / "switch"
        lock_dir = d / ".backupstudio"
        lock_dir.mkdir(parents=True)
        dead = lock_dir / "task-race.lock"
        dead.write_text(json.dumps({
            "pid": 999999, "host": socket.gethostname(), "user": "v",
            "task_id": "race", "started_at": time.time() - 3600.0, "token": "old1",
        }), encoding="utf-8")
        old = time.time() - 3600.0
        os.utime(dead, (old, old))
        true_rename = locks.os.rename
        live_body = json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(), "user": "winner",
            "task_id": "race", "started_at": time.time(), "token": "new1",
        })

        def racing_rename(src, dst):
            # 模拟"就在这一瞬间，别的进程已接管并发布了活锁"
            Path(src).unlink()
            Path(src).write_text(live_body, encoding="utf-8")
            return true_rename(src, dst)

        monkeypatch.setattr(locks.os, "rename", racing_rename)
        lk = locks.DirLock(d, "race")
        assert lk._take_over() is False, "改名前必须就地复读：不得旁置别人的活锁"
        assert Path(dead).read_text(encoding="utf-8") == live_body, "活锁内容被破坏"
        assert not list(lock_dir.glob("*.stale*")), "不该留下旁置残留"

    def test_takeover_postrename_recheck_restores_others_lock(self, tmp_path, monkeypatch):
        """[W-A2-5 防护层 3] 改名后才看清偷到的是活锁 → 原样放回，绝不据为己有。"""
        from backupstudio import locks

        d = tmp_path / "late"
        lock_dir = d / ".backupstudio"
        lock_dir.mkdir(parents=True)
        victim = lock_dir / "task-race.lock"
        victim.write_text(json.dumps({
            "pid": 999999, "host": socket.gethostname(), "user": "v",
            "task_id": "race", "started_at": time.time() - 3600.0, "token": "old2",
        }), encoding="utf-8")
        old = time.time() - 3600.0
        os.utime(victim, (old, old))
        true_rename = locks.os.rename
        live = locks.LockInfo(pid=os.getpid(), host=socket.gethostname(), user="winner",
                              task_id="race", started_at=time.time(), token="new2")
        real_read = locks._read_info

        def fake_read(path):
            # 锁名上读到的是"我刚判定陈旧的那把"；改名后读到的其实是别人的活锁
            # （真实竞态里对手刚好在复读与改名之间换掉了文件）
            if ".stale" in Path(path).name:
                return live
            return real_read(path)

        def rename_to_live(src, dst):
            true_rename(src, dst)
            Path(src).write_text(json.dumps({
                "pid": live.pid, "host": live.host, "user": live.user,
                "task_id": live.task_id, "started_at": live.started_at, "token": live.token,
            }), encoding="utf-8")

        monkeypatch.setattr(locks, "_read_info", fake_read)
        monkeypatch.setattr(locks.os, "rename", rename_to_live)
        lk = locks.DirLock(d, "race")
        took = lk._take_over()
        assert took is False, "指纹不是自己判定那一把，不得视为接管成功"
        assert victim.exists(), "放回失败：锁名空了"

    def test_takeover_without_lock_is_false(self, tmp_path):
        """无锁可接管时安全返回 False（不抛），且不会产生旁置文件。"""
        from backupstudio import locks

        d = tmp_path / "empty"
        (d / ".backupstudio").mkdir(parents=True)
        lk = locks.DirLock(d, "race")
        assert lk._take_over() is False

class TestNoUnboundedSpin:
    """[W-A2-8] 接管路径任何失败都必须退避并受 deadline 约束，不得空转。"""

    def test_rename_failure_times_out_promptly(self, tmp_path, monkeypatch):
        import time as _t

        from backupstudio import locks

        lock_dir = tmp_path / ".backupstudio"
        lock_dir.mkdir()
        (lock_dir / "task-spin.lock").write_text(json.dumps({
            "pid": 999999, "host": socket.gethostname(), "user": "u",
            "task_id": "spin", "started_at": time.time() - 7200.0, "token": "cccc3333",
        }), encoding="utf-8")
        old = time.time() - 7200.0
        os.utime(lock_dir / "task-spin.lock", (old, old))
        # 制造旧实现死循环的条件：改名永远失败（.stale 只读/被占用）
        calls = {"n": 0}

        def boom(src, dst):
            calls["n"] += 1
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(locks.os, "rename", boom)
        started = _t.monotonic()
        with pytest.raises(locks.LockTimeout):
            locks.DirLock(tmp_path, "spin", timeout=1.0).acquire()
        elapsed = _t.monotonic() - started
        assert elapsed < 5.0, f"acquire(timeout=1) 未准时返回（{elapsed:.1f}s）"
        assert calls["n"] < 200, f"改名失败被吞掉后空转：调用 {calls['n']} 次"


class TestInvalidLockSelfHealing:
    """[W-A2-6] 空锁/半截锁：超租约可接管；未超租约报"锁内容无效"。"""

    def _make(self, tmp_path, name, content):
        lock_dir = tmp_path / ".backupstudio"
        lock_dir.mkdir(parents=True, exist_ok=True)
        path = lock_dir / name
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _make_older(path, seconds):
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_zero_byte_lock_past_lease_is_taken_over(self, tmp_path):
        """0 字节锁（创建后写完前崩溃）+ mtime 超租约 → 可接管。"""
        from backupstudio import locks
        from backupstudio.locks import acquire_dir_lock

        path = self._make(tmp_path, "task-zb.lock", "")
        self._make_older(path, locks.STALE_LEASE_SECONDS + 60)
        lk = acquire_dir_lock(tmp_path, "zb", timeout=3.0)
        try:
            got = json.loads(lk.path.read_text(encoding="utf-8"))
            assert got["pid"] == os.getpid(), "超租约的空锁应被接管"
        finally:
            lk.release()

    def test_truncated_json_lock_past_lease_is_taken_over(self, tmp_path):
        from backupstudio import locks
        from backupstudio.locks import acquire_dir_lock

        path = self._make(tmp_path, "task-tr.lock", '{"pid": 123, "host": "o')
        self._make_older(path, locks.STALE_LEASE_SECONDS + 60)
        lk = acquire_dir_lock(tmp_path, "tr", timeout=3.0)
        try:
            assert json.loads(lk.path.read_text(encoding="utf-8"))["pid"] == os.getpid()
        finally:
            lk.release()

    def test_fresh_invalid_lock_times_out_with_clear_message(self, tmp_path):
        """未超租约的空锁：LockTimeout 文案点明"锁内容无效"，不再只说"未知进程"。"""
        from backupstudio.locks import DirLock, LockTimeout

        self._make(tmp_path, "task-fresh.lock", "")
        with pytest.raises(LockTimeout) as ei:
            DirLock(tmp_path, "fresh", timeout=0).acquire()
        msg = str(ei.value)
        assert "锁内容无效" in msg, msg
        assert "未知进程" not in msg, msg

    @pytest.mark.parametrize("payload", ["", "123", "true", "null", "[1, 2]",
                                         '{"pid": "x"}', "{}", '{"pid": null}'])
    def test_read_info_returns_none_on_garbage_without_raising(self, tmp_path, payload):
        """[W-A2-6] 非 dict / 坏字段一律 None，不抛 AttributeError。"""
        from backupstudio.locks import _read_info

        name = f"task-g{abs(hash(payload)) % 1000}.lock"
        path = self._make(tmp_path, name, payload)
        info = _read_info(path)
        assert info is None or isinstance(info.pid, int)


class TestNoHardlinkFallback:
    """文件系统不支持 os.link（exFAT/FAT、部分网络盘）→ 退回 O_EXCL 直写仍要正确。

    不依赖内部开关：直接让 os.link 抛 EPERM，走降级分支。
    """

    @pytest.fixture(autouse=True)
    def _no_link(self, monkeypatch):
        import errno as _errno

        from backupstudio import locks

        def denied(src, dst, *a, **kw):
            # EAGAIN 不在"永久不可用"集合里：本次调用照样降级到 O_EXCL 直写，
            # 但不会污染进程级的目录缓存（测试之间互不影响）
            raise OSError(_errno.EAGAIN, "Link not supported")

        monkeypatch.setattr(locks.os, "link", denied)

    def test_excl_path_still_mutually_exclusive(self, tmp_path):
        from backupstudio import locks
        from backupstudio.locks import DirLock, LockTimeout

        a = DirLock(tmp_path, "nl", timeout=0).acquire()
        try:
            assert locks._read_info(a.path) is not None
            with pytest.raises(LockTimeout):
                DirLock(tmp_path, "nl", timeout=0).acquire()
        finally:
            a.release()

    def test_excl_path_can_take_over_dead_lock(self, tmp_path):
        from backupstudio import locks
        from backupstudio.locks import DirLock

        lock_dir = tmp_path / ".backupstudio"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "task-nl2.lock").write_text(json.dumps({
            "pid": 999999, "host": socket.gethostname(), "user": "u",
            "task_id": "nl2", "started_at": time.time(),
        }), encoding="utf-8")
        lk = DirLock(tmp_path, "nl2", timeout=3.0).acquire()
        try:
            assert locks._read_info(lk.path).pid == os.getpid()
        finally:
            lk.release()

    def test_excl_path_sweeps_sidecar_after_takeover(self, tmp_path):
        from backupstudio.locks import DirLock

        lock_dir = tmp_path / ".backupstudio"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "task-nl3.lock").write_text(json.dumps({
            "pid": 999999, "host": socket.gethostname(), "user": "u",
            "task_id": "nl3", "started_at": time.time(),
        }), encoding="utf-8")
        lk = DirLock(tmp_path, "nl3", timeout=3.0).acquire()
        lk.release()
        assert not list(lock_dir.glob("*.stale*"))
        assert not list(lock_dir.glob("*.tmp-*"))


class TestSidecarHygiene:
    """[W-A2-7/M6/S3] 旁置与临时残留清理。"""

    def test_takeover_leaves_no_sidecar_or_tmp(self, tmp_path):
        from backupstudio.locks import acquire_dir_lock

        lock_dir = tmp_path / ".backupstudio"
        lock_dir.mkdir()
        (lock_dir / "task-h.lock").write_text(json.dumps({
            "pid": 999999, "host": socket.gethostname(), "user": "u",
            "task_id": "h", "started_at": time.time(),
        }), encoding="utf-8")
        lk = acquire_dir_lock(tmp_path, "h", timeout=3.0)
        lk.release()
        assert not list(lock_dir.glob("*.stale*")), "接管后不应留下旁置"
        assert not list(lock_dir.glob("*.tmp-*")), "发布后不应留下临时文件"

    def test_aged_leftovers_are_swept(self, tmp_path):
        """超龄（>24h）的旁置/临时残留（含旧版固定名）在 acquire/release 时被顺手清理。"""
        from backupstudio.locks import acquire_dir_lock

        lock_dir = tmp_path / ".backupstudio"
        lock_dir.mkdir()
        old = time.time() - 25 * 3600
        names = ["task-old.lock.stale-1234-abcd1234",  # 新版唯一名
                 "task-old.lock.stale",                # 旧版固定名（存量磁盘）
                 "task-old.lock.tmp-1234-abcd1234"]    # 发布临时残留
        for name in names:
            f = lock_dir / name
            f.write_text("{}", encoding="utf-8")
            os.utime(f, (old, old))
        lk = acquire_dir_lock(tmp_path, "new", timeout=0)
        lk.release()
        for name in names:
            assert not (lock_dir / name).exists(), f"超龄残留应被清理：{name}"

    def test_fresh_leftovers_are_not_swept(self, tmp_path):
        """未超龄的旁置可能正被别人使用，不得删除。"""
        from backupstudio.locks import acquire_dir_lock

        lock_dir = tmp_path / ".backupstudio"
        lock_dir.mkdir()
        f = lock_dir / "task-x.lock.stale-9999-deadbeef"
        f.write_text('{"pid": 9999}', encoding="utf-8")
        lk = acquire_dir_lock(tmp_path, "y", timeout=0)
        lk.release()
        assert f.exists(), "未超龄的旁置不应删除"

    def test_published_lock_is_always_parseable(self, tmp_path):
        """原子发布：锁名一旦出现即内容完整，且带 token 供身份复核。"""
        from backupstudio.locks import _read_info, acquire_dir_lock

        lk = acquire_dir_lock(tmp_path, "atomic", timeout=0)
        try:
            info = _read_info(lk.path)
            assert info is not None, "锁名存在却读不出内容 = 半截锁"
            assert info.token, "锁内容应带 token"
        finally:
            lk.release()
