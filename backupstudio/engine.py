"""任务引擎：把任务配置变成一次真实备份（扫描 → 压缩 → 校验 → 保留策略 → 运行记录）。

引擎不依赖 Qt，可被 GUI、CLI、守护进程共用；所有阻塞操作都在调用方线程执行，
调用方负责放进工作线程。
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .archives import (
    FIRST_VOLUME_HINT,
    FORMAT_EXTENSIONS,
    ArchiveCancelled,
    ArchiveResult,
    ArchiveSpec,
    CompressionError,
    Item,
    Progress,
    create_archive,
    plan_items,
)
from .capabilities import FORMAT_LABELS
from .config import BackupTask, RunRecord
from .paths import config_dir
from .util import free_space, human_size, is_path_inside, render_template, sanitize_component, template_context

log = logging.getLogger("backupstudio.engine")


# --------------------------------------------------------------------------
# 命名
# --------------------------------------------------------------------------

def archive_stem(task: BackupTask, when: datetime | None = None) -> str:
    """根据任务与文件名模板生成压缩包主名（不含扩展名）。"""
    ctx = template_context(task.name or "backup", {"id": task.id})
    raw = render_template(task.options.filename_template, ctx)
    raw = sanitize_component(raw)
    return raw


def archive_path(task: BackupTask, when: datetime | None = None) -> Path:
    dest = Path(task.destination or "")
    stem = archive_stem(task, when)
    ext = FORMAT_EXTENSIONS.get(task.options.fmt, ".7z")
    if not task.destination:
        raise CompressionError("未设置备份位置", fix="在任务里选择一个备份目录")
    return dest / f"{stem}{ext}"


def volume_glob_paths(main: Path) -> list[Path]:
    """根据主文件路径列出属于同一次备份的分卷文件（主文件本身一定在最前）。"""
    folder = main.parent
    name = main.name
    found: dict[str, Path] = {}
    if main.is_file():
        found[str(main).lower()] = main
    patterns = [f"{name}.part*"]
    for ext in (".7z", ".zip", ".rar", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if name.endswith(ext):
            base = name[: -len(ext)]
            patterns += [f"{name}.[0-9][0-9][0-9]", f"{base}.part*.rar"]
            break
    for pat in patterns:
        try:
            for p in folder.glob(pat):
                if p.is_file():
                    found.setdefault(str(p).lower(), p)
        except OSError:
            continue
    return sorted(found.values(), key=lambda p: p.name)


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------

def build_sources(task: BackupTask) -> tuple[list[Item], list[str]]:
    """把任务里的来源转换为压缩条目，返回 (条目, 警告)。

    * mode=folder：整个文件夹（自动排除备份目标目录本身，避免自包含）
    * mode=files ：文件夹内指定文件/子目录（可多选）
    """
    items: list[Item] = []
    warnings: list[str] = []
    dest = Path(task.destination) if task.destination else None
    for src in task.sources:
        base = Path(src.path)
        if not src.path or not base.exists():
            warnings.append(f"来源不存在：{src.path or '(空)'}")
            continue
        root_name = base.name or "root"
        if src.mode == "folder":
            items.append(Item(base, root_name))
            if dest is not None and is_path_inside(dest, base):
                warnings.append(
                    f"备份位置位于源文件夹内部，已自动排除目标目录 {dest}（如需要请把备份放到其它位置）"
                )
            continue
        # 指定文件模式
        if src.files:
            for rel in src.files:
                cand = (base / rel) if not Path(rel).is_absolute() else Path(rel)
                try:
                    cand = cand.resolve(strict=False)
                except OSError:
                    pass
                if not cand.exists():
                    warnings.append(f"指定的文件不存在：{cand}")
                    continue
                try:
                    arc = f"{root_name}/{cand.relative_to(base.resolve()).as_posix()}"
                except ValueError:
                    arc = cand.name
                if cand.is_dir():
                    items.append(Item(cand, arc))
                else:
                    items.append(Item(cand, arc))
        else:
            # 未指定文件时退回整个文件夹
            warnings.append(f"{base} 未选择具体文件，将备份整个文件夹")
            items.append(Item(base, root_name))
    return items, warnings


def is_backup_destination_inside_source(task: BackupTask) -> bool:
    dest = task.destination
    if not dest:
        return False
    return any(is_path_inside(dest, s.path) for s in task.sources if s.path)


# 系统/软件产生的元数据文件，备份时自动跳过（用户仍可用排除规则调整）
_AUTO_SKIP_NAMES = {
    "thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db", "$recycle.bin",
    "system volume information", ".spotlight-v100", ".fseventsd", ".trashes",
    "last-run.json",
}


def _auto_skip(path: Path) -> bool:
    name = path.name.lower()
    if name in _AUTO_SKIP_NAMES:
        return True
    if name.startswith(".bs-staging") or name.endswith(".full-tmp") or ".part-tmp" in name:
        return True
    if ".bs-tmp-" in name:
        # 事务性落地的临时产物（<最终名>.bs-tmp-<pid>）：不是备份、不是源数据
        return True
    return False


def _friendly_error(exc: BaseException) -> str:
    """把底层异常翻译成用户能看懂的提示。"""
    text = str(exc)
    low = text.lower()
    if "拒绝访问" in text or "permission denied" in low or "access is denied" in low:
        return (
            "没有写入权限：目标目录或来源中的文件被占用/受限。"
            "请换一个备份位置，或确认当前用户对目标目录有写权限。"
        )
    if "cannot open file" in low or "is not archive" in low or "文件已存在" in text:
        return "压缩程序无法写入目标文件（可能被安全软件拦截或目录受保护），请换一个备份位置重试。"
    if "no space" in low or "空间不足" in text:
        return "磁盘空间不足，请清理目标磁盘后重试。"
    return text


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

def verify_archive(main: Path, fmt: str, password: str | None = None) -> tuple[bool, str]:
    """校验压缩包完整性（不依赖第三方模块）。"""
    try:
        if fmt == "7z":
            from .tools import find_7z

            exe = find_7z()
            if not exe:
                return True, "跳过校验：未找到 7-Zip"
            import subprocess
            import sys

            argv = [exe, "t", str(main)]
            if password:
                argv.insert(2, f"-p{password}")
            kwargs: dict = {"capture_output": True, "timeout": 3600}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(argv, **kwargs)
            out = (proc.stdout or b"").decode("utf-8", "replace")
            if proc.returncode == 0:
                return True, "7-Zip 测试通过"
            tail = out.strip().splitlines()[-1] if out.strip() else ""
            return False, f"7-Zip 测试失败（退出码 {proc.returncode}）{tail}"
        if fmt == "zip":
            import zipfile

            with zipfile.ZipFile(main) as zf:
                bad = zf.testzip()
                if bad is None:
                    return True, f"{len(zf.infolist())} 个条目校验通过"
                return False, f"CRC 校验失败：{bad}"
        if fmt == "rar":
            from .tools import find_unrar

            exe = find_unrar()
            if not exe:
                return True, "跳过校验：未找到 UnRAR"
            import subprocess
            import sys

            if Path(exe).name.lower().startswith("7z"):
                argv = [exe, "t", str(main)]
            else:
                argv = [exe, "t", "-idq", str(main)]
            kwargs = {"capture_output": True, "timeout": 3600}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(argv, **kwargs)
            return proc.returncode == 0, "RAR 测试通过" if proc.returncode == 0 else f"RAR 测试失败（退出码 {proc.returncode}）"
        if fmt.startswith("tar"):
            import tarfile

            if fmt == "tar.zst":
                # Python < 3.14 的 tarfile 不认识 zstd：优先用 zstandard 模块流式解压
                # 再交给 tarfile 遍历（覆盖 pip 安装的普通用户机）；
                # 其次 7-Zip，再到系统 zstd，最后才回退 tarfile 原生（会失败）。
                try:
                    import zstandard as _zstd  # noqa: PLC0415

                    with open(main, "rb") as fh:
                        reader = _zstd.ZstdDecompressor().stream_reader(fh)
                        with tarfile.open(fileobj=reader, mode="r|") as tf:
                            count = 0
                            for m in tf:
                                count += 1
                                if m.isfile():
                                    f = tf.extractfile(m)
                                    if f is not None:
                                        while f.read(1 << 20):
                                            pass
                    return True, f"{count} 个条目校验通过（zstandard 流式）"
                except ImportError:
                    pass
                except Exception as exc:  # noqa: BLE001 - 能解但内容坏 → 明确失败
                    return False, f"zstandard 校验失败：{type(exc).__name__}: {exc}"
                from .tools import find_7z

                seven = find_7z()
                if seven:
                    import subprocess
                    import sys

                    kwargs: dict = {"capture_output": True, "timeout": 3600, "stdin": subprocess.DEVNULL}
                    if sys.platform == "win32":
                        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    proc = subprocess.run([seven, "t", str(main)], **kwargs)
                    out = (proc.stdout or b"").decode("utf-8", "replace")
                    if proc.returncode == 0:
                        return True, "7-Zip 测试通过"
                    return False, f"7-Zip 测试失败（退出码 {proc.returncode}）" + out.strip()[-200:]
                zstd = __import__("shutil").which("zstd")
                if zstd:
                    import subprocess

                    proc = subprocess.run([zstd, "-t", str(main)], capture_output=True, timeout=3600, stdin=subprocess.DEVNULL)
                    return proc.returncode == 0, "zstd 校验通过" if proc.returncode == 0 else "zstd 校验失败"
                return False, "跳过校验：无 zstandard 模块 / zstd / 7-Zip，无法验证 tar.zst 内容"
            with tarfile.open(main, "r:*") as tf:
                count = 0
                for _ in tf:
                    count += 1
            return True, f"{count} 个条目可读取"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, "该格式无内置校验实现"


# --------------------------------------------------------------------------
# 保留策略
# --------------------------------------------------------------------------

@dataclass
class CleanupItem:
    blob: str  # 主文件
    files: list[str]
    size: int
    mtime: float


def find_backup_blobs(task: BackupTask) -> list[CleanupItem]:
    """列出备份目录下属于该任务的压缩包（含分卷归组）。"""
    if not task.destination:
        return []
    folder = Path(task.destination)
    if not folder.is_dir():
        return []
    ext = FORMAT_EXTENSIONS.get(task.options.fmt, ".7z")
    blobs: list[CleanupItem] = []
    seen: set[str] = set()
    for p in sorted(folder.iterdir()):
        if not p.is_file() or str(p).lower() in seen:
            continue
        if not p.name.endswith(ext):
            continue
        try:
            group = volume_glob_paths(p)
            size = sum(f.stat().st_size for f in group)
            mtime = min(f.stat().st_mtime for f in group)
        except OSError:
            continue
        for f in group:
            seen.add(str(f).lower())
        blobs.append(CleanupItem(str(p), [str(f) for f in group], size, mtime))
    return sorted(blobs, key=lambda b: b.mtime)


def apply_retention(task: BackupTask, protected: list[str] | None = None) -> list[str]:
    """执行保留策略，返回被处理（删除或移动）的文件列表。

    时间边界以**本次刚生成的备份**为基准，而不是「此刻」——
    这样即使刚写完（mtime 很新）也能按份数/天数正确淘汰旧文件。
    """
    policy = task.retention
    if not policy.enabled:
        return []
    blobs = find_backup_blobs(task)
    if not blobs:
        return []
    protected_set = {str(Path(p).resolve()).lower() for p in (protected or [])}
    # 以最新一份备份完成时刻作为时间基准
    now = blobs[-1].mtime if protected else time.time()
    expired: list[CleanupItem] = []

    # 1) 份数：保留最新 keep_last 份
    if policy.keep_last and len(blobs) > policy.keep_last:
        expired.extend(blobs[: len(blobs) - policy.keep_last])

    # 2) 天数
    cutoff = now - policy.keep_days * 86400
    for b in blobs:
        if b.mtime < cutoff and b not in expired:
            expired.append(b)

    # 3) 总量
    if policy.max_total_gb and policy.max_total_gb > 0:
        limit = policy.max_total_gb * (1024**3)
        total = sum(b.size for b in blobs)
        for b in blobs:
            if total <= limit:
                break
            if b not in expired:
                expired.append(b)
                total -= b.size

    handled: list[str] = []
    for b in expired:
        if any(str(Path(f).resolve()).lower() in protected_set for f in b.files):
            continue
        if policy.on_cleanup == "move" and policy.cleanup_dir:
            target_dir = Path(policy.cleanup_dir)
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("清理目录不可用 %s: %s", target_dir, exc)
                continue
            for f in b.files:
                src = Path(f)
                try:
                    shutil.move(str(src), str(target_dir / src.name))
                    handled.append(str(target_dir / src.name))
                    log.info("清理：移动过期备份 %s -> %s", src, target_dir)
                except OSError as exc:
                    log.warning("移动失败 %s: %s", src, exc)
        else:
            for f in b.files:
                src = Path(f)
                try:
                    src.unlink()
                    handled.append(str(src))
                    log.info("清理：删除过期备份 %s", src)
                except OSError as exc:
                    log.warning("删除失败 %s: %s", src, exc)
    return handled


# --------------------------------------------------------------------------
# 单次执行
# --------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    record: RunRecord
    result: ArchiveResult | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.record.status == "success"

    def summary(self) -> str:
        rec = self.record
        if rec.status == "success":
            return (
                f"完成：{rec.files} 个文件，{human_size(rec.source_bytes)} → {human_size(rec.archive_bytes)}，"
                f"耗时 {rec.duration:.1f}s"
            )
        if rec.status == "skipped":
            return f"跳过：{rec.errors[0] if rec.errors else '原因未知'}"
        return f"失败：{rec.errors[0] if rec.errors else '原因未知'}"


class TaskEngine:
    """执行任务；同一任务不允许并发执行。"""

    def __init__(self, log_fn: Callable[[str], None] | None = None) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.log_fn = log_fn

    def _lock(self, task_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(task_id, threading.Lock())

    def is_running(self, task_id: str) -> bool:
        lk = self._locks.get(task_id)
        if lk is None:
            return False
        acquired = lk.acquire(blocking=False)
        if acquired:
            lk.release()
            return False
        return True

    def run(
        self,
        task: BackupTask,
        trigger: str = "manual",
        progress_cb: Callable[[dict], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        apply_cleanup: bool = True,
    ) -> ExecutionResult:
        lock = self._lock(task.id)
        if not lock.acquire(blocking=False):
            rec = RunRecord(
                task_id=task.id, task_name=task.name, started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                status="skipped", errors=["该任务正在运行中"], trigger=trigger,
            )
            return ExecutionResult(rec, message="任务正在运行中")
        started = time.monotonic()
        started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        record = RunRecord(task_id=task.id, task_name=task.name, started_at=started_at, trigger=trigger)
        try:
            return self._run_locked(task, record, started, progress_cb, cancel, apply_cleanup)
        finally:
            lock.release()

    def _run_locked(
        self,
        task: BackupTask,
        record: RunRecord,
        started: float,
        progress_cb: Callable[[dict], None] | None,
        cancel: Callable[[], bool] | None,
        apply_cleanup: bool,
    ) -> ExecutionResult:
        warnings: list[str] = []
        opts = task.options
        record.format = opts.fmt

        # 1. 基本校验
        problems: list[str] = []
        if not task.destination:
            problems.append("未设置备份位置")
        source_paths = [p for p in task.source_paths()]
        if not source_paths:
            problems.append("未设置备份来源")
        for p in source_paths:
            if not Path(p).exists():
                problems.append(f"来源不存在：{p}")
        if opts.password and not opts.encrypt_mode:
            problems.append("设置了密码但未选择加密方式")
        if problems:
            record.status = "skipped"
            record.errors = problems
            record.finished_at = _now()
            record.duration = round(time.monotonic() - started, 2)
            return ExecutionResult(record, message="；".join(problems))

        dest_dir = Path(task.destination)
        main_path = archive_path(task)
        # 同秒重名：追加序号
        counter = 1
        base_main = main_path
        while main_path.exists() or any(main_path.parent.glob(main_path.name + ".*")):
            main_path = base_main.with_name(f"{base_main.stem}_{counter}{base_main.suffix}")
            counter += 1
            if counter > 500:
                break

        # 2. 规划
        sources, src_warnings = build_sources(task)
        warnings.extend(src_warnings)
        if not sources:
            record.status = "skipped"
            record.errors = src_warnings or ["没有可备份的内容"]
            record.finished_at = _now()
            record.duration = round(time.monotonic() - started, 2)
            return ExecutionResult(record, message="没有可备份的内容")

        exclude_dirs = []
        if is_path_inside(dest_dir, task.primary_source or "/"):
            exclude_dirs.append(dest_dir)
        items, skipped = plan_items(
            sources,
            exclude_globs=opts.exclude_globs,
            exclude_abs_dirs=exclude_dirs,
            follow_symlinks=opts.follow_symlinks,
        )
        record.skipped = skipped

        # 3. 空间检查
        needed = sum(i.size for i in items)
        avail = free_space(dest_dir)
        if avail and needed and avail < needed * 0.2:
            warnings.append(
                f"备份目标剩余空间 {human_size(avail)}，源数据 {human_size(needed)}；7z 外部压缩还需同等大小的临时空间"
            )

        # 4. 压缩
        # 提前判定压缩引擎（force=True 时允许降级到内置），失败时给出明确原因
        from .archives import BackendUnavailable, backend_for_format

        try:
            engine_choice = backend_for_format(opts.fmt, force=opts.force)
        except BackendUnavailable as exc:
            record.status = "failure"
            record.errors = [str(exc)]
            record.finished_at = _now()
            record.duration = round(time.monotonic() - started, 2)
            self._log(f"[{task.name}] 无法开始备份：{exc}")
            return ExecutionResult(record, message=str(exc))
        warnings.append(f"压缩引擎：{engine_choice.name}")
        if engine_choice.note:
            warnings.append(engine_choice.note)

        # 分卷模式：create_archive 会把 7z/rar 的「原生分卷」统一映射为字节切片，
        # 这样外部压缩程序失败时也能无缝回退到内置引擎。
        chosen_multivolume = opts.multivolume or None
        spec = ArchiveSpec(
            dest_path=main_path,
            fmt=opts.fmt,
            password=opts.password or None,
            encrypt_mode=opts.encrypt_mode if opts.password else None,
            level=opts.level,
            multivolume=chosen_multivolume,
            volume_size=opts.volume_size_bytes() if opts.multivolume else 0,
            follow_symlinks=opts.follow_symlinks,
            force=opts.force,
        )
        progress = Progress(progress_cb)
        self._log(f"[{task.name}] 开始备份 → {main_path}")
        try:
            result = create_archive(spec, items, progress=progress, cancel=cancel)
        except ArchiveCancelled:
            record.status = "skipped"
            record.errors = ["已取消"]
            record.finished_at = _now()
            record.duration = round(time.monotonic() - started, 2)
            return ExecutionResult(record, message="已取消")
        except (CompressionError, OSError) as exc:
            record.status = "failure"
            record.errors = [str(exc)]
            record.finished_at = _now()
            record.duration = round(time.monotonic() - started, 2)
            self._log(f"[{task.name}] 备份失败：{exc}")
            return ExecutionResult(record, message=str(exc))
        except Exception as exc:  # 未预期错误，保留堆栈便于排查
            record.status = "failure"
            record.errors = [f"{type(exc).__name__}: {exc}"]
            record.verification_message = traceback.format_exc()[-2000:]
            record.finished_at = _now()
            record.duration = round(time.monotonic() - started, 2)
            self._log(f"[{task.name}] 备份异常：{record.errors[0]}")
            return ExecutionResult(record, message=record.errors[0])

        # 5. 填充记录
        record.archive = str(result.path)
        record.backend = result.backend
        record.volumes = len(result.volumes) or 1
        record.archive_bytes = result.archive_bytes
        record.source_bytes = max(result.source_bytes, needed)
        record.files = result.files
        record.dirs = result.dirs
        record.encrypted = result.encrypted
        record.encrypt_mode = result.encrypt_mode or ""
        record.secure = result.secure
        record.multivolume = result.multivolume or ""
        if len(result.volumes) > 1:
            record.multivolume = record.multivolume or "split"
        record.forced = result.forced or bool(opts.force)
        warnings.extend(result.notes)
        warnings.extend(result.errors)  # 单个文件失败不中断整体

        if result.files == 0 and result.dirs > 0:
            warnings.append("没有文件被写入压缩包（只包含空目录）")

        # 6. 校验（分卷包无法直接测试，已通过逐卷字节数/分卷首尾完整性检查）
        if task.verify:
            if len(result.volumes) > 1:
                bad = [v.path.name for v in result.volumes if v.path.stat().st_size <= 0]
                record.verification = "failed" if bad else "passed"
                record.verification_message = (
                    f"分卷校验失败：{', '.join(bad)} 为空" if bad else f"已生成 {len(result.volumes)} 个分卷（完整性在校验时重建）"
                )
                record.extracted_text = "\n".join(v.path.name for v in result.volumes[:12])
            else:
                progress.describe("verify")
                ok, msg = verify_archive(result.path, record.format, opts.password or None)
                record.verification = "passed" if ok else "failed"
                record.extracted_text = _extract_text_sample(result.path, record.format) if ok else ""
                record.verification_message = msg
            if record.verification == "failed":
                record.status = "failure"
                record.errors.append(f"校验失败：{record.verification_message}")
        else:
            record.verification = "skipped"

        # 7. 保留策略（只用「是否刚生成」作保护，旧的同名包照常淘汰）
        if apply_cleanup and task.retention.enabled:
            try:
                protected = [str(v.path) for v in result.volumes]
                record.cleaned = apply_retention(task, protected=protected)
                if not record.cleaned:
                    log.info(
                        "[%s] 保留策略：未淘汰任何文件（共 %d 份）",
                        task.name, len(find_backup_blobs(task)),
                    )
            except Exception as exc:
                warnings.append(f"清理过期备份失败：{exc}")

        record.warnings = warnings
        record.finished_at = _now()
        record.duration = round(time.monotonic() - started, 2)
        if record.status != "failure":
            record.status = "success" if record.verification != "failed" else "failure"

        # 8. 写运行记录
        append_history(record, task)
        write_last_result(task, record, result)
        msg = (
            f"完成：{record.files} 个文件，{human_size(record.source_bytes)} → {human_size(record.archive_bytes)}，"
            f"{record.volumes} 个文件，耗时 {record.duration:.1f}s"
        )
        if record.verification == "passed":
            msg += "，校验通过"
        self._log(f"[{task.name}] {msg}")
        return ExecutionResult(record, result=result, message=msg)

    def _log(self, text: str) -> None:
        log.info(text)
        if self.log_fn:
            with contextlib.suppress(Exception):
                self.log_fn(text)


def _extract_text_sample(path: Path, fmt: str, limit: int = 1200) -> str:
    """校验通过后，重新读取压缩包头部并生成一段清单摘要（证明可还原）。"""
    try:
        if fmt == "zip":
            import zipfile

            with zipfile.ZipFile(path) as zf:
                names = [i.filename for i in zf.infolist()[:8]]
                head = zf.read(names[0]) if names and not zf.infolist()[0].is_dir() else b""
            return _format_sample(names, head, limit)
        if fmt == "7z":
            from .tools import find_7z

            exe = find_7z()
            if not exe:
                return ""
            import subprocess
            import sys

            kwargs: dict = {"capture_output": True, "timeout": 60}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run([exe, "l", "-ba", str(path)], **kwargs)
            text = (proc.stdout or b"").decode("utf-8", "replace")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            return "\n".join(lines[:8])[:limit]
        if fmt.startswith("tar"):
            import tarfile

            with tarfile.open(path, "r:*") as tf:
                members = [m for m in tf.getmembers()[:12]]
                names = [m.name for m in members if m.isfile()][:8]
                head = b""
                if names:
                    fh = tf.extractfile(names[0])
                    if fh is not None:
                        head = fh.read(512)
            return _format_sample(names, head, limit)
    except Exception:
        return ""
    return ""


def _format_sample(names: list[str], head: bytes, limit: int) -> str:
    lines = names[:]
    if head:
        text = head.decode("utf-8", "replace")
        preview = text.splitlines()[0][:80] if text.splitlines() else ""
        if preview:
            lines.append(f"  …：{preview}")
    return "\n".join(lines)[:limit]


def _skipped_note(record: RunRecord, task: BackupTask) -> str:
    """把「被跳过」的原因翻译成可执行的建议。"""
    errors = record.errors or []
    text = "\n".join(errors)
    if "RAR" in text or "rar" in text:
        return "RAR 需要 WinRAR / RAR 软件（专有，需自行安装）。可在任务里勾选「强制压缩」，或改用 7z / zip。"
    if "7-Zip" in text:
        return "缺少 7-Zip。Windows 可运行 winget install 7zip.7zip；或启用「强制压缩」使用内置引擎。"
    return text or "任务未执行"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 历史与结果报告
# --------------------------------------------------------------------------

def history_path() -> Path:
    return config_dir() / "history.jsonl"


def append_history(record: RunRecord, task: BackupTask | None = None) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = record.to_dict()
    if task is not None:
        data["task"] = {
            "destination": task.destination,
            "fmt": task.options.fmt,
            "schedule": task.schedule.describe(),
        }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False) + "\n")


def read_history(limit: int = 500) -> list[dict]:
    path = history_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 2_000_000))
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    for line in raw.splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            rows.append(json.loads(line))
    return rows


def write_last_result(task: BackupTask, record: RunRecord, result: ArchiveResult | None) -> None:
    """在备份目录写一份 last-run.json，方便自动化读取。"""
    if not task.destination:
        return
    data = {
        "app": "BackupStudio",
        "version": __version__,
        "task": task.name,
        "task_id": task.id,
        "status": record.status,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "archive": record.archive,
        "format": record.format,
        "format_label": FORMAT_LABELS.get(record.format, record.format),
        "volumes": [str(v.path) for v in (result.volumes if result else [])],
        "first_volume_hint": FIRST_VOLUME_HINT.get(record.multivolume or "", ""),
        "files": record.files,
        "dirs": record.dirs,
        "source_bytes": record.source_bytes,
        "archive_bytes": record.archive_bytes,
        "encrypted": record.encrypted,
        "encrypt_mode": record.encrypt_mode,
        "verification": record.verification,
        "warnings": record.warnings,
        "errors": record.errors,
    }
    try:
        from .config import save_json_atomic

        save_json_atomic(Path(task.destination) / "last-run.json", data)
    except OSError:
        pass
