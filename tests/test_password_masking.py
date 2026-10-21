"""密码脱敏回归（审查 P0-14）：日志/CLI 输出不得泄漏密码。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestMaskArgv:
    def test_mask_separate_flag(self):
        from backupstudio.app import _mask_argv

        assert _mask_argv(["--password", "secret1"]) == ["--password", "***"]
        assert _mask_argv(["--pass", "s"]) == ["--pass", "***"]

    def test_mask_equals_form(self):
        from backupstudio.app import _mask_argv

        assert _mask_argv(["--password=abc123"]) == ["--password=***"]

    def test_non_sensitive_untouched(self):
        from backupstudio.app import _mask_argv

        argv = ["task", "add", "--name", "x", "--format", "7z"]
        assert _mask_argv(argv) == argv


class TestTaskShowHidesPassword:
    def test_show_masks_password(self, tmp_path):
        """CLI task show 不回显明文密码。"""
        env_cfg = tmp_path / "cfg"
        env_cfg.mkdir()
        py = sys.executable
        import os as _os
        env = dict(_os.environ)
        env["BACKUPSTUDIO_CONFIG_DIR"] = str(env_cfg)
        # 建任务
        subprocess.run(
            [py, "-m", "backupstudio", "task", "add", "--name", "pwt", "--source", str(tmp_path),
             "--dest", str(tmp_path / "out"), "--password", "TOPSECRET999"],
            capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=60, encoding="utf-8", errors="replace",
        )
        r = subprocess.run(
            [py, "-m", "backupstudio", "task", "list"], capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
            cwd=str(ROOT), timeout=60,
        )
        # 找到任务 id（列表第一列）
        task_id = None
        for line in r.stdout.splitlines():
            parts = line.split()
            if parts and len(parts[0]) == 12:
                task_id = parts[0]
                break
        assert task_id, f"未解析到 task id：{r.stdout}"
        r2 = subprocess.run(
            [py, "-m", "backupstudio", "task", "show", task_id], capture_output=True, text=True,
            env=env, cwd=str(ROOT), timeout=60, encoding="utf-8", errors="replace",
        )
        assert "TOPSECRET999" not in r2.stdout, "task show 泄漏了明文密码！"
        assert "已隐藏" in r2.stdout or "***" in r2.stdout
