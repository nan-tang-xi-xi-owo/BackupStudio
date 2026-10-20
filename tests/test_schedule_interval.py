"""interval 调度恒真修复的回归测试（E1 §2 重点③ / E5 §1.5 / E6 §2a）。

背景：修复前 `latest_expected` 的 interval 分支返回 `last_run + interval`
（恒大于 last_run）→ `due()` 对 interval 任务恒真 → daemon(15s)/GUI(20s)
每 tick 连轴执行（P0 级资源灾难，C-g16）。

修复：未到点返回 `last_run`（due 为 False），到点/过点返回 `due_at`
（due 为 True），首跑返回 `now`（due 为 True，行为不变）。

注意：E5 §1.5 已实测警告——"字面修复返回 last_run"会让 due() 恒假
（永不触发）。本文件的断言必须双向覆盖：未到点为 False、到期为 True。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from backupstudio.config import BackupTask, ScheduleConfig
from backupstudio.schedule import due, latest_expected, next_run

NOW = datetime(2026, 1, 1, 12, 0, 0)


def _interval_task(minutes: int = 60) -> BackupTask:
    return BackupTask(name="t", schedule=ScheduleConfig(type="interval", minutes=minutes))


class TestIntervalDue:
    """interval 任务 due() 的双向断言（核心 bug 回归）。"""

    def test_not_yet_due_returns_false(self):
        """核心断言：10 分钟前跑过、间隔 60 分钟 → 未到点，不触发。

        修复前该断言为 True（每 tick 连轴执行）。
        """
        task = _interval_task(60)
        last = NOW - timedelta(minutes=10)
        assert due(task, NOW, last) is False

    def test_overdue_returns_true(self):
        """61 分钟前跑过、间隔 60 分钟 → 已过点，触发。"""
        task = _interval_task(60)
        last = NOW - timedelta(minutes=61)
        assert due(task, NOW, last) is True

    def test_exactly_due_returns_true(self):
        """正好 60 分钟前跑过 → 到点，触发（边界含等号）。"""
        task = _interval_task(60)
        last = NOW - timedelta(minutes=60)
        assert due(task, NOW, last) is True

    def test_first_run_returns_true(self):
        """首次启用（last_run=None）→ 立即触发（"启用即跑"行为不变）。"""
        task = _interval_task(60)
        assert due(task, NOW, None) is True

    def test_long_overdue_still_true(self):
        """历史修复防退化：1440 分钟前跑过（错过多个周期）→ 仍触发一次。"""
        task = _interval_task(60)
        last = NOW - timedelta(minutes=1440)
        assert due(task, NOW, last) is True


class TestIntervalLatestExpected:
    """latest_expected 对 interval 不得返回未来时刻（E1-G16 实测断言）。"""

    def test_not_due_returns_no_future_time(self):
        task = _interval_task(60)
        last = NOW - timedelta(minutes=10)
        expected = latest_expected(task.schedule, NOW, last)
        assert expected is not None
        assert expected <= NOW
        assert expected == last  # 未到点：返回 last_run（不是下一次时刻）

    def test_due_returns_due_at(self):
        task = _interval_task(60)
        last = NOW - timedelta(minutes=61)
        assert latest_expected(task.schedule, NOW, last) == last + timedelta(minutes=60)

    def test_due_consistent_with_next_run(self):
        """due == True ⇔ next_run <= now（E6 §2a 的正确性条件）。

        describe_next 基于 next_run：due 为 True 时显示"即将执行"，
        为 False 时显示"X 分钟后"，修复后语义一致。
        """
        task = _interval_task(60)
        for last in (None, NOW - timedelta(minutes=10), NOW - timedelta(minutes=60), NOW - timedelta(minutes=61)):
            nxt = next_run(task.schedule, NOW, last)
            assert nxt is not None
            assert due(task, NOW, last) is (nxt <= NOW)


class TestOtherScheduleTypesUnaffected:
    """对照：修复不得波及 daily/weekly/monthly 的 due 语义。"""

    def test_daily_unchanged(self):
        task = BackupTask(name="t", schedule=ScheduleConfig(type="daily", time="02:00"))
        # 昨天 02:00 跑过、今天 03:00 已过点 → 触发
        assert due(task, datetime(2026, 1, 1, 3, 0), datetime(2025, 12, 31, 2, 0)) is True
        # 今天 02:00 已跑过 → 不触发
        assert due(task, datetime(2026, 1, 1, 3, 0), datetime(2026, 1, 1, 2, 0)) is False

    def test_weekly_unchanged(self):
        # 每周一 02:00；2026-01-05 是周一，now=周二 03:00
        task = BackupTask(name="t", schedule=ScheduleConfig(type="weekly", time="02:00", days=[0]))
        now = datetime(2026, 1, 6, 3, 0)
        # 上次跑了上一个周一 → 本应 2026-01-05 02:00 的这次还没跑 → 触发
        assert due(task, now, datetime(2025, 12, 29, 2, 0)) is True
        # 本周一 02:00 已跑过 → 不触发
        assert due(task, now, datetime(2026, 1, 5, 2, 0)) is False

    def test_monthly_unchanged(self):
        # 每月 15 日 01:00；now=2026-01-20 12:00
        task = BackupTask(name="t", schedule=ScheduleConfig(type="monthly", day=15, time="01:00"))
        now = datetime(2026, 1, 20, 12, 0)
        assert due(task, now, datetime(2025, 12, 15, 1, 0)) is True
        assert due(task, now, datetime(2026, 1, 15, 1, 0)) is False
