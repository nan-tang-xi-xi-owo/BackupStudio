"""把临时目录放到备份目标所在的磁盘上。

原因：
* 某些 Windows 受限环境（沙箱 / 低完整性 / 重定向的 %TEMP%）下，7z 子进程
  无法写入系统临时目录，会报 "cannot open file / 拒绝访问"；
* 同卷暂存还能避免把几十 GB 数据先写到系统盘再复制过去。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import time
from pathlib import Path


def same_volume(a: Path, b: Path) -> bool:
    try:
        return os.path.splitdrive(os.path.abspath(str(a)))[0].lower() == os.path.splitdrive(os.path.abspath(str(b)))[0].lower()
    except (OSError, ValueError):
        return False


def staging_parent(near: Path) -> Path:
    """为 ``near`` 选一个可写的暂存父目录。

    顺序：目标目录 → 目标目录的 ``.bs-staging`` 子目录 → 系统临时目录。
    """
    near = Path(near)
    bases: list[Path] = []
    if near.is_dir():
        bases.append(near)
    elif near.parent.is_dir():
        bases.append(near.parent)
    for base in bases:
        for probe in (base, base / ".bs-staging"):
            try:
                probe.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            if not os.access(str(probe), os.W_OK):
                continue
            try:
                with tempfile.NamedTemporaryFile(dir=str(probe), prefix=".w-", delete=True):
                    pass
            except OSError:
                continue
            return probe
    return Path(tempfile.gettempdir())


def make_staging_dir(near: Path, prefix: str = "bs-stage-") -> Path:
    parent = staging_parent(near)
    # 新建之前先清掉「上一轮被强杀」留下的残骸（用户测试 P0）。
    # 暂存目录里会有 staging.tar 以及**解包后的明文文件**；正常结束时 finally 会删，
    # 但进程被强杀（任务管理器 / 断电 / 合盖休眠）时 finally 不执行，
    # 明文就永久留在用户备份目录里 —— 加密形同虚设，还白占磁盘。
    sweep_stale(parent, prefix)
    try:
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
    except OSError:
        path = Path(tempfile.mkdtemp(prefix=prefix))
    mark_owner(path)
    return path


#: 判定「残留」的最小年龄（秒）。太短会误删同批次另一个任务正在用的目录。
_STALE_MIN_AGE = 60.0
#: 每个目录里放一个标记文件，写入当前 pid，用于判断属主是否还活着。
_OWNER_FILE = ".bs-owner"


def sweep_stale(parent: Path, prefix: str = "bs-stage-", *, min_age: float | None = None) -> list[Path]:
    """删除 ``parent`` 下属于**已退出进程**的 ``<prefix>*`` 目录。

    ``min_age`` 默认 60 秒，避免误删并发任务正在用的目录。
    返回被清掉的目录列表。

    判定逻辑（保守，宁可留着也不误删）：
      1. 目录名以 ``prefix`` 开头；
      2. 目录 mtime 早于 ``min_age`` 秒前；
      3. 若目录里有 ``.bs-owner`` 且写着 pid → 该 pid **已不存在**才删；
         没有 owner 文件（旧版本残留）→ 只按年龄判断。
    """
    parent = Path(parent)
    if not parent.is_dir():
        return []
    cutoff = time.time() - (_STALE_MIN_AGE if min_age is None else min_age)
    removed: list[Path] = []
    try:
        candidates = [p for p in parent.iterdir() if p.is_dir() and p.name.startswith(prefix)]
    except OSError:
        return []

    for cand in candidates:
        try:
            if cand.stat().st_mtime > cutoff:
                continue  # 太新，可能有人正在用
            owner = cand / _OWNER_FILE
            if owner.is_file():
                try:
                    pid = int(owner.read_text(encoding="ascii").strip() or "0")
                except (OSError, ValueError):
                    pid = 0
                if pid > 0 and _pid_alive(pid):
                    continue  # 属主还活着，别动
            force_rmtree(cand)
            if not cand.exists():
                removed.append(cand)
        except OSError:
            continue
    return removed


def force_rmtree(path: Path) -> bool:
    """删除目录树，**能处理只读文件**；返回是否删净。

    为什么不用 ``shutil.rmtree(..., ignore_errors=True)``（用户测试 P0）：

    Windows 上只读（R 属性）文件无法被删除，``rmtree`` 会抛 ``WinError 5``。
    加了 ``ignore_errors=True`` 就变成**静默失败**：暂存目录里的
    ``unpacked/`` 明文副本永久留在用户的备份目标目录里，而用户以为加密了。

    实测（科研/离线用户独立复现）：源里 1 个只读文件 → AES 加密备份成功，
    但目标目录留下 ``bs-stage-*/unpacked/.../salary.txt`` 明文，
    连跑 3 次残留从 2 个增到 4 个；引擎自己的清扫也报 ``WinError 5``。

    策略：先清只读属性再删；失败重试几次（杀软/索引器会短暂占用）。
    """
    path = Path(path)
    if not path.exists():
        return True

    def _on_error(func, target, _exc_info):  # noqa: ANN001
        # 遇到只读/权限问题：改权限后再试一次
        with contextlib.suppress(OSError):
            p = Path(target)
            if p.is_dir():
                os.chmod(p, 0o700)
            else:
                os.chmod(p, 0o600)
            func(target)

    for attempt in range(3):
        with contextlib.suppress(OSError):
            shutil.rmtree(path, onerror=_on_error)
        if not path.exists():
            return True
        # 再扫一遍：把所有文件的只读位清掉
        with contextlib.suppress(OSError):
            for p in path.rglob("*"):
                with contextlib.suppress(OSError):
                    os.chmod(p, 0o700 if p.is_dir() else 0o600)
        if attempt < 2:
            time.sleep(0.05 * (attempt + 1))
    return not path.exists()


def _pid_alive(pid: int) -> bool:
    """判断 pid 是否仍在运行（跨平台，失败时保守返回 True）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def mark_owner(path: Path) -> None:
    """在暂存目录里写下当前进程 pid，供 ``sweep_stale`` 判断属主是否存活。"""
    with contextlib.suppress(OSError):
        (Path(path) / _OWNER_FILE).write_text(str(os.getpid()), encoding="ascii")


@contextlib.contextmanager
def staging_dir(near: Path, prefix: str = "bs-stage-"):
    path = make_staging_dir(near, prefix)
    try:
        yield path
    finally:
        force_rmtree(path)
