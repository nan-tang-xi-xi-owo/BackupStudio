"""跨进程任务锁：防止 GUI 与 daemon 同时向同一目标执行同一任务。

背景（审查 P0-6/F2 实测）：引擎的 threading.Lock 只在进程内有效；
两进程同秒同名会产出坏包、双方都报 success。本模块提供基于文件系统的
目录锁（target 目录下 `.backupstudio/task-<id>.lock`），满足四条件：
* 落在文件系统上（跨进程有效）；
* 可判定持有者存活（同机 pid + psutil；否则租约超时）→ 崩溃自愈；
* 内容可读（pid/host/user/时间），等待时可解释；
* 接管有防竞态（唯一旁置名 + 独占发布 + 接管后身份复核）。

接管协议（审查 W-A2-5 / W-A2-6 / W-A2-7 / W-A2-8）
--------------------------------------------------
旧实现是"读到 stale → 固定名 replace → suppress 吞异常 → continue"，有四个洞：

1) 互斥破裂（W-A2-5，S1）：N 个进程先读到同一把陈旧锁；赢者接管并新建活锁后，
   输者才执行它早已决定的 replace —— 此时 self.path 指向赢者的**活锁**，
   活锁被旁置偷走，双方都自认为持有（实测 6 进程 × 12/12 轮复现、31 次窃取）。
   → 现在：改名目标唯一化（pid+随机 token）；改名失败一律 `return False`
   回到循环重判（不再吞异常后照抢）；改名后**复核** mtime+内容指纹就是我判定
   的那把锁才删除，对不上说明偷到别人的活锁 → 原样放回；acquire 成功后再
   **读回复核**身份，不是自己就放手重抢。
2) 空锁/半截锁永久卡死（W-A2-6，S1）：`_try_acquire` 先 O_EXCL 创建、后写内容，
   创建后写完前被杀 → 0 字节锁 → `_read_info` 恒 None → 永不判 stale → 同机
   永久 LockTimeout（任务/GUI/daemon 全封死）；顶层非 dict 的 JSON 还会抛异常。
   → 现在：发布原子化（先写 `.tmp-<pid>-<token>`，再 `os.link` 挂到锁名；平台
   不支持硬链接才退回 O_EXCL 直写），"存在即完整"；`_read_info` 对空/坏/非 dict
   一律返回 None 不抛；内容无效但文件存在时按 **mtime 超租约** STALE_LEASE_SECONDS
   判可接管（自愈兜底），未超租约时 LockTimeout 文案点明"锁内容无效"。
3) 无界空转（W-A2-8，S1）：replace 失败被 suppress 后 `continue`，该分支既无
   sleep 也不检查 deadline → 100% CPU 永不返回。→ 现在每条分支都 sleep+查超时。
4) 旁置残留（W-A2-7/M6，S3）：固定名 `.lock.stale` 被反复覆盖且无人删除。
   → 现在：唯一名 + 用完即删，并在 acquire/release 路径顺手清理同目录超龄
   （>24h）的 `*.lock.stale-*` / `*.tmp-*`（含 `.quarantine/`）。
"""

from __future__ import annotations

import contextlib
import errno
import getpass
import json
import logging
import os
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("backupstudio.locks")

LOCK_DIRNAME = ".backupstudio"
STALE_LEASE_SECONDS = 30 * 60  # 跨机（网络盘）租约上限；空锁/坏锁的超龄兜底
SIDECAR_MARK = ".lock.stale-"  # 旁置名：task-<id>.lock.stale-<pid>-<token>
SIDECAR_PREFIX = ".lock.stale"  # 匹配用：同时覆盖旧版固定名 task-<id>.lock.stale
TMP_MARK = ".tmp-"  # 发布前临时名：task-<id>.lock.tmp-<pid>-<token>
QUARANTINE_DIRNAME = ".quarantine"  # 放不回锁名的旁置文件归置处
SIDECAR_MAX_AGE_SECONDS = 24 * 3600  # 旁置/临时残留超龄清理阈值
TAKEOVER_GRACE_SECONDS = 0.5  # 锁龄小于此值先不旁置（避开刚建好的活锁窗口）
_BACKOFF_RETRY = 0.05  # 接管被别人抢先 → 快速重试
_BACKOFF_YOUNG = 0.1  # 陈旧锁太年轻（等它过接管闸门）
_BACKOFF_WAIT = 0.5  # 别人正常持锁 → 常规等待
_FREE_RETRY_LIMIT = 3  # 锁名刚空出来时最多立刻重抢几次（有界，避免与 deadline 抢跑）
_RESTORE_RETRIES = 3  # 误旁置的活锁放回重试次数

_LINK_DISABLED: set[str] = set()  # 不支持 os.link 的锁目录（退回 O_EXCL 直写）


class LockError(RuntimeError):
    pass


class LockTimeout(LockError):
    pass


@dataclass
class LockInfo:
    pid: int
    host: str
    user: str
    task_id: str
    started_at: float
    token: str = field(default="")

    def key(self) -> tuple:
        """同一把锁的身份指纹（接管复核用）。旧版本没有 token，退化为三元组。"""
        return (self.pid, self.host, round(self.started_at, 3), self.token)


def _lock_path(directory: Path, task_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id) or "task"
    return Path(directory) / LOCK_DIRNAME / f"task-{safe}.lock"


def _read_info(path: Path) -> LockInfo | None:
    """读锁内容；空文件/半截 JSON/顶层非 dict/坏字段一律返回 None（绝不抛）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):  # 123 / true / null / [1, 2] —— W-A2-6 的崩溃点
        return None
    try:
        return LockInfo(
            pid=int(data.get("pid", -1)),
            host=str(data.get("host", "")),
            user=str(data.get("user", "")),
            task_id=str(data.get("task_id", "")),
            started_at=float(data.get("started_at", 0.0)),
            token=str(data.get("token", "")),
        )
    except (TypeError, ValueError):
        return None


def _mtime(path: Path) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _age(path: Path) -> float | None:
    """锁文件存在时长（秒，按 mtime；用本机时钟，不受写入方 started_at 偏差影响）。"""
    mt = _mtime(path)
    if mt is None:
        return None
    return max(0.0, time.time() - mt)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore[import-not-found]

        return psutil.pid_exists(pid)
    except ImportError:  # pragma: no cover
        try:
            os.kill(pid, 0)  # Windows 上对死进程不一定抛，故优先 psutil
            return True
        except OSError:
            return False


def _is_stale(info: LockInfo) -> bool:
    """持有者已死（同机 pid 不在）或超租约（跨机场景）。"""
    mine = socket.gethostname()
    if info.host and info.host != mine:
        return (time.time() - info.started_at) > STALE_LEASE_SECONDS
    return not _pid_alive(info.pid)


def invalid_lock_stale(path: Path) -> bool:
    """内容无效（空/半截/坏 JSON）的锁是否可回收：按 mtime 超租约（W-A2-6 兜底）。"""
    age = _age(path)
    return age is not None and age > STALE_LEASE_SECONDS


class DirLock:
    """任务级文件锁（上下文管理器）。"""

    def __init__(self, directory: Path, task_id: str, *, timeout: float = 0.0, on_wait=None) -> None:
        self.directory = Path(directory)
        self.task_id = task_id
        self.timeout = timeout
        self.on_wait = on_wait
        self.path = _lock_path(self.directory, task_id)
        self._held = False
        self._token = ""
        self._rename_warned = False  # 旁置失败只警告一次（避免长 timeout 刷日志）

    # ------------------------------------------------------------------
    def acquire(self) -> "DirLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, self.timeout)
        # 崩溃残留的陈旧锁可能"还在接管闸门内"，给一段与 timeout 无关的微等待，
        # 让 timeout=0 的调用方（引擎 / daemon 守卫锁）也能完成崩溃自愈。
        grace_deadline = time.monotonic() + TAKEOVER_GRACE_SECONDS + 0.2
        backoff = _BACKOFF_WAIT
        limit = deadline
        free_retries = _FREE_RETRY_LIMIT
        while True:
            if self._try_acquire():
                if self._verify_ownership():
                    self._held = True
                    _sweep(self.path.parent, self.path)
                    return self
                # 复核失败：锁名上的不是我刚发布的那把 → 放手重回竞争
                log.warning("锁身份复核未通过，重新抢占：%s", self.path)
                self._drop_if_own()
                backoff, limit = _BACKOFF_RETRY, deadline
            else:
                info = _read_info(self.path)
                limit = deadline
                if _age(self.path) is None and free_retries > 0:
                    # 锁名刚好被释放（在我们失败的瞬间）：立刻重抢，有界，不会空转
                    free_retries -= 1
                    continue
                free_retries = _FREE_RETRY_LIMIT
                if self._adoptable(info):
                    self._held = True  # 锁名上就是我本次发布的那把（被误旁置后放回）
                    return self
                elif info is not None and self._is_own(info):
                    backoff = _BACKOFF_YOUNG  # 本进程别处仍持锁，等它释放
                elif info is not None and not _is_stale(info):
                    self._notify_wait(_owner_label(info))  # 活锁：常规等待对方结束
                    backoff = _BACKOFF_WAIT
                elif self._take_over():
                    continue  # 锁名已腾出，立刻重抢
                elif info is None:
                    backoff = _BACKOFF_WAIT  # 空锁/坏锁未超租约：等待（超龄才接管）
                else:
                    # 陈旧但太年轻：最多等到过接管闸门，之后由 timeout 判定
                    limit = max(deadline, grace_deadline)
                    backoff = _BACKOFF_YOUNG
            if time.monotonic() >= limit:
                raise LockTimeout(self._timeout_message(_read_info(self.path)))
            time.sleep(backoff)

    # ------------------------------------------------------------------
    def _notify_wait(self, who: str) -> None:
        if self.on_wait is None:
            return
        with contextlib.suppress(Exception):
            self.on_wait(who)

    def _is_own(self, info: LockInfo) -> bool:
        return info.pid == os.getpid() and (not info.host or info.host == socket.gethostname())

    def _adoptable(self, info: LockInfo | None) -> bool:
        """锁名上是"我本次刚发布的那把"（token 全等）——例如被别人的陈旧判定
        误旁置后又放了回来。直接认定持有，避免"发布成功→复核扑空→重抢失败→
        再复核扑空"的活锁自旋。"""
        return (
            info is not None
            and self._token != ""
            and info.token == self._token
            and self._is_own(info)
        )

    def _verify_ownership(self) -> bool:
        """发布成功后读回锁文件，确认 pid/host/token 都是自己（W-A2-5 的身份复核）。"""
        return self._adoptable(_read_info(self.path))

    def _timeout_message(self, info: LockInfo | None) -> str:
        if info is not None:
            return f"任务正在被另一个进程执行：{_owner_label(info)}"
        age = _age(self.path)
        if age is None:
            return "无法读取锁文件内容，且锁目录不可用（磁盘或权限故障）"
        if age > STALE_LEASE_SECONDS:
            return f"任务正在被另一个进程执行：锁内容无效且已超租约（存在 {age / 60:.0f} 分钟），接管失败"
        return (
            f"任务正在被另一个进程执行：锁内容无效（疑似崩溃残留的 0 字节/半截锁，"
            f"可能是文件系统故障），已存在 {age / 60:.0f} 分钟；"
            f"超过租约 {STALE_LEASE_SECONDS // 60} 分钟后可自动接管"
        )

    def _take_over(self) -> bool:
        """把陈旧锁改名旁置。True＝锁名已腾出（可重抢）；False＝让别人先走一步。

        四层防护（W-A2-5；与 acquire() 成功后的身份复核共同构成五道闸门）：
        1. 改名**前**就地复读内容与 mtime——陈旧判定必须以"此刻"成立，不能沿用
           一个循环之前的旧观察（旧实现正是这一点让输者把赢者的活锁偷走）；
        2. 接管年龄闸门：锁龄小于 TAKEOVER_GRACE_SECONDS 不旁置，覆盖"持有者 pid
           恰好判不出活"的新锁；
        3. 改名目标唯一化（pid+随机 token），失败即 return False 回循环重判，
           绝不吞异常后照抢；
        4. 改名**后**复核内容指纹全等（token 每次发布随机生成）才删除；对不上
           说明偷到别人的活锁 → 原样放回锁名。
        """
        mt_before = _mtime(self.path)
        if mt_before is None:
            return False  # 锁名已空 → 回循环直接重抢
        age = max(0.0, time.time() - mt_before)
        judged = _read_info(self.path)  # 以"此刻这一把"为准
        if judged is not None and not _is_stale(judged):
            return False  # 已经是别人的活锁：不动它
        if judged is None:
            if age <= STALE_LEASE_SECONDS:
                return False  # 空锁/坏锁还没超租约：不碰，也就不会反复改名空转
        elif age < TAKEOVER_GRACE_SECONDS:
            return False  # 太年轻：可能是别人刚建好的锁（其 pid 恰好不可判活）

        sidecar = self.path.with_name(f"{self.path.name}{SIDECAR_MARK}{os.getpid()}-{_token()}")
        try:
            os.rename(str(self.path), str(sidecar))
        except FileNotFoundError:
            return False  # 已被别人接管 → 回循环重判（旧实现吞异常后照抢＝双持有根因）
        except OSError as exc:
            # 只读/被占用等（W-A2-8 的空转来源）：记日志后交给上层退避 + deadline。
            # 同一 DirLock 只警告一次，避免长 timeout 下按退避频率刷日志。
            if self._rename_warned:
                log.debug("旁置陈旧锁仍失败（%s）：%s", exc, self.path)
            else:
                self._rename_warned = True
                log.warning("旁置陈旧锁失败（%s）：%s", exc, self.path)
            _sweep(self.path.parent, self.path)
            return False
        try:
            moved = _read_info(sidecar)
            if moved is None:
                keep = invalid_lock_stale(sidecar)
            elif judged is None:
                keep = False  # 我判定时内容无效、现在却有有效内容 → 是别人的新锁
            else:
                # 指纹（pid+host+started_at+随机 token）全等才是"我看的那把"；
                # token 随机且每次发布新生成，误判成别人的活锁在实践上不可能
                keep = moved.key() == judged.key() and _is_stale(moved)
            if keep:
                _sweep(self.path.parent, self.path)
                return True
            # 偷到了别人的活锁（或看错了对象）→ 原样放回，绝不据为己有
            if self._restore(sidecar):
                return False
            log.warning("活锁旁置后放回失败，已隔离：%s", sidecar.name)
            _park(self.path.parent, sidecar)
            return False
        finally:
            with contextlib.suppress(OSError):
                os.unlink(sidecar)

    def _restore(self, sidecar: Path) -> bool:
        """把误旁置的活锁按原内容放回锁名。

        放回成功、或锁名上已经是同一把锁（别人也发现了并放回）都算成功。
        锁名被**另一把**锁占用时重试几次仍不成则放弃（调用方会隔离旁置文件）。
        """
        try:
            text = sidecar.read_text(encoding="utf-8")
        except OSError:
            return False
        if not text.strip():
            return False
        judged = _read_info(sidecar)
        for attempt in range(_RESTORE_RETRIES):
            try:
                if _publish(text, self.path, self.path.parent):
                    return True
            except LockError as exc:  # 放回时目录/权限抖动：按失败重试，不冒泡
                log.debug("放回活锁失败（第 %d 次）：%s", attempt + 1, exc)
            current = _read_info(self.path)
            if judged is not None and current is not None and current.key() == judged.key():
                return True  # 已经有人在放了，锁名上的就是这一把
            time.sleep(0.01 * (attempt + 1))
        return False

    def _drop_if_own(self) -> None:
        """只删除属于自己本次发布的锁，不碰别人的。"""
        info = _read_info(self.path)
        if info is not None and self._is_own(info) and info.token == self._token:
            with contextlib.suppress(OSError):
                self.path.unlink()
        self._held = False

    def _try_acquire(self) -> bool:
        self._token = _token()
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "user": _user(),
            "task_id": self.task_id,
            "started_at": time.time(),
            "token": self._token,
        }
        return _publish(json.dumps(payload), self.path, self.path.parent)

    def release(self) -> None:
        if not self._held:
            return
        # 身份三要素全等（pid+host+token）才删：旧版本残留的无 token 锁不会被误删，
        # 被别人接管的锁也不会被我们释放。
        if self._adoptable(_read_info(self.path)):
            with contextlib.suppress(OSError):
                self.path.unlink()
        self._held = False
        _sweep(self.path.parent, self.path)

    # ------------------------------------------------------------------
    def __enter__(self) -> "DirLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.release()


# ----------------------------------------------------------------------
# 原子发布：写临时文件 → os.link 到目标（"存在即失败"，且"存在即完整"）
# ----------------------------------------------------------------------
def _token() -> str:
    return secrets.token_hex(4)


def _publish(text: str, target: Path, directory: Path) -> bool:
    """把 text 原子地发布成 target。True=发布成功，False=目标已存在。

    先写 `.tmp-<pid>-<rand>` 再 `os.link` 挂到 target：link 是原子的且目标存在
    即失败，因此锁名一旦出现就一定内容完整（修 W-A2-6 的半截锁）。平台不支持
    硬链接（exFAT/FAT、部分网络盘）时退回 O_EXCL 直写——仍保持独占语义，残留的
    "创建后写入前"窗口由 mtime 超租约兜底自愈。
    """
    tmp = target.with_name(f"{target.name}{TMP_MARK}{os.getpid()}-{_token()}")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        try:
            if str(directory) not in _LINK_DISABLED:
                try:
                    os.link(str(tmp), str(target))
                    return True
                except FileExistsError:
                    return False
                except OSError as exc:
                    _note_link_failure(directory, exc)
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(target)
                raise
            return True
        except FileExistsError:
            return False
        except OSError as exc:
            raise LockError(f"无法创建锁文件 {target}：{exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _note_link_failure(directory: Path, exc: OSError) -> None:
    """硬链接被文件系统永久拒绝（exFAT/FAT/部分网络盘）→ 该目录退回 O_EXCL 直写。"""
    permanent = {errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP, errno.EXDEV, errno.EACCES, errno.EINVAL}
    if getattr(exc, "errno", None) in permanent:
        _LINK_DISABLED.add(str(directory))
    log.debug("os.link 在 %s 不可用（%s），退回 O_EXCL 直写", directory, exc)


# ----------------------------------------------------------------------
# 旁置/临时残留清理（W-A2-7 / M6）
# ----------------------------------------------------------------------
def _leftover(name: str, keep: str | None = None) -> bool:
    if keep is not None and name == keep:
        return False
    # 唯一名旁置（.lock.stale-<pid>-<rand>）、旧版固定名旁置（.lock.stale）、
    # 以及发布用的临时文件（.tmp-<pid>-<rand>）
    return SIDECAR_PREFIX in name or TMP_MARK in name


def _sweep(directory: Path, keep: Path | None = None) -> None:
    """删除同目录超龄（>24h）的旁置/临时残留；目录类的挪进 .quarantine/。"""
    cutoff = time.time() - SIDECAR_MAX_AGE_SECONDS
    for path in _aged_leftovers(directory, keep.name if keep is not None else None, cutoff):
        with contextlib.suppress(OSError):
            if path.is_dir():
                _park(directory, path)
            else:
                path.unlink()
    qdir = directory / QUARANTINE_DIRNAME
    if qdir.is_dir():
        for path in _aged_leftovers(qdir, None, cutoff):
            with contextlib.suppress(OSError):
                path.unlink()


def _aged_leftovers(directory: Path, keep_name: str | None, cutoff: float) -> list[Path]:
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    out: list[Path] = []
    for entry in entries:
        if not _leftover(entry.name, keep_name):
            continue
        try:
            if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                continue  # 还不老：别人可能正在用
        except OSError:
            continue
        out.append(Path(entry.path))
    return out


def _park(directory: Path, orphan: Path) -> None:
    """放不回锁名、又删不掉的旁置文件挪进 .quarantine/，等超龄清理。"""
    qdir = directory / QUARANTINE_DIRNAME
    try:
        qdir.mkdir(parents=True, exist_ok=True)
        os.rename(str(orphan), str(qdir / orphan.name))
    except OSError:
        pass


def _owner_label(info: LockInfo | None) -> str:
    if info is None:
        return "未知进程（锁内容无效）"
    return f"{info.user}@{info.host} (pid {info.pid})"


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


def acquire_dir_lock(directory: Path | str, task_id: str, *, timeout: float = 0.0, on_wait=None) -> DirLock:
    """便捷函数：获取任务目录锁（失败抛 LockTimeout）。"""
    return DirLock(Path(directory), task_id, timeout=timeout, on_wait=on_wait).acquire()
