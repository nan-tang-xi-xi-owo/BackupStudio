"""任务引擎：把任务配置变成一次真实备份（扫描 → 压缩 → 校验 → 保留策略 → 运行记录）。

引擎不依赖 Qt，可被 GUI、CLI、守护进程共用；所有阻塞操作都在调用方线程执行，
调用方负责放进工作线程。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
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
from .manifest import collect_groups
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
    if name == ".backupstudio" or name.startswith(".bs-staging") or name.endswith(".full-tmp") or ".part-tmp" in name:
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

def _verify_volumes(volumes, fmt: str, password: str | None = None) -> tuple[bool | None, str]:
    """分卷真实校验（审查 E-2）：逐卷字节拼接成临时整包后走 verify_archive。

    * 通用切片卷（.partNN）：字节拼接即还原原包（按卷序，数值排序）；
    * 7z 原生卷（.001 集）：7za 能直接 t 首卷（自动串联），优先走它；
    * 无法拼接/校验的形态 → 返回 None（skipped），不得误报 passed。
    """
    import tempfile

    paths = [Path(v.path) for v in volumes]
    if not paths:
        return False, "分卷清单为空"
    # 7z 原生分卷：首卷 .001，7za t 自动串联
    if fmt == "7z" and paths[0].name.endswith(".001"):
        return verify_archive(paths[0], "7z", password)
    # 通用切片：字节拼接
    try:
        with tempfile.TemporaryDirectory(prefix="bs-verify-") as td:
            joined = Path(td) / f"joined{FORMAT_EXTENSIONS.get(fmt, '.bin')}"
            with open(joined, "wb") as out:
                for p in paths:
                    with open(p, "rb") as fh:
                        shutil.copyfileobj(fh, out, 1 << 20)
            ok, msg = verify_archive(joined, fmt, password)
            return ok, (f"分卷拼接校验：{msg}" if ok is not None else f"分卷拼接后：{msg}")
    except OSError as exc:
        return None, f"分卷校验无法执行：{exc}"


# py7zr "读得出目录但解不开载荷"的异常：属于无法判定，不得报 failed（名字匹配，避免版本间类名漂移）
_PY7ZR_UNDECIDABLE = frozenset({"UnsupportedCompressionMethodError", "DecompressionError"})


def _verify_7z_py7zr(main: Path, password: str | None) -> tuple[bool | None, str]:
    """无 7-Zip 时用 py7zr 校验 7z 的**载荷**，返回三态（True / False / None）。

    A1：旧实现只调 ``getnames()``（只读头），载荷被翻转字节的 7z 也会被判 passed。
    这里改用 py7zr 1.x 的 ``testzip()``：读完所有条目并比对 CRC，返回 None 或出错的文件名。

    三态规则（None 绝不等于 passed）：
    * True  —— testzip() 返回 None：所有条目 CRC 全部匹配；
    * False —— 结构性损坏（Bad7zFile / CrcError），或给了密码但解不开；
    * None  —— 无法判定：没装 py7zr、py7zr 无 testzip()、归档加密但未提供密码、
               本机 py7zr 缺该压缩方法的解码器。
    """
    try:
        import py7zr
    except ImportError:
        return None, "跳过校验：未找到 7-Zip，且未安装 py7zr，无法校验 7z 载荷"
    if not hasattr(py7zr.SevenZipFile, "testzip"):
        return None, (
            "跳过校验：未找到 7-Zip，当前 py7zr "
            f"{getattr(py7zr, '__version__', '未知')} 没有 testzip()（需 py7zr>=1.0）"
        )
    try:
        with py7zr.SevenZipFile(str(main), "r", password=password) as z:
            bad = z.testzip()
    except Exception as exc:  # noqa: BLE001 - 按类型名分类，不依赖具体异常类的位置
        name = type(exc).__name__
        if name == "PasswordRequired":
            if password:
                return False, "校验失败：密码错误（py7zr 无法解开归档）"
            return None, "跳过校验：归档已加密，请提供密码后重试（本机无 7-Zip）"
        if name in ("Bad7zFile", "CrcError"):
            return False, f"7z 校验失败（{name}）：{exc}"
        if name in _PY7ZR_UNDECIDABLE:
            return None, f"跳过校验：本机 py7zr 无法解码该归档（{name}）"
        if password is None:
            # 无密码时无法区分"载荷损坏"与"载荷被加密"，不武断判失败
            return None, f"跳过校验：py7zr 无法判定（{name}）"
        return False, f"7z 校验失败（{name}）：{exc}"
    if bad is None:
        return True, "py7zr 载荷校验通过（testzip）"
    return False, f"py7zr 校验失败：条目 {bad} 的 CRC 不匹配"


def verify_archive(main: Path, fmt: str, password: str | None = None) -> tuple[bool | None, str]:
    """校验压缩包完整性。

    返回三态：True=校验通过；False=校验失败；**None=无法校验（skipped）**。
    None 绝不等于 passed——调用方必须把跳过的真值呈现给用户（审查 E-1）。
    """
    if not main.exists():
        return False, f"文件不存在：{main.name}"
    try:
        if fmt == "7z":
            from .tools import find_7z

            exe = find_7z()
            if not exe:
                # 无 7-Zip：交给 py7zr 做**载荷级**校验（审查 A1：只读头会把坏包判 passed）
                return _verify_7z_py7zr(main, password)
            import subprocess
            import sys

            argv = [exe, "t", str(main)]
            if password:
                argv.insert(2, f"-p{password}")
            kwargs: dict = {"capture_output": True, "timeout": 600, "stdin": subprocess.DEVNULL}
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
                infos = zf.infolist()
                encrypted = any(bool(i.flag_bits & 0x1) for i in infos if not i.is_dir())
                if encrypted:
                    # 加密条目无法用 zipfile 测试（读不了内容）：
                    # 有 7-Zip 时用 7z t 带密码测试；否则明确跳过（不得误报成败）。
                    from .tools import find_7z

                    seven = find_7z()
                    if seven:
                        import subprocess
                        import sys as _sys

                        argv = [seven, "t", str(main)]
                        if password:
                            argv.insert(2, f"-p{password}")
                        kwargs: dict = {"capture_output": True, "timeout": 600, "stdin": subprocess.DEVNULL}
                        if _sys.platform == "win32":
                            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        proc = subprocess.run(argv, **kwargs)
                        out7 = (proc.stdout or b"").decode("utf-8", "replace")
                        if proc.returncode == 0:
                            return True, "7-Zip 测试通过（加密 ZIP）"
                        if not password:
                            return False, "加密 ZIP 需要密码才能校验，请提供密码"
                        return False, f"加密 ZIP 校验失败（退出码 {proc.returncode}）{out7.strip()[-200:]}"
                    return None, "跳过内容校验：加密 ZIP 需 7-Zip（条目数与加密位已核对）"
                bad = zf.testzip()
                if bad is None:
                    return True, f"{len(zf.infolist())} 个条目校验通过"
                return False, f"CRC 校验失败：{bad}"
        if fmt == "rar":
            from .tools import find_unrar

            exe = find_unrar()
            if not exe:
                return None, "跳过校验：未找到 UnRAR / 7-Zip"
            import subprocess
            import sys

            if Path(exe).name.lower().startswith("7z"):
                argv = [exe, "t", str(main)]
            else:
                argv = [exe, "t", "-idq", str(main)]
            kwargs = {"capture_output": True, "timeout": 600, "stdin": subprocess.DEVNULL}
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

                    proc = subprocess.run([zstd, "-t", str(main)], capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
                    return proc.returncode == 0, "zstd 校验通过" if proc.returncode == 0 else "zstd 校验失败"
                return None, "跳过校验：无 zstandard 模块 / zstd / 7-Zip，无法验证 tar.zst 内容"
            with tarfile.open(main, "r:*") as tf:
                count = 0
                for _ in tf:
                    count += 1
            return True, f"{count} 个条目可读取"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return None, "跳过校验：该格式无内置校验实现"


# --------------------------------------------------------------------------
# 保留策略
# --------------------------------------------------------------------------

@dataclass
class CleanupItem:
    blob: str  # 主文件
    files: list[str]
    size: int
    mtime: float
    managed: bool = False  # True = 可信备份（计入份数，可自动淘汰）；False = 旧包/不可信
    complete: bool = True  # False = 缺卷/残包
    stem: str = ""  # manifest stem（managed 时用于清单清理）
    reclaimable: bool = False  # True = 不可信但可回收（committing / partial / unverified）
    # manifest 状态（committed / committing / partial / unverified；无清单时为空串）。
    # P0 修复必需：仅凭 reclaimable 无法区分「中断/失败残包」与「只是没验过」。
    state: str = ""


def _is_unverified(b: CleanupItem) -> bool:
    """是否是「没验过」的备份（P0：它**是**一份备份，只是没做校验）。

    ``verify=False``（用户主动关闭校验）产出的包状态是 ``unverified``；
    它和 ``committing``/``partial`` 一样落在 ``RECLAIMABLE_STATES`` 里，
    但语义完全不同：前两者是"没写完的残包"，后者是"写完了、没验"。
    """
    from .manifest import STATE_UNVERIFIED

    return b.state == STATE_UNVERIFIED


def find_backup_blobs(task: BackupTask) -> list[CleanupItem]:
    """列出备份目录下属于该任务的备份组（manifest 权威 + 旧包后缀启发式）。

    恢复历史行为（审查 C11/E1）：分卷包（.partNN / .001 / partN.rar）与
    `_N` 重名后缀都正确归组；manifest 管理的组带 committed_at 排序依据；
    无清单旧包标记 managed=False（迁移期不被自动淘汰——E5 红线）。
    """
    if not task.destination:
        return []
    folder = Path(task.destination)
    if not folder.is_dir():
        return []
    ext = FORMAT_EXTENSIONS.get(task.options.fmt, ".7z")
    all_exts = tuple(FORMAT_EXTENSIONS.values())
    blobs: list[CleanupItem] = []
    for g in collect_groups(folder, ext, fmts=all_exts):
        # 只保留与该任务格式匹配的组（后缀启发式组）；manifest 组按 fmt 字段过滤
        if g.manifest is not None:
            if g.manifest.fmt and g.manifest.fmt != task.options.fmt:
                continue
            mtime = g.committed_at or (min(f.stat().st_mtime for f in g.files) if g.files else 0.0)
        else:
            if not any(f.name.lower().endswith(ext.lower()) or ("." in f.name and f.name.lower().rfind(ext.lower()) >= 0) for f in g.files):
                continue
            mtime = g.committed_at
        blobs.append(
            CleanupItem(
                blob=str(g.files[0]) if g.files else g.stem,
                files=[str(f) for f in g.files],
                size=g.size,
                mtime=mtime,
                managed=g.managed,
                complete=g.complete,
                stem=g.stem,
                reclaimable=g.reclaimable,
                state=(g.manifest.state if g.manifest is not None else ""),
            )
        )
    return sorted(blobs, key=lambda b: b.mtime)


def apply_retention(
    task: BackupTask,
    protected: list[str] | None = None,
    *,
    include_legacy: bool = False,
    dry_run: bool = False,
) -> list[str]:
    """执行保留策略，返回被处理（删除或移动）的文件列表。

    * 时间基准取 manifest 的 committed_at（当前实现沿用组排序，避免 mtime 触碰劫持）；
      以**本次刚生成的备份**为基准（protected 非空时用最新组时间），保证刚写完也能正确淘汰旧组。
    * **迁移红线（E5）**：无 manifest 的旧包默认不参与自动淘汰（include_legacy=False）；
      调用方可用 plan_adoption() 向用户报告，用户显式开启接管后才传 include_legacy=True。
    * dry_run=True 时只返回"将被处理"的清单，不动文件（供 GUI 预览）。
    """
    policy = task.retention
    if not policy.enabled:
        return []
    blobs = find_backup_blobs(task)
    if not blobs:
        return []
    protected_set = {str(Path(p).resolve()).lower() for p in (protected or [])}

    def _protected(b: CleanupItem) -> bool:
        return any(str(Path(f).resolve()).lower() in protected_set for f in b.files)

    # 三类对象必须分开算（审查 A3 + P0 回归）：
    # * trusted     —— 可信备份（manifest 状态 = committed）：**唯一**计入份数/天数/总量的对象；
    # * unverified  —— 「写完了但没验过」（verify=False 的产物）：**也是一份备份**，
    #                  必须计入份数。否则 verify=False 的用户每次备份都会把上一份当残包删掉
    #                  （P0 数据丢失：实测 keep_last=30 连跑 3 次只剩 1 份）；
    # * junk        —— 中断/失败残组（committing / partial）：不是备份，也不该永久占盘 → 回收；
    # * legacy      —— 无清单旧包：E5 红线，默认只读（include_legacy=True 才参与淘汰）。
    # 本次刚产出的组即使在 retention 时仍是 committing，也必须按"一份备份"计入份数，
    # 否则 keep_last 永远少算一份（原实现的 protected 兜底就是这个作用，不能丢）。
    trusted = [b for b in blobs if b.managed or _protected(b)]
    junk = [
        b
        for b in blobs
        if b.reclaimable and not b.managed and not _protected(b) and not _is_unverified(b)
    ]
    unverified = [
        b
        for b in blobs
        if b.reclaimable and not b.managed and not _protected(b) and _is_unverified(b)
    ]
    legacy = [b for b in blobs if not b.managed and not b.reclaimable and not _protected(b)]
    trusted += unverified  # 无条件计入：没验过 ≠ 不是备份
    trusted += legacy if include_legacy else []
    trusted.sort(key=lambda b: b.mtime)
    junk.sort(key=lambda b: b.mtime)

    expired: list[CleanupItem] = []
    # 0) 中断/失败残组：不是备份 → 直接回收（在飞的本次产物除外；默认 move 到回收目录，可反悔）
    expired.extend(junk)

    if not trusted and not expired:
        return []
    # 以最新一份**可信**备份的完成时刻为时间基准（没有可信备份时退回"此刻"）
    now = trusted[-1].mtime if (protected and trusted) else time.time()

    # 1) 份数：只数可信备份，保留最新 keep_last 份
    if policy.keep_last and len(trusted) > policy.keep_last:
        expired.extend(trusted[: len(trusted) - policy.keep_last])

    # 2) 天数
    cutoff = now - policy.keep_days * 86400
    for b in trusted:
        if b.mtime < cutoff and b not in expired:
            expired.append(b)

    # 3) 总量
    if policy.max_total_gb and policy.max_total_gb > 0:
        limit = policy.max_total_gb * (1024**3)
        total = sum(b.size for b in trusted)
        for b in trusted:
            if total <= limit:
                break
            if b not in expired:
                expired.append(b)
                total -= b.size

    handled: list[str] = []
    for b in expired:
        if any(str(Path(f).resolve()).lower() in protected_set for f in b.files):
            continue
        if dry_run:
            handled.extend(b.files)
            continue
        # 安全默认（审查 E1-1.8/D-r5）：move 模式优先用配置的回收目录；
        # 留空则默认挪到 <备份目录>/.backupstudio/trash/<日期>/（同卷，可反悔）。
        # 绝不静默回退成 delete（"以为只是挪走、实际已删光"——数据丢失类）。
        if policy.on_cleanup == "move":
            if policy.cleanup_dir:
                target_dir = Path(policy.cleanup_dir)
            else:
                target_dir = Path(task.destination) / ".backupstudio" / "trash" / time.strftime("%Y-%m-%d")
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("回收目录不可用 %s: %s；为防误删，本次跳过该组", target_dir, exc)
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
        # 组淘汰后同步删清单（否则遗留"清单在、卷不在"的空组）
        if b.managed and b.stem and not dry_run:
            try:
                from .manifest import delete_manifest

                delete_manifest(task.destination, b.stem)
            except Exception:  # noqa: BLE001
                pass
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


def _manifest_stem_for(path: Path) -> str:
    """主文件名归一到「含压缩格式扩展名」的 stem（去掉 .partNN / .001 等分卷尾巴）。"""
    name = Path(path).name
    low = name.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".zip", ".7z", ".rar", ".tar"):
        idx = low.find(ext)
        if idx >= 0:
            return name[: idx + len(ext)]
    return name


def manifest_state_for(record: RunRecord) -> str:
    """运行记录 → 清单状态（审查 A3：把三态真正落到提交状态上）。

    * 失败（含校验失败）      → ``partial``     ：不可信、可被保留策略回收；
    * 校验未执行（skipped）    → ``unverified``  ：**不算可信备份**（此前写成 committed，
      等于把"没校验"当"可信"，与 skipped 绝不当 passed 的承诺自相矛盾）；
    * 校验通过                → ``committed``   ：唯一可计入份数的状态。
    """
    from .manifest import STATE_COMMITTED, STATE_PARTIAL, STATE_UNVERIFIED

    if record.status != "success":
        return STATE_PARTIAL
    if record.verification == "passed":
        return STATE_COMMITTED
    if record.verification == "failed":
        return STATE_PARTIAL
    return STATE_UNVERIFIED


def _write_manifest_stage(task: BackupTask, record: RunRecord, result: ArchiveResult, stem: str, state: str) -> None:
    """写入/覆写清单（失败只记 warning，不阻断备份本身）。"""
    from .manifest import Manifest, write_manifest

    try:
        mf = Manifest(
            stem=stem,
            task_id=task.id,
            task_name=task.name,
            fmt=record.format,
            state=state,
            volumes=[v.path.name for v in result.volumes],
            size_bytes=record.archive_bytes,
            source_bytes=record.source_bytes,
            files=record.files,
            dirs=record.dirs,
            backend=record.backend,
            encrypted=record.encrypted,
            encrypt_mode=record.encrypt_mode or "",
            created_at=record.started_at,
            committed_at=record.finished_at or record.started_at,
            verified=record.verification,
        )
        write_manifest(task.destination, mf)
    except Exception as exc:  # noqa: BLE001 - 清单失败不阻断备份本身
        record.warnings.append(f"写入备份清单失败（不影响备份文件）：{exc}")


class TaskEngine:
    """执行任务；同一任务不允许并发执行。"""

    def __init__(self, log_fn: Callable[[str], None] | None = None) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.log_fn = log_fn

    def _lock(self, task_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(task_id, threading.Lock())

    def preview_retention(self, task: BackupTask) -> list[str]:
        """只读预览：按当前保留策略，哪些文件会被清理（不动磁盘）。

        供任务对话框展示"将清理 N 份（X GB）"（审查 E1-1.8/D-r5）。
        """
        try:
            return apply_retention(task, dry_run=True)
        except Exception:  # noqa: BLE001 - 预览失败不阻塞编辑
            return []

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
        try:
            # 进程内锁只防同进程重入；跨进程（GUI vs daemon）用目标目录文件锁
            # （审查 P0-6/F2：两进程同秒同名会产坏包且都报 success）。
            dir_lock = None
            if task.destination:
                from .locks import LockTimeout, acquire_dir_lock

                try:
                    dir_lock = acquire_dir_lock(Path(task.destination), task.id, timeout=0.0)
                except LockTimeout as exc:
                    rec = RunRecord(
                        task_id=task.id, task_name=task.name,
                        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                        status="skipped", errors=[f"任务被另一个进程占用：{exc}"], trigger=trigger,
                    )
                    self._log(f"[{task.name}] 跳过：{exc}")
                    return ExecutionResult(rec, message=str(exc))
                except Exception as exc:  # noqa: BLE001 - 锁基础设施失败不阻断备份（降级为进程内锁）
                    self._log(f"[{task.name}] 跨进程锁不可用（降级）：{exc}")
            try:
                started = time.monotonic()
                started_at = datetime.now().astimezone().isoformat(timespec="seconds")
                record = RunRecord(task_id=task.id, task_name=task.name, started_at=started_at, trigger=trigger)
                return self._run_locked(task, record, started, progress_cb, cancel, apply_cleanup)
            finally:
                if dir_lock is not None:
                    with contextlib.suppress(Exception):
                        dir_lock.release()
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
            return self._finish_early(record, task, started, "；".join(problems))

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
            return self._finish_early(record, task, started, "没有可备份的内容")

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

        # 3. 压缩
        # 提前判定压缩引擎（force=True 时允许降级到内置），失败时给出明确原因
        from .archives import BackendUnavailable, backend_for_format

        try:
            engine_choice = backend_for_format(opts.fmt, force=opts.force)
        except BackendUnavailable as exc:
            record.status = "failure"
            record.errors = [str(exc)]
            self._log(f"[{task.name}] 无法开始备份：{exc}")
            return self._finish_early(record, task, started, str(exc))
        warnings.append(f"压缩引擎：{engine_choice.name}")
        if engine_choice.note:
            warnings.append(engine_choice.note)

        # 3b. 空间检查（审查 E1-1.8/D-f6）：按后端形态估磁盘峰值——
        # 外部 7z/rar 需 源tar + 成品 两份（≈2×）；内置引擎 ≈1×；
        # 通用分卷再乘一份完整成品的余量。不足时阻止（不再只 warning）。
        needed = sum(i.size for i in items)
        avail = free_space(dest_dir)
        if avail and needed:
            external = engine_choice.name.startswith(("7z:", "rar"))
            peak_factor = 2.05 if external else 1.05
            if opts.multivolume:
                peak_factor += 1.05  # split 模式先整包再切片，峰值再加一份成品
            est_peak = int(needed * peak_factor)
            if avail < est_peak:
                msg = (
                    f"备份目标剩余空间不足：可用 {human_size(avail)}，"
                    f"本次约需 {human_size(est_peak)}（源数据 {human_size(needed)} × 峰值系数 {peak_factor:.2f}）。"
                    "请清理磁盘或更换备份位置。"
                )
                record.status = "skipped"
                record.errors = [msg]
                self._log(f"[{task.name}] 空间不足，已阻止备份：{msg}")
                return self._finish_early(record, task, started, msg)
            if avail < est_peak * 1.5:
                warnings.append(
                    f"备份目标剩余空间偏紧：可用 {human_size(avail)}，预计峰值约需 {human_size(est_peak)}"
                )

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
            return self._finish_early(record, task, started, "已取消")
        except (CompressionError, OSError) as exc:
            record.status = "failure"
            record.errors = [str(exc)]
            self._log(f"[{task.name}] 备份失败：{exc}")
            return self._finish_early(record, task, started, str(exc))
        except Exception as exc:  # 未预期错误，保留堆栈便于排查
            record.status = "failure"
            record.errors = [f"{type(exc).__name__}: {exc}"]
            record.verification_message = traceback.format_exc()[-2000:]
            self._log(f"[{task.name}] 备份异常：{record.errors[0]}")
            return self._finish_early(record, task, started, record.errors[0])

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

        # 5b. 两阶段提交第一步：卷已全部落盘、但还没校验 → 先写 committing 清单。
        #     崩溃/断电在本步之后 → 目录里留下 committing 组，保留策略能看见并回收
        #     （此前完全没有清单 → 被当 legacy 旧包 → 永不淘汰，磁盘悄悄涨；审查 A3）。
        _manifest_stem = _manifest_stem_for(result.path)
        _write_manifest_stage(task, record, result, _manifest_stem, "committing")

        # 6. 校验（三态：passed / failed / skipped——skipped 绝不当成 passed）
        if task.verify:
            if len(result.volumes) > 1:
                progress.describe("verify")
                ok, msg = _verify_volumes(result.volumes, record.format, opts.password or None)
                record.verification = "passed" if ok is True else ("failed" if ok is False else "skipped")
                record.verification_message = msg
                record.extracted_text = "\n".join(v.path.name for v in result.volumes[:12])
            else:
                progress.describe("verify")
                ok, msg = verify_archive(result.path, record.format, opts.password or None)
                record.verification = "passed" if ok is True else ("failed" if ok is False else "skipped")
                record.extracted_text = _extract_text_sample(result.path, record.format) if ok is True else ""
                record.verification_message = msg
            if record.verification == "failed":
                record.status = "failure"
                record.errors.append(f"校验失败：{record.verification_message}")
            elif record.verification == "skipped":
                warnings.append(f"校验未执行：{record.verification_message}")
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

        # 8. 两阶段提交第二步：状态定稿后覆写清单。
        #    committed = 唯一样本；committing/partial/unverified 都不是「一份可信备份」。
        _final_state = manifest_state_for(record)
        _write_manifest_stage(task, record, result, _manifest_stem, _final_state)

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

    def _finish_early(self, record: RunRecord, task: BackupTask, started: float, message: str = "") -> ExecutionResult:
        """早退路径的统一收尾：定稿时长 + 写历史 + 写 last-run（审查 P0-16：
        此前所有早退都不写 history，用户看记录页以为没跑过）。"""
        record.finished_at = _now()
        record.duration = round(time.monotonic() - started, 2)
        with contextlib.suppress(Exception):
            append_history(record, task)
        with contextlib.suppress(Exception):
            write_last_result(task, record, None)
        return ExecutionResult(record, message=message or (record.errors[0] if record.errors else record.status))


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

            kwargs: dict = {"capture_output": True, "timeout": 60, "stdin": subprocess.DEVNULL}
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


_HISTORY_MAX_BYTES = 5 * 1024 * 1024
_HISTORY_KEEP_LINES = 5000


def _rotate_history_if_needed(path: Path) -> None:
    """超过上限时保留尾部若干行重写（审查 E1-1.7：history.jsonl 无轮转）。"""
    try:
        if not path.exists() or path.stat().st_size <= _HISTORY_MAX_BYTES:
            return
        with open(path, "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
        lines = [ln for ln in raw.splitlines() if ln.strip()][-_HISTORY_KEEP_LINES:]
        tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        pass  # 轮转失败不影响本次写入（下次再试）


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
    _rotate_history_if_needed(path)
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
