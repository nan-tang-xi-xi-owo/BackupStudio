"""应用数据目录与设置存储（跨平台）。

设计原则：**任何目录创建失败都不能让程序静默退出**。
* 配置目录不可写时，退到「用户临时目录 / 可执行文件旁」，并记录原因；
* 运行期目录（锁、PID、IPC）永远有多个候选，最后一个候选必然可用；
* 所有失败都会写入 ``STARTUP_DIAGNOSTICS``，由界面/CLI 展示给用户。
"""

from __future__ import annotations

import getpass
import os
import sys
import tempfile
import time
from pathlib import Path

from . import APP_NAME, __version__

# 启动过程中的可恢复问题（供界面/CLI 展示）
STARTUP_DIAGNOSTICS: list[str] = []

# 已确定的目录（避免重复探测；探测失败会重试多次，很慢）
_DECIDED: dict[str, Path] = {}


def _note(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {message}"
    if line not in STARTUP_DIAGNOSTICS:
        STARTUP_DIAGNOSTICS.append(line)


def _mkdir(path: Path, *, what: str) -> bool:
    """创建目录，带 3 次重试（杀毒/索引器常会短暂占用）。"""
    if path.is_dir():
        return True
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except OSError as first:
        for delay in (0.15, 0.4, 0.8):
            time.sleep(delay)
            try:
                path.mkdir(parents=True, exist_ok=True)
                return path.is_dir()
            except OSError:
                continue
        _note(f"无法创建{what}目录 {path}：{type(first).__name__}: {first}")
        return False


def reset_decisions() -> None:
    """测试用：清空目录缓存与诊断信息。"""
    _DECIDED.clear()
    STARTUP_DIAGNOSTICS.clear()


def _appdata_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "backupstudio"


def _fallback_candidates() -> list[Path]:
    """配置目录不可写时的备选位置（按优先级）。

    注意：只有 ``LOCALAPPDATA`` 这类「稳定、下次还能找到」的位置才允许当配置目录；
    进程内唯一的临时目录只用于运行期文件，否则重启后就找不到自己的配置了。
    """
    candidates: list[Path] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / APP_NAME)
        candidates.append(Path(sys.executable).resolve().parent / "data")
        return candidates
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        candidates.append(Path(runtime) / "backupstudio")
    candidates.append(Path.home() / f".{APP_NAME.lower()}")
    return candidates


def _process_scoped_temp(prefix: str) -> Path:
    """进程内唯一的临时目录（不可用于持久配置）。"""
    base = Path(tempfile.gettempdir()) / f"{APP_NAME.lower()}-{getpass.getuser()}"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))


def app_data_dir() -> Path:
    """返回应用数据目录，并按需创建（带多层兜底）。

    结果会缓存：目录一旦确定就不再重复探测（失败重试很慢）。
    """
    cached = _DECIDED.get("config")
    if cached is not None and cached.is_dir():
        return cached
    override = os.environ.get("BACKUPSTUDIO_CONFIG_DIR")
    if override:
        target = Path(override)
        if _usable_dir(target, what="配置"):
            _DECIDED["config"] = target
            return target
        _note(f"BACKUPSTUDIO_CONFIG_DIR 指定的目录不可用，将使用备用位置：{target}")
    primary = _appdata_dir()
    if _usable_dir(primary, what="配置"):
        _DECIDED["config"] = primary
        return primary
    for cand in _fallback_candidates():
        if _usable_dir(cand, what="备用配置"):
            _note(f"配置已临时改存到 {cand}（原位置 {primary} 不可写）")
            _DECIDED["config"] = cand
            return cand
    # 最后手段：临时目录里**固定**的一个位置（同一用户下次还能找到）
    tmp = Path(tempfile.gettempdir()) / f"{APP_NAME}-{getpass.getuser()}" / "config"
    if _usable_dir(tmp, what="临时配置"):
        _note(f"所有配置目录都不可用，已临时改存到 {tmp}（原位置 {primary} 不可写）")
        _DECIDED["config"] = tmp
        return tmp
    tmp = Path(tempfile.mkdtemp(prefix=f"{APP_NAME}-"))
    _note(f"所有配置目录都不可用，已使用临时目录 {tmp}（退出后数据会丢失）")
    _DECIDED["config"] = tmp
    return tmp


def config_dir() -> Path:
    """存储配置的目录（可被 ``BACKUPSTUDIO_CONFIG_DIR`` 覆盖，便于测试）。"""
    return app_data_dir()


def log_dir() -> Path:
    d = config_dir() / "logs"
    return d if _mkdir(d, what="日志") else config_dir()


def cache_dir() -> Path:
    d = config_dir() / "cache"
    return d if _mkdir(d, what="缓存") else config_dir()


def runtime_dir() -> Path:
    """运行期临时文件（IPC 端点、锁、PID 等）。永远返回一个可写目录。"""
    cached = _DECIDED.get("runtime")
    if cached is not None and cached.is_dir() and os.access(str(cached), os.W_OK):
        return cached
    candidates: list[Path] = []
    if sys.platform != "win32":
        base = os.environ.get("XDG_RUNTIME_DIR")
        if base:
            candidates.append(Path(base) / "backupstudio")
    candidates.append(config_dir() / "run")
    # 固定位置（同一用户重启后还能找到 PID/IPC）优先于进程内唯一目录
    candidates.append(Path(tempfile.gettempdir()) / f"{APP_NAME.lower()}-{getpass.getuser()}" / "run")
    for cand in candidates:
        if _usable_dir(cand, what="运行"):
            _DECIDED["runtime"] = cand
            return cand
    # 兜底：进程内唯一的临时目录
    try:
        tmp = _process_scoped_temp(prefix="run-")
        _note(f"运行目录不可用，使用临时目录 {tmp}")
        _DECIDED["runtime"] = tmp
        return tmp
    except OSError:
        return Path(tempfile.gettempdir())


def _usable_dir(path: Path, *, what: str) -> bool:
    """目录可用 = 能创建（或已存在）**并且**确实能往里写文件。"""
    if not _mkdir(path, what=what):
        return False
    probe = path / f".bs-write-test-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return True
    except OSError as exc:
        _note(f"{what}目录不可写 {path}：{type(exc).__name__}: {exc}")
        # 目录存在但不可写：记一下，后续让调用方换位置
        return False


def pid_file_path() -> Path:
    return runtime_dir() / "daemon.pid"


def safe_file_write(path: Path, text: str, *, encoding: str = "utf-8") -> bool:
    """尽力写出一个小文件（失败只记录，不抛异常）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return True
    except OSError as exc:
        _note(f"写入 {path} 失败：{type(exc).__name__}: {exc}")
        return False


def safe_file_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def ipc_endpoint(port: int) -> str:
    """守护进程 IPC 地址：Windows 用本地端口，Unix 用文件套接字。"""
    if sys.platform == "win32":
        return f"tcp://127.0.0.1:{port}"
    sock = runtime_dir() / f"bs-{port}.sock"
    # Unix 域套接字路径长度上限 107/108 字节，过长时退化为 tcp
    if len(str(sock)) > 100:
        return f"tcp://127.0.0.1:{port}"
    return f"ipc://{sock}"


def state_file_path() -> Path:
    return config_dir() / "state.json"


def app_version() -> str:
    return __version__
