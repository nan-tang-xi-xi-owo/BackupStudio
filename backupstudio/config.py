"""任务与配置数据模型（纯数据 + 序列化，不依赖 GUI）。"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from .archives import SUPPORTED_FORMATS
from .util import normalize_path

SCHEDULE_TYPES = ("manual", "interval", "daily", "weekly", "monthly")
ENCRYPTION_MODES = ("aes256", "zipcrypto", "gpg")
MULTIVOLUME_MODES = ("7z-split", "rar-native", "split")
ON_ERRORS = ("stop", "continue")
NOTIFY_EVENTS = ("success", "failure")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ScheduleConfig:
    """调度配置。

    * ``manual``   仅手动
    * ``interval`` 每隔 minutes 分钟
    * ``daily``    每天 time（HH:MM）
    * ``weekly``   每周 days（0=周一）的 time
    * ``monthly``  每月 day 日 的 time
    """

    type: str = "manual"
    time: str = "02:00"
    days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # 周一~周五
    day: int = 1  # 每月几号
    minutes: int = 60

    def __post_init__(self) -> None:
        self.normalize()

    def normalize(self) -> None:
        if self.type not in SCHEDULE_TYPES:
            self.type = "manual"
        hh, mm = _parse_time(self.time)
        self.time = f"{hh:02d}:{mm:02d}"
        self.days = sorted({int(d) for d in (self.days or []) if 0 <= int(d) <= 6})
        self.day = max(1, min(31, int(self.day or 1)))
        self.minutes = max(1, int(self.minutes or 60))

    def describe(self) -> str:
        if self.type == "manual":
            return "仅手动"
        if self.type == "interval":
            if self.minutes % 60 == 0:
                return f"每 {self.minutes // 60} 小时"
            return f"每 {self.minutes} 分钟"
        if self.type == "daily":
            return f"每天 {self.time}"
        if self.type == "weekly":
            names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            if not self.days:
                return "每周（未选日期）"
            if self.days == [0, 1, 2, 3, 4]:
                prefix = "每个工作日"
            elif self.days == [5, 6]:
                prefix = "每周末"
            elif self.days == list(range(7)):
                prefix = "每天"
            else:
                prefix = "每周 " + "、".join(names[d] for d in self.days)
            return f"{prefix} {self.time}"
        if self.type == "monthly":
            return f"每月 {self.day} 日 {self.time}"
        return self.type

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ScheduleConfig":
        data = data or {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def _parse_time(text: str) -> tuple[int, int]:
    try:
        parts = str(text).strip().split(":")
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except (ValueError, IndexError):
        pass
    return 2, 0


@dataclass
class SourceConfig:
    path: str = ""
    mode: str = "folder"  # folder | files
    files: list[str] = field(default_factory=list)  # mode=files 时文件夹内指定文件（相对路径或子路径）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SourceConfig":
        data = data or {}
        return cls(
            path=str(data.get("path", "")),
            mode="files" if data.get("mode") == "files" else "folder",
            files=[str(f) for f in data.get("files", []) or []],
        )


@dataclass
class ArchiveOptions:
    fmt: str = "7z"
    level: int = 5
    password: str = ""
    encrypt_mode: str = "aes256"
    multivolume: str = ""  # 空 = 不分卷
    volume_value: int = 100
    volume_unit: str = "MB"
    filename_template: str = "{name}_{datetime}"
    exclude_globs: list[str] = field(default_factory=list)
    follow_symlinks: bool = False
    force: bool = False

    def __post_init__(self) -> None:
        self.normalize()

    def normalize(self) -> None:
        if self.fmt not in SUPPORTED_FORMATS:
            self.fmt = "7z"
        self.level = max(0, min(9, int(self.level)))
        if self.encrypt_mode not in ENCRYPTION_MODES:
            self.encrypt_mode = "aes256"
        if self.multivolume not in ("", *MULTIVOLUME_MODES):
            self.multivolume = ""
        self.volume_value = max(1, int(self.volume_value or 1))
        if self.volume_unit not in ("KB", "MB", "GB"):
            self.volume_unit = "MB"
        if not self.filename_template.strip():
            self.filename_template = "{name}_{datetime}"
        self.exclude_globs = [g.strip() for g in self.exclude_globs if g and g.strip()]

    def volume_size_bytes(self) -> int:
        factor = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}[self.volume_unit]
        return self.volume_value * factor

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ArchiveOptions":
        data = data or {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class RetentionPolicy:
    """保留策略：按份数 / 天数 / 总量，超出即为「过期」。"""

    enabled: bool = False
    keep_last: int = 30
    keep_days: int = 90
    max_total_gb: float = 0.0  # 0 表示不限制
    on_cleanup: str = "delete"  # delete | move
    cleanup_dir: str = ""

    def __post_init__(self) -> None:
        self.keep_last = max(1, int(self.keep_last or 1))
        self.keep_days = max(1, int(self.keep_days or 1))
        self.max_total_gb = max(0.0, float(self.max_total_gb or 0))
        if self.on_cleanup not in ("delete", "move"):
            self.on_cleanup = "delete"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RetentionPolicy":
        data = data or {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class BackupTask:
    id: str = field(default_factory=_new_id)
    name: str = ""
    enabled: bool = True
    sources: list[SourceConfig] = field(default_factory=list)
    destination: str = ""
    destination_mode: str = "template"  # template（按模板建子目录） | flat
    options: ArchiveOptions = field(default_factory=ArchiveOptions)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    on_error: str = "continue"
    verify: bool = True
    notify: list[str] = field(default_factory=lambda: ["failure"])
    last_run: str = ""
    last_status: str = ""  # success | failure | skipped | running
    last_message: str = ""
    last_archive: str = ""
    last_duration: float = 0.0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self._default_name()
        if not self.sources:
            self.sources = [SourceConfig()]
        self.options.normalize()
        self.schedule.normalize()

    def _default_name(self) -> str:
        for s in self.sources:
            if s.path:
                return Path(s.path).name or "备份任务"
        return "备份任务"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sources"] = [s.to_dict() for s in self.sources]
        data["options"] = self.options.to_dict()
        data["schedule"] = self.schedule.to_dict()
        data["retention"] = self.retention.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackupTask":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["sources"] = [SourceConfig.from_dict(s) for s in data.get("sources", [])]
        kwargs["options"] = ArchiveOptions.from_dict(data.get("options"))
        kwargs["schedule"] = ScheduleConfig.from_dict(data.get("schedule"))
        kwargs["retention"] = RetentionPolicy.from_dict(data.get("retention"))
        if "id" not in kwargs or not kwargs["id"]:
            kwargs["id"] = _new_id()
        task = cls(**kwargs)
        if not task.sources:
            task.sources = [SourceConfig()]
        return task

    def needs_password(self) -> bool:
        return bool(self.options.password)

    def source_paths(self) -> list[str]:
        out = []
        for s in self.sources:
            p = normalize_path(s.path)
            if p:
                out.append(p)
        return out

    @property
    def primary_source(self) -> str:
        paths = self.source_paths()
        return paths[0] if paths else ""

    def with_defaults(self) -> "BackupTask":
        return copy.deepcopy(self)


MANUAL_HINT = "（仅手动时不会自动执行，可用托盘菜单或「立即备份」触发）"


@dataclass
class NotificationConfig:
    enabled: bool = True
    tray: bool = True
    native: bool = True
    events: list[str] = field(default_factory=lambda: list(NOTIFY_EVENTS))
    quiet_hours: bool = False
    quiet_start: str = "23:00"
    quiet_end: str = "07:00"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "NotificationConfig":
        data = data or {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class AppSettings:
    start_with_system: bool = False
    start_minimized: bool = False
    close_to_tray: bool = True
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    log_retention_days: int = 90
    auto_run_backup_daemon: bool = True
    theme_mode: str = "system"  # system | light | dark
    tray_hint_shown: bool = False  # 是否已经提示过「程序在托盘里」

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["notifications"] = self.notifications.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AppSettings":
        data = data or {}
        return cls(
            start_with_system=bool(data.get("start_with_system", False)),
            start_minimized=bool(data.get("start_minimized", False)),
            close_to_tray=bool(data.get("close_to_tray", True)),
            notifications=NotificationConfig.from_dict(data.get("notifications")),
            log_retention_days=int(data.get("log_retention_days", 90) or 90),
            auto_run_backup_daemon=bool(data.get("auto_run_backup_daemon", True)),
            theme_mode=str(data.get("theme_mode", "system") or "system"),
            tray_hint_shown=bool(data.get("tray_hint_shown", False)),
        )


# --------------------------------------------------------------------------
# 运行记录
# --------------------------------------------------------------------------

@dataclass
class RunRecord:
    task_id: str
    task_name: str
    started_at: str
    finished_at: str = ""
    status: str = "success"
    archive: str = ""
    format: str = ""
    backend: str = ""
    volumes: int = 1
    archive_bytes: int = 0
    source_bytes: int = 0
    files: int = 0
    dirs: int = 0
    skipped: int = 0
    duration: float = 0.0
    verification: str = "skipped"  # passed | failed | skipped
    verification_message: str = ""
    extracted_text: str = ""  # 校验后重建的清单摘要
    encrypted: bool = False
    encrypt_mode: str = ""
    secure: bool = True
    multivolume: str = ""
    forced: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cleaned: list[str] = field(default_factory=list)
    trigger: str = "manual"  # manual | schedule

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------

def save_json_atomic(path: Path | str, data: Any) -> None:
    """原子写入 JSON；文件权限收紧到仅当前用户可读写。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_json(path: Path | str, default: Any) -> Any:
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, json.JSONDecodeError):
        # 损坏的配置不阻塞启动：备份一份后回到默认值
        try:
            if path.exists():
                path.replace(path.with_suffix(path.suffix + ".corrupt"))
        except OSError:
            pass
        return copy.deepcopy(default)
