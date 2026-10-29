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

# 运行状态字段：由守护进程/引擎回写，与用户编辑的"配置字段"分属两组写者。
# 三方合并时它们单独取"最近一次执行结果"的一方，避免 GUI 保存设置时把
# 守护进程刚写回的 last_run/last_status 洗回空串（W-A5 质疑②附带 / R3-C12 方向 2）。
RUN_STATE_FIELDS = ("last_run", "last_status", "last_message", "last_archive", "last_duration")


def _canon_task(data: Any) -> dict | None:
    """把磁盘上的任务字典规范化（from_dict → to_dict）后再比较。

    直接比原始 JSON 会因"读入时 normalize() 修正了非法值"而产生假差异，
    导致每次保存都误判"外部有变更"。规范化后两侧在同一空间内比较。
    """
    try:
        return BackupTask.from_dict(data).to_dict()
    except Exception:  # noqa: BLE001 - 畸形条目不参与比较（由调用方决定去留）
        return None


def _canon_settings(data: Any) -> dict:
    try:
        return AppSettings.from_dict(data).to_dict()
    except Exception:  # noqa: BLE001
        return {}


def _ts(text: Any) -> float:
    """ISO 时间戳 → 可比较的数值；无法解析时返回 -1（视为最旧）。"""
    if not text:
        return -1.0
    try:
        dt = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return -1.0
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def _pick_newer(mem_data: dict, disk_data: dict) -> str:
    """冲突时按 updated_at 判定新旧；平局（同秒 / 都无法解析）以内存为准，
    保证本地未落盘的编辑不会被外部快照无声丢弃。"""
    return "disk" if _ts(disk_data.get("updated_at")) > _ts(mem_data.get("updated_at")) else "mem"


def _pick_newer_run(mem_data: dict, disk_data: dict) -> str:
    """运行状态冲突：按 last_run 判定，平局以内存为准。"""
    return "disk" if _ts(disk_data.get("last_run")) > _ts(mem_data.get("last_run")) else "mem"


class TaskStore:
    """保存、加载、校验任务与全局设置。

    双写者安全（W-A7-2 / W-A5 质疑②）：本仓库记住"上次看到的磁盘内容"作为基线，
    载入外部改动与写盘前都做三方合并（基线 / 内存 / 磁盘），
    保守方向为**绝不丢任何一方的任务**：宁可多留一个任务，也不静默抹掉它。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.tasks: dict[str, BackupTask] = {}
        self.settings = AppSettings()
        # 基线与磁盘快照（_load 内填充）
        self._baseline: dict[str, dict] = {}
        self._baseline_settings: dict = {}
        self._deleted_ids: set[str] = set()
        self._disk_stamp: tuple[float, int] | None = None
        self._load()
        self._undo: list[dict[str, Any]] = []
        # save() 的合并如果吸收了外部任务（有外部新增等），置位供界面下次轮询时刷新列表
        self._absorbed_on_save = False

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
        # settings 字段畸形（"str" / 123 / notifications="on"）曾让 AppSettings.from_dict
        # 直接抛 AttributeError → GUI 与守护进程双双启动即崩（审查 W-A5-7，S2）。
        # 任务列表比设置重要得多：这里降级为默认设置，绝不因设置放弃整个仓库。
        try:
            self.settings = AppSettings.from_dict(raw.get("settings"))
        except Exception as exc:  # noqa: BLE001
            log.warning("全局设置无法解析，已使用默认设置（任务列表照常载入）：%s", exc)
            self.settings = AppSettings()
        tasks: dict[str, BackupTask] = {}
        raw_tasks = raw.get("tasks", []) or []
        if not isinstance(raw_tasks, list):
            log.warning("tasks 字段不是列表（%s），已忽略", type(raw_tasks).__name__)
            raw_tasks = []
        for item in raw_tasks:
            try:
                t = BackupTask.from_dict(item)
                tasks[t.id] = t
            except Exception as exc:  # 单条损坏不影响其它任务
                log.warning("跳过损坏的任务配置: %s (%s)", exc, type(exc).__name__)
        self.tasks = tasks
        self._version = raw.get("version", CONFIG_VERSION)
        # 基线 = 本次载入的磁盘内容（规范化形式），供后续三方比较
        self._baseline = {t.id: t.to_dict() for t in tasks.values()}
        self._baseline_settings = self.settings.to_dict()
        self._deleted_ids = set()
        self._disk_stamp = self._stat_stamp()

    def _stat_stamp(self) -> tuple[float, int] | None:
        """磁盘文件的 (mtime, size) 指纹；不可读时返回 None（视作"无外部改动"）。"""
        try:
            st = self.path.stat()
            return (round(st.st_mtime, 6), st.st_size)
        except OSError:
            return None

    def external_changed(self) -> bool:
        """磁盘 tasks.json 是否被本进程之外改动过（(mtime, size) 指纹比对）。

        文件消失/不可 stat 时指纹为 None，与已记录的指纹不等 → 视为"有变化"，
        由调用方（reload_if_changed / _merge_from_disk_quiet）再判断要不要合并。
        """
        with self._lock:
            return self._stat_stamp() != self._disk_stamp

    # ------------------------------------------------------------------
    def reload_if_changed(self) -> bool:
        """外部改动时：磁盘内容三方合并进内存（不丢弃任何一方的任务）。

        返回 True 表示内存被外部内容改变（调用方可据此刷新界面）。
        读盘失败（瞬态占用）→ 保守返回 False，什么都不改。
        """
        with self._lock:
            if not self.external_changed():
                return False
            try:
                disk_raw = load_json(self.path, None)
            except Exception as exc:  # noqa: BLE001 - 瞬态读失败：保持内存不动
                log.debug("重载配置失败（保持内存不变）：%s", exc)
                self._disk_stamp = self._stat_stamp()
                return False
            if disk_raw is None:  # 文件不存在/被删：不合并，避免误清空
                self._disk_stamp = self._stat_stamp()
                return False
            before = {t.id: t.to_dict() for t in self.tasks.values()}
            self._merge_locked(disk_raw)
            self._disk_stamp = self._stat_stamp()
            after = {t.id: t.to_dict() for t in self.tasks.values()}
            return before != after

    def _merge_locked(self, disk_raw: Any) -> None:
        """三方合并：基线 `self._baseline` / 内存 `self.tasks` / 磁盘 `disk_raw`。

        规则（保守方向 = 宁多勿少，绝不静默丢任何一方的**任务**）：

        * 内存没有、磁盘有：
          - 该 id 在删除墓碑 `_deleted_ids` 里 → 尊重我自己的删除意图，不复活；
          - 否则 → 外部新增/外部写回：收下。
        * 内存有、磁盘没有：
          - 基线没有 → 本地新增尚未落盘：保留；
          - 基线有、但内存已本地改动 → 删除 vs 修改冲突：**保留内存版本**
            （外部那次"删除"很可能是陈旧快照的整体覆写，宁可多留一个任务）；
          - 基线有、且内存与基线一致 → 外部删除是有意的：吸收这次删除。
        * 双方都有且不同：
          - 磁盘 == 基线（外部没碰）→ 用内存；
          - 内存 == 基线（本地没碰）→ 用磁盘；
          - 都改过 → **字段级**三方合并：相对基线只有一方动过的字段取动过的那一方，
            两边都动过的字段按 `updated_at` 裁决（平局内存优先）；
            运行状态组 `RUN_STATE_FIELDS` 单独裁决（只有外部动过 → 吸收外部，
            只有本地动过 → 保内存，都动过 → 取 `last_run` 新者）。
            这样"守护进程回写执行结果"与"用户改任务配置"两组写者互不洗掉。
        * 磁盘内容不可解释（非 dict / 缺 `tasks` 键）→ 直接返回：绝不据此判定"外部删光了任务"。
        """

        raw_items = disk_raw.get("tasks", []) if isinstance(disk_raw, dict) else []
        if not isinstance(disk_raw, dict) or "tasks" not in disk_raw:
            return  # 磁盘内容不可解释（非字典/缺 tasks 键）：绝不据此判定"外部删光了任务"
        if not isinstance(raw_items, list):
            raw_items = []
        disk_tasks: dict[str, dict] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue  # 磁盘上的畸形条目不参与合并（也不复活）
            canon = _canon_task(item)
            if canon is not None and canon.get("id"):
                disk_tasks[str(canon["id"])] = canon

        mem_data = {t.id: t.to_dict() for t in self.tasks.values()}
        base = self._baseline
        merged: dict[str, dict] = {}

        for tid in list(mem_data) + [x for x in disk_tasks if x not in mem_data]:
            m = mem_data.get(tid)
            d = disk_tasks.get(tid)
            b = base.get(tid)
            if m is None:
                # 内存没有这条：我的删除意图优先，否则视为外部新增收下
                if tid in self._deleted_ids:
                    continue
                merged[tid] = d
                continue
            if d is None:
                if b is None or m != b:
                    merged[tid] = m  # 本地新增未落盘 / 本地已改动：保留（宁多勿少）
                # 否则：内存与基线一致而外部删了它 → 吸收外部删除
                continue
            if m == d:
                merged[tid] = m
                continue
            if b is not None and d == b:
                merged[tid] = m  # 外部未改动 → 用内存
                continue
            if b is not None and m == b:
                merged[tid] = d  # 本地未改动 → 用磁盘
                continue
            # 双方都改：字段级三方合并（相对基线只有一方动过的字段取动过的那一方），
            # 都动过的字段按 updated_at 裁决（平局内存优先）
            winner = dict(m)
            disk_wins_config = _pick_newer(m, d) == "disk"
            for key, val in d.items():
                if key == "id" or key in RUN_STATE_FIELDS:
                    continue
                if b is None:
                    if disk_wins_config:
                        winner[key] = val
                    continue
                m_changed = m.get(key) != b.get(key)
                d_changed = val != b.get(key)
                if d_changed and (not m_changed or disk_wins_config):
                    winner[key] = val
            # 运行状态组（守护进程/引擎是其主要写者）单独裁决：
            # 只有外部动过 → 吸收外部（GUI 改主题不得洗掉 last_run/last_status）；
            # 只有本地动过 → 保内存；两边都动过 → 取 last_run 更新的一方。
            run_from = d
            if b is not None:
                m_run = {k: m.get(k) for k in RUN_STATE_FIELDS}
                b_run = {k: b.get(k) for k in RUN_STATE_FIELDS}
                d_run = {k: d.get(k) for k in RUN_STATE_FIELDS}
                if m_run == b_run:
                    run_from = d  # 本地没碰运行状态
                elif d_run != b_run:
                    run_from = d if _pick_newer_run(m, d) == "disk" else m
                else:
                    run_from = m  # 只有本地动过
            elif _pick_newer_run(m, d) != "disk":
                run_from = m
            for key in RUN_STATE_FIELDS:
                winner[key] = run_from.get(key, winner.get(key))
            winner["updated_at"] = max(
                (m.get("updated_at") or ""), (d.get("updated_at") or ""), key=_ts
            )
            merged[tid] = winner

        new_tasks: dict[str, BackupTask] = {}
        for tid, data in merged.items():
            if data is None:
                continue
            try:
                t = BackupTask.from_dict({**data, "id": tid})
            except Exception as exc:  # noqa: BLE001 - 合并结果理论上都可解析
                log.warning("合并后的任务无法解析，已跳过：%s", exc)
                continue
            new_tasks[t.id] = t
        self.tasks = new_tasks

        # 全局设置：只有 GUI 会写设置（守护进程不回写），冲突时保留内存（本次保存的意图）
        disk_settings = _canon_settings(disk_raw.get("settings") if isinstance(disk_raw, dict) else None)
        base_settings = self._baseline_settings or {}
        if disk_settings and self.settings.to_dict() == base_settings and disk_settings != base_settings:
            # 本地没碰设置、外部改了 → 吸收外部设置
            self.settings = AppSettings.from_dict(disk_settings)
        # 删除墓碑：磁盘上已不存在的（删除意图已落到磁盘）可以遗忘，
        # 仍在磁盘上的保留到下一次成功写盘，避免多次合并中被外部陈旧快照复活。
        self._deleted_ids &= set(disk_tasks)
        self._baseline = {t.id: t.to_dict() for t in self.tasks.values()}
        self._baseline_settings = self.settings.to_dict()

    def save(self) -> None:
        with self._lock:
            # **跨进程排他锁**（用户测试 P0：8 并发 task add 只活 1 个）。
            # 线程锁管不了 CLI/daemon/GUI 多进程；不加这把锁时，8 个进程会
            # 同时读到同一份旧快照、都认为"无外部改动"，然后各自整体覆写，
            # 后写者抹掉前写者。实测丢失率 7/8。
            #
            # 持锁范围覆盖「读磁盘 → 合并 → 原子写」整段，使读-改-写成为临界区。
            from .filelock import FileLock

            with FileLock(self.path):
                # 锁内重读磁盘，干掉 TOCTOU（路径 B）：_load() 的「读内容」与
                # 「取 (mtime,size) 指纹」不原子，若别的进程在两者之间写盘，
                # external_changed() 会误判"无外部改动" → 持锁也跳过合并。
                # 基线一旦出自锁外，锁就白加了。
                self._save_locked()


    def _save_locked(self) -> None:
        """真正的保存逻辑（调用方必须已持有跨进程锁）。"""
        # 加载曾失败且原文件仍在：先保全迁移，**再从保全文件回读合并**（P0）。
        #
        # 旧实现只做 `path.replace(rescue)` 然后写自己内存里的内容 ——
        # 而内存是空的（正是"加载失败"导致的），于是 **10 个任务只剩本次这 1 个**。
        # 红队实测：一次瞬态 OSError（Windows 上 os.replace 与 open 的共享冲突，
        # `save_json_atomic` 自己都为此重试 12 次，说明这个瞬态是**已知会发生的**）
        # 就能让用户任务整库消失，且 rc=0。
        #
        # 保全方向（不覆写）是对的，但**保全之后必须把能读出来的都读回来**。
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
            # **关键：从保全文件回读，把用户原有的任务救回来**
            try:
                recovered = load_json(rescue, {})
                if isinstance(recovered, dict) and isinstance(recovered.get("tasks"), list):
                    self._merge_locked(recovered)
                    log.warning(
                        "已从保全文件 %s 回读并合并 %d 个任务（避免整库丢失）",
                        rescue.name,
                        len(recovered.get("tasks") or []),
                    )
            except Exception as exc:  # noqa: BLE001 - 回读失败也不能阻止保存
                log.error("从保全文件回读失败（任务可能仍需人工从 %s 恢复）：%s", rescue.name, exc)
            self._disk_stamp = self._stat_stamp()
        else:
            # **锁内无条件重读磁盘再合并**（TOCTOU 修复，并发 Agent 验证的形状）。
            #
            # 旧写法 `self._merge_from_disk_quiet()` 先问 `external_changed()`，
            # 而它按 `(mtime, size)` 指纹判断 —— 指纹撞车率实测 2.6%（300 次
            # os.replace 有 10 次相邻指纹完全相同），且 `_load()` 里「读内容」与
            # 「取指纹」本身不原子（读在加锁之前）：
            #   A 读到旧内容 → B 写盘 → A 才 stat 到新文件 → 判定"无外部改动"
            #   → **持锁也跳过合并** → 整库覆写。
            # 确定性复现：A、B 全程都持锁，TASK-B 仍被静默抹掉。
            #
            # 现在锁已经拿到了，磁盘就是权威 —— 无条件读、无条件合并。
            # **注意：整个过程只合并这一次**（早前版本在 save() 里再加一次
            # `_reload_under_lock()`，造成双重合并 → 20 并发丢 93%，已回滚）。
            self._merge_from_disk_locked()
        data = self._snapshot()
        save_json_atomic(self.path, data)
        self._undo.append({"saved_at": data["saved_at"], "data": copy.deepcopy(data["tasks"])})
        del self._undo[:-20]
        self._after_write()

    def _merge_from_disk_locked(self) -> None:
        """**锁内**无条件重读磁盘并合并（TOCTOU 修复）。

        与 `_merge_from_disk_quiet()` 的区别：**不问 `external_changed()`**。
        锁已经在手，磁盘就是当前真相，指纹判断只会带来漏判（实测撞车率 2.6%，
        且「读内容」与「取指纹」不原子）。

        读失败时保持保守：只写自己内存里的内容，绝不清空别人的任务。
        """
        try:
            disk_raw = load_json(self.path, {})
        except Exception as exc:  # noqa: BLE001 - 瞬态读失败：保守，只写自己
            log.warning("锁内读取磁盘失败（本次只写自己内存的内容）：%s", exc)
            return
        before = set(self.tasks)
        try:
            self._merge_locked(disk_raw)
        except Exception as exc:  # noqa: BLE001 - 合并异常不应阻止保存
            log.warning("锁内合并外部改动失败（本次只写自己内存的内容）：%s", exc)
            return
        self._disk_stamp = self._stat_stamp()
        if set(self.tasks) - before:
            # 界面这次保存吸收到外部任务：让下一次轮询刷新列表（不丢显示）
            self._absorbed_on_save = True

    def _merge_from_disk_quiet(self) -> None:
        """把磁盘内容三方合并进内存；任何读取异常都按"无外部改动"处理。"""
        if not self.external_changed():
            return
        try:
            disk_raw = load_json(self.path, {})
        except Exception as exc:  # noqa: BLE001 - 瞬态读失败：保守，只写自己的内容
            log.warning("保存前读取外部改动失败（本次按无外部改动处理）：%s", exc)
            return
        before = set(self.tasks)
        try:
            self._merge_locked(disk_raw)
        except Exception as exc:  # noqa: BLE001 - 合并异常不应阻止保存
            log.warning("保存前合并外部改动失败（本次按无外部改动处理）：%s", exc)
        self._disk_stamp = self._stat_stamp()
        if set(self.tasks) - before:
            # 界面这次保存吸收到外部任务：让下一次轮询刷新列表（不丢显示）
            self._absorbed_on_save = True

    def take_pending_absorb(self) -> bool:
        """取出并清除"保存时吸收了外部改动"标记（界面据此刷新列表一次）。"""
        with self._lock:
            flag = self._absorbed_on_save
            self._absorbed_on_save = False
            return flag

    def _snapshot(self) -> dict:
        return {
            "app": APP_NAME,
            "version": CONFIG_VERSION,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "settings": self.settings.to_dict(),
            "tasks": [t.to_dict() for t in self.tasks.values()],
        }

    def _after_write(self) -> None:
        """写盘成功后：基线与磁盘指纹对齐到刚写出的内容，删除墓碑可以作废（本锁内调用）。

        墓碑只需要跨越 `delete()` → 本次落盘 这段窗口（防止合并期间被外部陈旧快照复活）；
        写出去的内容已不含被删 id，删除意图已经落到磁盘。
        """
        self._baseline = {t.id: t.to_dict() for t in self.tasks.values()}
        self._baseline_settings = self.settings.to_dict()
        self._deleted_ids = set()
        self._disk_stamp = self._stat_stamp()

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
            removed = self.tasks.pop(task_id, None) is not None
            if removed:
                # 记住"这是我主动的删除"，合并时不被外部快照复活；
                # 若基线里本就没有它（从未落盘的新任务）则无需记录。
                if task_id in self._baseline:
                    self._deleted_ids.add(task_id)
            return removed

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
            # **必须持跨进程锁**（并发 Agent 确定性复现）：
            # 这是全仓库唯一一条「锁外整库写」路径。不加锁时与 `save()` 并发会
            # 互相覆写 —— 实测 CLI 打印「已创建任务」rc=0，随后被 GUI 的 undo
            # 静默抹掉。
            from .filelock import FileLock

            with FileLock(self.path):
                snapshot = self._undo.pop()
                self.tasks = {}
                for item in snapshot["data"]:
                    t = BackupTask.from_dict(item)
                    self.tasks[t.id] = t
                # 回退是"用旧快照覆盖"，同样先合并：否则撤销一次保存会顺手抹掉
                # 期间外部（CLI/守护进程）新增的任务。锁已在手 → 用锁内重读版本。
                self._merge_from_disk_locked()
                data = self._snapshot()
                save_json_atomic(self.path, data)
                self._after_write()
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
                # 显式"整体替换"是用户的删除意图：先记下全部现有 id，避免 save 前的
                # 三方合并把磁盘上被替换掉的旧任务又吸回来（导入后仍存在的 id 会被撤销）。
                self._deleted_ids |= set(self.tasks)
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
