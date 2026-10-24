"""并发写安全与持久化容错测试（对应审查 R4）。

覆盖 4 组行为：

1. 多进程并发 ``save_json_atomic``：唯一临时名 + fsync + replace 重试，
   要求 0 次失败且最终文件始终是合法 JSON（旧实现同 tmp 名交错写会产出
   「两份 JSON 拼接」的坏文件）；
2. 瞬态 OSError 不误判损坏：replace/open 第一次抛错后恢复 → 操作成功、
   文件未被改名成 .corrupt-*；持续抛错 → load 抛出不改名（调用方兜底）；
3. 坏字节（0xff）尽力恢复：结构可解析则不抛不改名；彻底损坏才归档；
4. 连续两次损坏 → 两个 .corrupt-* 归档并存（同秒也不互相覆盖）。

Windows spawn 注意：worker 必须是模块顶层函数。
"""

from __future__ import annotations

import builtins
import json
import multiprocessing
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backupstudio.config import load_json, save_json_atomic  # noqa: E402


# --------------------------------------------------------------------------
# multiprocessing worker（顶层函数，spawn 可 pickle）
# --------------------------------------------------------------------------

def _spawn_worker(path_str: str, iterations: int, tag: int) -> int:
    """子进程：并发写同一文件 iterations 次，返回抛 OSError 的次数。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from backupstudio.config import save_json_atomic as save

    pad = "x" * 2048  # 加大负载，提升旧实现交错写的检出概率
    fails = 0
    for i in range(iterations):
        try:
            save(path_str, {"tag": tag, "seq": i, "pad": pad})
        except OSError:
            fails += 1
    return fails


# --------------------------------------------------------------------------
# 1) 并发写
# --------------------------------------------------------------------------

class TestConcurrentSave:
    def test_concurrent_writers_no_failure_no_corruption(self, tmp_path):
        target = tmp_path / "tasks.json"
        procs, iters = 4, 200
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=procs) as pool:
            result = pool.starmap_async(
                _spawn_worker, [(str(target), iters, i) for i in range(procs)]
            )
            try:
                fails = result.get(timeout=120)
            except multiprocessing.TimeoutError:  # pragma: no cover - 防挂起
                pool.terminate()
                pytest.fail("并发写入超时（>120s）")
        assert fails == [0] * procs, f"save_json_atomic 抛错次数: {fails}"
        data = json.loads(target.read_text(encoding="utf-8"))  # 最终文件必须是合法 JSON
        assert data["tag"] in range(procs)
        assert data["pad"] == "x" * 2048
        # 不遗留临时文件
        assert list(tmp_path.glob("*.tmp")) == []


# --------------------------------------------------------------------------
# 2) 瞬态 OSError 不误判损坏
# --------------------------------------------------------------------------

class TestTransientOSError:
    def test_replace_transient_error_is_retried(self, tmp_path, monkeypatch):
        """os.replace 第一次抛 PermissionError 后恢复 → save 成功，不动 .corrupt。"""
        target = tmp_path / "tasks.json"
        save_json_atomic(target, {"v": 1})

        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("transient lock (simulated)")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", flaky_replace)
        save_json_atomic(target, {"v": 2})  # 不应抛出

        assert calls["n"] >= 2
        assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}
        assert list(tmp_path.glob("*.corrupt*")) == []
        assert list(tmp_path.glob("*.tmp")) == []

    def test_open_transient_error_during_load_recovers(self, tmp_path, monkeypatch):
        """load 时 open 第一次抛 PermissionError 后恢复 → 正常读到内容，不改名。"""
        target = tmp_path / "tasks.json"
        save_json_atomic(target, {"v": 1})

        real_open = builtins.open
        calls = {"n": 0}

        def flaky_open(file, *args, **kwargs):
            if str(file) == str(target):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError("transient read lock (simulated)")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", flaky_open)
        assert load_json(target, {"fallback": True}) == {"v": 1}
        assert target.exists()
        assert list(tmp_path.glob("*.corrupt*")) == []

    def test_persistent_open_error_raises_and_keeps_file(self, tmp_path, monkeypatch):
        """持续 OSError → 抛出（调用方 store.py 兜底），绝不改名 .corrupt。"""
        target = tmp_path / "tasks.json"
        save_json_atomic(target, {"v": 1})

        real_open = builtins.open

        def blocked_open(file, *args, **kwargs):
            if str(file) == str(target):
                raise PermissionError("persistent lock (simulated)")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", blocked_open)
        with pytest.raises(OSError):
            load_json(target, {"fallback": True})

        assert target.exists()
        assert json.loads(target.read_bytes().decode("utf-8")) == {"v": 1}
        assert list(tmp_path.glob("*.corrupt*")) == []


# --------------------------------------------------------------------------
# 3) 坏字节（0xff）尽力恢复
# --------------------------------------------------------------------------

class TestBadBytes:
    def test_recoverable_bad_byte_not_archived(self, tmp_path):
        """0xff 坏字节在字符串里（JSON 结构完好）→ errors=replace 恢复，不抛不改名。"""
        target = tmp_path / "tasks.json"
        target.write_bytes(b'{"tasks": [], "note": "\xff bad byte"}')

        data = load_json(target, {"fallback": True})
        assert data["tasks"] == []
        assert "bad byte" in data["note"]  # 坏字节被替换字符顶上，其余完好
        assert target.exists()
        assert list(tmp_path.glob("*.corrupt*")) == []

    def test_hopeless_corruption_archived_with_timestamp(self, tmp_path):
        """截断 + 坏字节 → 结构无法解析 → 归档为 .corrupt-<ts> 后返回默认值。"""
        target = tmp_path / "tasks.json"
        target.write_bytes(b'{"tasks": [\xff')

        data = load_json(target, {"fallback": True})
        assert data == {"fallback": True}
        assert not target.exists()
        archives = list(tmp_path.glob("tasks.json.corrupt-*"))
        assert len(archives) == 1
        assert archives[0].read_bytes() == b'{"tasks": [\xff'  # 原始字节完整保留


# --------------------------------------------------------------------------
# 4) 二次损坏并存
# --------------------------------------------------------------------------

class TestRepeatedCorruption:
    def test_two_corruptions_keep_both_archives(self, tmp_path):
        target = tmp_path / "tasks.json"

        target.write_bytes(b'{"tasks": [\xff')
        load_json(target, {})

        target.write_bytes(b'\xff\xff definitely not json')
        load_json(target, {})

        archives = sorted(tmp_path.glob("tasks.json.corrupt-*"))
        assert len(archives) == 2, f"应并存两份损坏备份，实际: {[p.name for p in archives]}"
        assert not target.exists()
