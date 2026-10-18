"""调度计划：纯函数，便于测试与 CLI/守护进程共用。"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta

from .config import BackupTask, ScheduleConfig


def _next_time(base: datetime, hh: int, mm: int) -> datetime:
    cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= base:
        cand += timedelta(days=1)
    return cand


def parse_hhmm(text: str) -> tuple[int, int]:
    try:
        parts = str(text).split(":")
        return int(parts[0]) % 24, int(parts[1]) % 60
    except (ValueError, IndexError):
        return 2, 0


def latest_expected(schedule: ScheduleConfig, now: datetime, last_run: datetime | None) -> datetime | None:
    """最近一次「本应发生」的计划时间点（用于触发判定）。"""
    if schedule.type == "manual":
        return None
    if schedule.type == "interval":
        anchor = last_run or (now - timedelta(minutes=schedule.minutes))
        if last_run is None:
            return anchor
        return last_run + timedelta(minutes=schedule.minutes)
    hh, mm = parse_hhmm(schedule.time)
    if schedule.type == "daily":
        today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if today > now:
            today -= timedelta(days=1)
        return today
    if schedule.type == "weekly":
        days = schedule.days or [0, 1, 2, 3, 4, 5, 6]
        for back in range(0, 8):
            day = now - timedelta(days=back)
            if day.weekday() in days:
                cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if cand <= now:
                    return cand
        return None
    if schedule.type == "monthly":
        year, month = now.year, now.month
        cand = datetime(year, month, min(schedule.day, calendar.monthrange(year, month)[1]), hh, mm)
        if cand <= now:
            return cand
        prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
        return datetime(prev_y, prev_m, min(schedule.day, calendar.monthrange(prev_y, prev_m)[1]), hh, mm)
    return None


def next_run(schedule: ScheduleConfig, now: datetime, last_run: datetime | None) -> datetime | None:
    """下一次计划执行时间（仅用于展示）。"""
    if schedule.type == "manual":
        return None
    if schedule.type == "interval":
        if last_run is None:
            return now
        return last_run + timedelta(minutes=schedule.minutes)
    hh, mm = parse_hhmm(schedule.time)
    if schedule.type == "daily":
        return _next_time(now, hh, mm)
    if schedule.type == "weekly":
        days = sorted(schedule.days or [])
        if not days:
            return None
        for ahead in range(0, 8):
            day = now + timedelta(days=ahead)
            if day.weekday() in days:
                cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if cand > now:
                    return cand
        return None
    if schedule.type == "monthly":
        for ahead in range(0, 62):
            day = now + timedelta(days=ahead)
            if day.day == schedule.day:
                cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if cand > now:
                    return cand
        return None
    return None


def due(task: BackupTask, now: datetime, last_run: datetime | None) -> bool:
    """任务此刻是否应当触发。"""
    if not task.enabled or task.schedule.type == "manual":
        return False
    if last_run is not None and last_run >= now:
        return False
    expected = latest_expected(task.schedule, now, last_run)
    if expected is None:
        return False
    if last_run is None:
        # 首次启用：daily/weekly/monthly 只在过点后的一个宽限期内触发，避免开机就补跑一堆
        if task.schedule.type in ("daily", "weekly", "monthly"):
            grace = timedelta(hours=2)
            if expected >= now - grace:
                return True
            # 未在宽限期内：首次只挂载下一次
            return False
        return True
    return expected > last_run


def parse_ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def describe_next(task: BackupTask, now: datetime | None = None) -> str:
    now = now or datetime.now()
    nxt = next_run(task.schedule, now, parse_ts(task.last_run))
    if nxt is None:
        return "—"
    delta = nxt - now
    if delta.total_seconds() <= 0:
        return "即将执行"
    if delta < timedelta(hours=1):
        return f"{nxt:%H:%M}（{int(delta.total_seconds() // 60)} 分钟后）"
    if delta < timedelta(days=2):
        return f"{nxt:%m-%d %H:%M}"
    return f"{nxt:%Y-%m-%d %H:%M}"
