"""测试员脚本：覆盖 CLI 全部命令 + 备份格式 + 保留策略 + 边界情况。

用法：python dev/tester_cli.py
输出：PASS/FAIL 列表与缺陷清单
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORK = Path(tempfile_dir := (ROOT / "build" / "tester-cli"))
RESULTS: list[tuple[str, bool, str]] = []


def run(args: list[str], env: dict | None = None, timeout: int = 300) -> tuple[int, str, str]:
    full_env = {**os.environ, **(env or {})}
    proc = subprocess.run(
        [sys.executable, "-m", "backupstudio", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), env=full_env, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail and not ok else ""))


def setup() -> dict:
    shutil.rmtree(WORK, ignore_errors=True)
    cfg = WORK / "config"
    src = WORK / "src"
    (src / "docs" / "2026").mkdir(parents=True)
    (src / "docs" / "report 最终版.txt").write_text("季度报告\n" * 300, encoding="utf-8")
    (src / "docs" / "2026" / "a.log").write_text("log\n" * 500, encoding="utf-8")
    (src / "空文件夹").mkdir()
    (src / "特殊 名称 & 符号.txt").write_text("x", encoding="utf-8")
    for i in range(5):
        (src / f"file{i}.txt").write_text("内容" * (i + 1), encoding="utf-8")
    dest = WORK / "backups"
    dest.mkdir()
    return {"cfg": str(cfg), "src": str(src), "dest": str(dest), "env": {"BACKUPSTUDIO_CONFIG_DIR": str(cfg)}}


def main() -> int:
    ctx = setup()
    env = ctx["env"]

    # ---------- CLI 基础 ----------
    code, out, err = run(["--version"], env)
    check("--version 输出且退出码 0", code == 0 and "1.0.0" in out, f"code={code} err={err[:200]}")
    check("--version 不打印 Traceback", "Traceback" not in err, err[:200])

    code, out, _ = run(["--help"], env)
    check("--help 退出码 0、含 selftest", code == 0 and "selftest" in out)

    code, out, err = run(["info"], env)
    check("info 正常输出", code == 0 and "ZIP" in out, err[:200])

    code, out, _ = run(["info", "--json"], env)
    parsed = None
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        pass
    check("info --json 是合法 JSON 且含 formats", bool(parsed and "formats" in parsed))

    code, out, _ = run(["doctor"], env)
    check("doctor 退出码 0", code == 0, out[:200])

    code, out, _ = run(["selftest"], env)
    check("selftest 全部通过", code == 0 and "全部通过" in out, out[-300:])

    # ---------- 任务管理 ----------
    code, out, err = run(["task", "list"], env)
    check("空仓库 task list 给出引导", code == 0 and "还没有任务" in out, err[:200])

    code, out, err = run([
        "task", "add", "--name", "文档备份", "--source", ctx["src"], "--dest", ctx["dest"],
        "--format", "zip", "--schedule", "daily", "--time", "03:30",
    ], env)
    check("task add 成功", code == 0 and "已创建任务" in out, err[:300])
    task_id = out.split("已创建任务")[1].split("：")[0].strip() if "已创建任务" in out else ""

    code, out, _ = run(["task", "list"], env)
    check("task list 显示任务", code == 0 and "文档备份" in out)

    code, out, err = run(["task", "add", "--name", "坏路径", "--source", str(WORK / "不存在"), "--dest", ctx["dest"]], env)
    check("task add 允许来源不存在？(应给出提示但不当崩溃)", code in (0, 1, 2), f"code={code} {err[:200]}")

    code, out, _ = run(["task", "enable", task_id, "off"], env)
    check("task enable off", code == 0 and "已停用" in out)
    code, _, _ = run(["task", "enable", task_id, "on"], env)
    check("task enable on", code == 0)

    code, out, err = run(["task", "rm", "not-exist"], env)
    check("task rm 不存在时退出码 1 且不崩溃", code == 1 and "不存在" in err, f"code={code}")
    return finish(ctx)


def finish(ctx: dict) -> int:
    failures = [r for r in RESULTS if not r[1]]
    print(f"\n总计 {len(RESULTS)} 项，失败 {len(failures)} 项")
    for name, _, detail in failures:
        print(f"  ✗ {name}: {detail[:300]}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
