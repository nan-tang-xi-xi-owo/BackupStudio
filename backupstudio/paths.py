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

# 配置目录的「决定过程」：请求值 / 首选值 / 实际值 / 是否回退（审查 N2）。
# 只靠 STARTUP_DIAGNOSTICS 的文本去猜「是否回退过」既脆弱又容易被别处的
# _note() 混进来，所以另存一份结构化状态，供 selftest / 界面如实展示。
_CONFIG_META: dict[str, object] = {}


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
    _CONFIG_META.clear()


def config_dir_state() -> dict[str, object]:
    """返回配置目录的决定结果（不存在则先决定一次）。

    字段：
    * ``requested``：``BACKUPSTUDIO_CONFIG_DIR`` 的请求值（没有则为 ``None``）；
    * ``preferred``：正常情况下**应该**用的目录（即 ``_appdata_dir()``）；
    * ``actual``：实际生效的目录；
    * ``fell_back``：**是否回退到了非首选位置**（实际目录 != 首选目录）；
    * ``override_ignored``：是否**忽略了用户/环境指定的目录**（指定了但不可用）；
    * ``reason``：原因（人话，可直接展示）。
    """
    actual = app_data_dir()
    preferred = Path(str(_CONFIG_META.get("preferred") or _appdata_dir()))
    requested = _CONFIG_META.get("requested")
    fell_back = bool(_CONFIG_META.get("fell_back", actual != preferred))
    return {
        "requested": requested,
        "preferred": preferred,
        "actual": actual,
        "fell_back": fell_back,
        "override_ignored": bool(_CONFIG_META.get("override_ignored", False)),
        "reason": str(_CONFIG_META.get("reason") or ""),
    }


def _platform() -> str:
    """当前平台标识。

    单独抽一层是为了**可测**：直接 monkeypatch ``sys.platform`` 会污染整个进程
    （PyInstaller 的 ``compat`` 模块在 import 时就会读它并缓存），
    测试里只该替换这个函数。
    """
    return sys.platform


def _appdata_dir() -> Path:
    if _platform() == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if _platform() == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "backupstudio"


def _fallback_candidates() -> list[Path]:
    """配置目录不可写时的备选位置（按优先级）。

    注意：只有 ``LOCALAPPDATA`` 这类「稳定、下次还能找到」的位置才允许当配置目录；
    进程内唯一的临时目录只用于运行期文件，否则重启后就找不到自己的配置了。
    """
    candidates: list[Path] = []
    if _platform() == "win32":
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


def _record_choice(
    actual: Path,
    *,
    preferred: Path,
    requested: str | None,
    fell_back: bool,
    reason: str,
    override_ignored: bool = False,
) -> None:
    _CONFIG_META.update(
        {
            "preferred": preferred,
            "requested": requested,
            "actual": actual,
            "fell_back": fell_back,
            "override_ignored": override_ignored,
            "reason": reason,
        }
    )


def app_data_dir() -> Path:
    """返回应用数据目录，并按需创建（带多层兜底）。

    结果会缓存：目录一旦确定就不再重复探测（失败重试很慢）。

    N2 修复要点：每次决定都记录**首选目录**与**是否回退**，供 ``selftest``
    如实报告。旧实现的 selftest 只检查「最终目录可写」，而最终目录恰恰是
    回退出来的临时位置（Flatpak 里是 tmpfs）→ 明明每次重启都丢配置，
    自检却显示 ``[OK] 配置目录可写``。
    """
    cached = _DECIDED.get("config")
    if cached is not None and cached.is_dir():
        return cached
    preferred = _appdata_dir()
    override = os.environ.get("BACKUPSTUDIO_CONFIG_DIR")
    override_failed = ""
    if override:
        target = Path(override)
        if _usable_dir(target, what="配置"):
            # 显式指定且可用 = 用户的选择，**不是**回退（测试/便携场景都走这条）
            _record_choice(
                target, preferred=preferred, requested=override,
                fell_back=False, reason="",
            )
            _DECIDED["config"] = target
            return target
        override_failed = f"指定的配置目录 {target} 不可用（不可创建或不可写）"
        _note(f"BACKUPSTUDIO_CONFIG_DIR 指定的目录不可用，将使用备用位置：{target}")
    if _usable_dir(preferred, what="配置"):
        _record_choice(
            preferred, preferred=preferred, requested=override,
            fell_back=False,
            reason=override_failed,
            override_ignored=bool(override_failed),
        )
        _DECIDED["config"] = preferred
        return preferred
    for cand in _fallback_candidates():
        if _usable_dir(cand, what="备用配置"):
            _note(f"配置已临时改存到 {cand}（原位置 {preferred} 不可写）")
            reason = f"首选目录 {preferred} 不可写，已回退到 {cand}"
            _record_choice(
                cand, preferred=preferred, requested=override, fell_back=True,
                reason=f"{override_failed}；{reason}" if override_failed else reason,
                override_ignored=bool(override_failed),
            )
            _DECIDED["config"] = cand
            return cand
    # 最后手段：临时目录里**固定**的一个位置（同一用户下次还能找到）
    tmp = Path(tempfile.gettempdir()) / f"{APP_NAME}-{getpass.getuser()}" / "config"
    if _usable_dir(tmp, what="临时配置"):
        _note(f"所有配置目录都不可用，已临时改存到 {tmp}（原位置 {preferred} 不可写）")
        reason = f"所有配置目录都不可用，已回退到临时目录 {tmp}（重启后可能丢失）"
        _record_choice(
            tmp, preferred=preferred, requested=override, fell_back=True,
            reason=f"{override_failed}；{reason}" if override_failed else reason,
            override_ignored=bool(override_failed),
        )
        _DECIDED["config"] = tmp
        return tmp
    tmp = Path(tempfile.mkdtemp(prefix=f"{APP_NAME}-"))
    _note(f"所有配置目录都不可用，已使用临时目录 {tmp}（退出后数据会丢失）")
    reason = f"所有配置目录都不可用，已使用临时目录 {tmp}（退出后数据会丢失）"
    _record_choice(
        tmp, preferred=preferred, requested=override, fell_back=True,
        reason=f"{override_failed}；{reason}" if override_failed else reason,
        override_ignored=bool(override_failed),
    )
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
    if _platform() != "win32":
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
    if _platform() == "win32":
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
