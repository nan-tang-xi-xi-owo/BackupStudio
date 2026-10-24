"""配置文件仓库：单文件 JSON，线程安全，原子写入，可回滚。"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import logging
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import APP_NAME, __version__
from .config import AppSettings, BackupTask, load_json, save_json_atomic

log = logging.getLogger("backupstudio.store")

CONFIG_VERSION = 1


class TaskStore:
    """保存、加载、校验任务与全局设置。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.tasks: dict[str, BackupTask] = {}
        self.settings = AppSettings()
        self._load()
        self._undo: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    def _load(self) -> None:
        self._load_error: BaseException | None = None
        try:
            raw = load_json(self.path, {})
        except Exception as exc:  # noqa: BLE001 - 配置损坏不应阻止启动
            log.warning("读取配置失败，使用空配置：%s", exc)
            # 记住失败原因：保存时若原文件仍存在，先保全再覆写（防"读不到→空库回写"数据丢失链）
            self._load_error = exc
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self.settings = AppSettings.from_dict(raw.get("settings"))
        tasks: dict[str, BackupTask] = {}
        for item in raw.get("tasks", []) or []:
            try:
                t = BackupTask.from_dict(item)
                tasks[t.id] = t
            except Exception as exc:  # 单条损坏不影响其它任务
                log.warning("跳过损坏的任务配置: %s (%s)", exc, type(exc).__name__)
        self.tasks = tasks
        self._version = raw.get("version", CONFIG_VERSION)

    def save(self) -> None:
        with self._lock:
            # 加载曾失败且原文件仍在：先保全迁移，禁绝不覆盖（防数据丢失链）
            if self._load_error is not None and self.path.exists():
                stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                rescue = self.path.with_name(self.path.name + f".rescue-{stamp}")
                try:
                    self.path.replace(rescue)
                    log.warning("配置读取曾失败（%s）；原文件已保全为 %s，避免覆写", self._load_error, rescue)
                except OSError:
                    log.error("配置读取曾失败且无法保全原文件，取消本次保存以避免覆写 %s", self.path)
                    raise
                self._load_error = None
            data = {
                "app": APP_NAME,
                "version": CONFIG_VERSION,
                "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "settings": self.settings.to_dict(),
                "tasks": [t.to_dict() for t in self.tasks.values()],
            }
            save_json_atomic(self.path, data)
            self._undo.append({"saved_at": data["saved_at"], "data": copy.deepcopy(data["tasks"])})
            del self._undo[:-20]

    # ------------------------------------------------------------------
    def list_tasks(self) -> list[BackupTask]:
        with self._lock:
            return list(self.tasks.values())

    def get(self, task_id: str) -> BackupTask | None:
        with self._lock:
            return self.tasks.get(task_id)

    def upsert(self, task: BackupTask) -> BackupTask:
        with self._lock:
            task.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.tasks[task.id] = task
            return task

    def delete(self, task_id: str) -> bool:
        with self._lock:
            return self.tasks.pop(task_id, None) is not None

    def duplicate(self, task_id: str) -> BackupTask | None:
        with self._lock:
            src = self.tasks.get(task_id)
            if src is None:
                return None
            data = src.to_dict()
            data["id"] = ""
            data["name"] = f"{src.name}（副本）"
            data["schedule"] = {"type": "manual"}
            data["last_run"] = ""
            data["last_status"] = ""
            data["last_message"] = ""
            data["last_archive"] = ""
            t = BackupTask.from_dict(data)
            self.tasks[t.id] = t
            return t

    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def editing(self, task_id: str):
        """以副本方式编辑任务，退出时校验并保存；异常则放弃修改。"""
        with self._lock:
            original = self.tasks.get(task_id)
            if original is None:
                raise KeyError(task_id)
            draft = BackupTask.from_dict(original.to_dict())
        yield draft
        with self._lock:
            self.tasks[draft.id] = draft
            self.save()

    # ------------------------------------------------------------------
    def undo_last_save(self) -> bool:
        with self._lock:
            if not self._undo:
                return False
            snapshot = self._undo.pop()
            self.tasks = {}
            for item in snapshot["data"]:
                t = BackupTask.from_dict(item)
                self.tasks[t.id] = t
            data = {
                "app": APP_NAME,
                "version": CONFIG_VERSION,
                "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "settings": self.settings.to_dict(),
                "tasks": [t.to_dict() for t in self.tasks.values()],
            }
            save_json_atomic(self.path, data)
            return True

    # ------------------------------------------------------------------
    def export_bundle(self, target: Path, include_passwords: bool = False) -> Path:
        """导出任务配置（可选含密码）为单个 json，便于迁移。"""
        with self._lock:
            tasks = []
            for t in self.tasks.values():
                data = t.to_dict()
                if not include_passwords:
                    data["options"]["password"] = ""
                tasks.append(data)
            bundle = {
                "app": APP_NAME,
                "version": __version__,
                "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "settings": self.settings.to_dict(),
                "tasks": tasks,
            }
        save_json_atomic(target, bundle)
        return target

    def import_bundle(self, source: Path, replace: bool = False) -> int:
        with open(source, encoding="utf-8") as fh:
            data = json.load(fh)
        incoming = data.get("tasks", []) if isinstance(data, dict) else []
        if not isinstance(incoming, list):
            incoming = []
        with self._lock:
            if replace:
                self.tasks.clear()
            count = 0
            for item in incoming:
                if not isinstance(item, dict):
                    continue  # 容忍畸形条目（字符串/数组等）
                try:
                    t = BackupTask.from_dict(item)
                except Exception as exc:  # noqa: BLE001
                    log.warning("导入任务失败，已跳过：%s", exc)
                    continue
                if t.id in self.tasks:
                    t.id = ""
                    t = BackupTask.from_dict({**t.to_dict(), "id": ""})
                self.tasks[t.id] = t
                count += 1
            self.save()
            return count

    # ------------------------------------------------------------------
    def readme_zip_bytes(self) -> bytes:
        """生成一个包含说明的恢复用 zip（可选功能入口）。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "README.txt",
                "由 BackupStudio 导出的任务清单。\n"
                "本文件不含备份数据，仅记录任务配置，便于迁移。\n",
            )
            with self._lock:
                for t in self.tasks.values():
                    data = t.to_dict()
                    data["options"]["password"] = ""
                    zf.writestr(f"tasks/{t.id}.json", json.dumps(data, ensure_ascii=False, indent=2))
        return buf.getvalue()
