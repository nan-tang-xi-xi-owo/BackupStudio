"""备份身份层（manifest）：回答"这是一份完整备份吗"。

背景（审查 E1/E2/E5）：判定"这是一份备份"的依据此前是**文件名后缀**，
由此导出三类实测缺陷：崩溃残包被当合法备份（挤掉好包）、分卷包对保留
策略完全不可见（磁盘无限增长）、保留排序被 mtime 触碰劫持。

设计要点：
* **提交标记**：`state: committing → committed`（全部卷就位后才写 committed）。
  只有 `committed` 的组才参与保留策略的份数/总量计算。
* 存储于 `<destination>/.backupstudio/manifests/<stem>.json`（相对路径、
  明文、原子写）；随目录整体移动不失效。
* **迁移安全（红线）**：无 manifest 的历史归档默认"只读适配"——可浏览、
  可统计，但保留策略**只报告不删除**（`plan_adoption` 供用户显式接管）。
* schema 带 `schema_version`，未知字段忽略（前向兼容）。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("backupstudio.manifest")

MANIFEST_DIRNAME = ".backupstudio"
MANIFEST_SUBDIR = "manifests"
SCHEMA_VERSION = 1

STATE_COMMITTING = "committing"
STATE_COMMITTED = "committed"
STATE_PARTIAL = "partial"


def manifest_dir(destination: Path | str) -> Path:
    """清单目录（自动创建）。"""
    d = Path(destination) / MANIFEST_DIRNAME / MANIFEST_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def manifest_path(destination: Path | str, stem: str) -> Path:
    return manifest_dir(destination) / f"{stem}.json"


@dataclass
class Manifest:
    """一份备份的身份记录。volumes 是权威卷组。"""

    stem: str
    task_id: str = ""
    task_name: str = ""
    fmt: str = ""
    state: str = STATE_COMMITTING
    volumes: list[str] = field(default_factory=list)  # 文件名（不含目录）
    size_bytes: int = 0
    source_bytes: int = 0
    files: int = 0
    dirs: int = 0
    backend: str = ""
    encrypted: bool = False
    encrypt_mode: str = ""
    created_at: str = ""
    committed_at: str = ""
    verified: str = ""  # passed | failed | skipped
    schema_version: int = SCHEMA_VERSION
    index_ref: str = ""  # 预留给未来的文件级索引
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "stem": self.stem,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "fmt": self.fmt,
            "state": self.state,
            "volumes": list(self.volumes),
            "size_bytes": self.size_bytes,
            "source_bytes": self.source_bytes,
            "files": self.files,
            "dirs": self.dirs,
            "backend": self.backend,
            "encrypted": self.encrypted,
            "encrypt_mode": self.encrypt_mode,
            "created_at": self.created_at,
            "committed_at": self.committed_at,
            "verified": self.verified,
            "index_ref": self.index_ref,
        }
        if self.extra:
            data["extra"] = self.extra
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        if not isinstance(kwargs.get("volumes"), list):
            kwargs["volumes"] = []
        return cls(**kwargs)

    @property
    def is_committed(self) -> bool:
        return self.state == STATE_COMMITTED


def write_manifest(destination: Path | str, manifest: Manifest) -> Path:
    """原子写清单（临时名唯一化 + os.replace 重试，与 config.save_json_atomic 同规）。"""
    target = manifest_path(destination, manifest.stem)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    text = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        last: OSError | None = None
        for _ in range(3):
            try:
                os.replace(tmp, target)
                return target
            except OSError as exc:  # Windows 杀软/索引器短暂占用
                last = exc
                time.sleep(0.05)
        assert last is not None
        raise last
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_manifest(path: Path) -> Manifest | None:
    """读单份清单；损坏返回 None（由调用方决定提示）。

    **PermissionError 短暂重试**（审查 W-A2-3，S3）：Windows 上 `os.replace` 发布新清单
    的瞬间、或杀软/索引器持有句柄时，读者会撞 `PermissionError`（实测并发读 31% 为
    PermissionError → 返回 None）。清单"读成 None"不是无害的：该组在 collect_groups 里
    降级为"无清单旧包"（managed=False）→ 保留策略本轮不认它、keep_last 计数少一组
    （多留一份）。故对权限类瞬态错误重试 3 次（间隔 20ms）再放弃。
    JSONDecodeError/结构无效是**确定性**损坏，不重试。
    """
    last_exc: OSError | None = None
    for attempt in range(3):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            break
        except FileNotFoundError:
            return None
        except PermissionError as exc:  # 瞬态：写者正在 replace / 句柄被占
            last_exc = exc
            time.sleep(0.02)
            continue
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("清单读取失败 %s：%s", path, exc)
            return None
    else:
        log.warning("清单读取失败（权限重试 3 次仍不可读）%s：%s", path, last_exc)
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Manifest.from_dict(data)
    except Exception as exc:  # noqa: BLE001 - 坏清单不阻塞
        log.warning("清单结构无效 %s：%s", path, exc)
        return None


def load_manifests(destination: Path | str) -> dict[str, Manifest]:
    """载入目标目录下全部清单（stem → Manifest）。"""
    out: dict[str, Manifest] = {}
    d = Path(destination) / MANIFEST_DIRNAME / MANIFEST_SUBDIR
    if not d.is_dir():
        return out
    for p in d.glob("*.json"):
        m = read_manifest(p)
        if m is not None and m.stem:
            out[m.stem] = m
    return out


def delete_manifest(destination: Path | str, stem: str) -> None:
    try:
        manifest_path(destination, stem).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 备份组（保留策略的操作单元）
# ---------------------------------------------------------------------------

@dataclass
class BackupGroup:
    """一份备份（或一份"疑似备份"的旧包）。"""

    stem: str
    files: list[Path]
    size: int
    committed_at: float  # 用于排序淘汰（committed 用 committed_at；旧包退回 mtime）
    manifest: Manifest | None = None  # None = 无清单的旧包/残包
    complete: bool = True  # False = 缺卷/残包

    @property
    def managed(self) -> bool:
        """是否由 manifest 管理（可安全参与自动淘汰）。"""
        return self.manifest is not None and self.manifest.is_committed


def _volume_sort_key(name: str) -> tuple:
    """分卷名 → 排序键：把结尾的纯数字段按数值排（.part100 > .part11）。"""
    stem = name
    num = -1
    for sep in (".part", "."):
        idx = stem.rfind(sep)
        if idx >= 0 and stem[idx + len(sep):].isdigit():
            num = int(stem[idx + len(sep):])
            stem = stem[:idx]
            break
    return (stem, num, name)


def collect_groups(destination: Path | str, fmt_ext: str, *, fmts: Iterable[str] | None = None) -> list[BackupGroup]:
    """扫描目标目录，按 stem 归组（manifest 权威 + 无清单旧包的后缀启发式）。

    * 有 manifest 的 stem：以 manifest.volumes 为准（缺文件 → complete=False）；
      历史上被改名的 `stem_1` 等后缀会自动算作独立 stem 并尝试"收养"
      （见 adopt_legacy_candidates）。
    * 无 manifest 的包：按 `fmt_ext`/`fmts` 后缀+分卷模式归组（只读用途）。
    """
    folder = Path(destination)
    groups: dict[str, BackupGroup] = {}
    if not folder.is_dir():
        return []

    manifests = load_manifests(folder)
    exts = tuple(fmts) if fmts else (fmt_ext,)

    # 1) 先按清单建组
    for stem, m in manifests.items():
        files: list[Path] = []
        complete = True
        for name in m.volumes or []:
            p = folder / name
            if p.is_file():
                files.append(p)
            else:
                complete = False
        if not files:
            # 清单在但卷全丢失：一组空组（由调用方决定提示/清理）
            complete = False
        size = sum(f.stat().st_size for f in files) if files else 0
        committed_ts = _parse_ts(m.committed_at or m.created_at)
        groups[stem] = BackupGroup(
            stem=stem, files=files, size=size, committed_at=committed_ts, manifest=m, complete=complete
        )

    # 2) 无清单文件：后缀启发式归组（只读）
    claimed: set[str] = set()
    for g in groups.values():
        for f in g.files:
            claimed.add(f.name.lower())

    def _matches(name: str) -> bool:
        low = name.lower()
        return any(low.endswith(e.lower()) or (".part" in low and low.split(".part")[0].lower().endswith(e.lower())) for e in exts)

    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name.lower() in claimed:
            continue
        if not _matches(p.name):
            continue
        # 找这个包的主名（去分卷后缀）
        stem = _stem_of(p.name, exts)
        vols = [q for q in sorted(folder.iterdir(), key=lambda x: _volume_sort_key(x.name))
                if q.is_file() and _stem_of(q.name, exts) == stem]
        if not vols:
            continue
        for q in vols:
            claimed.add(q.name.lower())
        size = sum(q.stat().st_size for q in vols)
        mtime = min(q.stat().st_mtime for q in vols)
        if stem in groups:
            # 主组已由 manifest 建过：把未认领的旧卷并入（complete 判定交给调用方）
            for q in vols:
                if q not in groups[stem].files:
                    groups[stem].files.append(q)
            continue
        groups[stem] = BackupGroup(stem=stem, files=vols, size=size, committed_at=mtime, manifest=None)

    return sorted(groups.values(), key=lambda g: (g.committed_at, g.stem))


def _stem_of(name: str, exts: tuple[str, ...]) -> str:
    """从文件名提取主名（去掉**尾部**分卷后缀，保留容器扩展名）。

    审查 W-A2-2 的同类缺陷：旧实现用 `str.find(ext)` **首匹配**扩展名，任务名本身含
    扩展名时（任务名 `foo.zip` → 产物 `foo.zip_20260101_120000.zip`）会把主名截成
    `foo.zip`，于是**同任务的不同备份被并成一个启发式组**——该组一旦参与淘汰就是整组
    删除（显式接管 include_legacy=True 时会连带删掉有清单的好包）。
    新规则：先去掉**尾部**卷序号（`.001` / `.part01`），再按**最后一个**扩展名切；
    得到的主名与 archives.product_stem(dest.name) 一致（native 分卷
    `X.7z.001` → `X.7z`；split 分卷 `X.tar.gz.part01` → `X.tar.gz`），
    因此启发式组能与 manifest 组正确对表，不再跨备份误并。
    """
    base = name
    low = base.lower()
    for sep in (".part", "."):
        idx = low.rfind(sep)
        if idx > 0 and low[idx + len(sep) :].isdigit():
            base = base[:idx]
            low = base.lower()
            break
    for e in sorted(exts, key=len, reverse=True):
        idx = low.rfind(e.lower())
        if idx >= 0:
            return base[: idx + len(e)]
    return base


def _parse_ts(text: str) -> float:
    if not text:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# 迁移安全（E5 红线）：旧包只报告、不自动删
# ---------------------------------------------------------------------------

@dataclass
class LegacyReport:
    """无清单旧包的盘点报告（用户显式接管前的唯一产物）。"""

    stem: str
    files: list[str]
    size: int
    complete_hint: bool  # 后缀启发式的"像不像完整"（仅供提示）


def plan_adoption(destination: Path | str, fmt_ext: str, *, fmts: Iterable[str] | None = None) -> list[LegacyReport]:
    """清单未覆盖的旧包盘点（只读，绝不删移）。"""
    reports: list[LegacyReport] = []
    for g in collect_groups(destination, fmt_ext, fmts=fmts):
        if g.manifest is None:
            reports.append(
                LegacyReport(
                    stem=g.stem,
                    files=[f.name for f in g.files],
                    size=g.size,
                    complete_hint=bool(g.files),
                )
            )
    return reports
