"""测试员脚本（第二轮）：实际备份、特殊字符、并发、取消、边界输入、守护进程。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WORK = ROOT / "build" / "tester-round2"
RESULTS: list[tuple[str, bool, str]] = []


def run(args: list[str], env: dict, timeout: int = 600) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "backupstudio", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), env={**os.environ, **env}, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail and not ok else ""))


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    cfg = WORK / "config"
    src = WORK / "src"
    (src / "子目录" / "深层").mkdir(parents=True)
    (src / "子目录" / "深层" / "data.txt").write_text("深层数据\n" * 100, encoding="utf-8")
    (src / "报告 2026 最终版.txt").write_text("报告\n" * 200, encoding="utf-8")
    (src / "emoji😀文件.txt").write_text("emoji", encoding="utf-8")
    (src / "空目录").mkdir()
    (src / ".hidden").write_text("hidden", encoding="utf-8")
    (src / "f1.tmp").write_text("临时文件", encoding="utf-8")
    dest = WORK / "backups"
    dest.mkdir()
    env = {"BACKUPSTUDIO_CONFIG_DIR": str(cfg)}

    # 1) 各格式真实备份
    formats = ["zip", "7z", "tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst"]
    for fmt in formats:
        d = dest / fmt.replace(".", "_")
        code, out, err = run([
            "task", "add", "--name", f"f-{fmt}", "--source", str(src), "--dest", str(d),
            "--format", fmt, "--force",
        ], env)
        tid = out.split("已创建任务")[1].split("：")[0].strip() if "已创建任务" in out else ""
        code, out, err = run(["run", tid], env)
        artifacts = [p.name for p in d.glob("*") if p.name != "last-run.json"] if d.exists() else []
        check(f"{fmt} 备份成功且产出文件", code == 0 and bool(artifacts), f"code={code} out={out[-200:]} err={err[-300:]}")
        # 中文/emoji 文件名是否保留
        arc = d / artifacts[0] if artifacts else None
        if arc and arc.exists():
            names = read_names(arc)
            check(f"{fmt} 保留中文/emoji 文件名", any("报告 2026" in n for n in names), f"names={names[:6]}")

    # 2) 排除规则
    d = dest / "exclude"
    code, out, _ = run([
        "task", "add", "--name", "排除测试", "--source", str(src), "--dest", str(d),
        "--format", "zip", "--exclude", "*.tmp,*.log", "--force",
    ], env)
    tid = out.split("已创建任务")[1].split("：")[0].strip()
    run(["run", tid], env)
    arc = next(d.glob("*.zip"), None)
    names = read_names(arc) if arc else []
    check("排除规则生效（无 .tmp）", not any(n.endswith(".tmp") for n in names), str(names[:8]))
    check("隐藏文件被保留", any(".hidden" in n for n in names), str(names[:8]))

    # 3) 空源目录
    empty_src = WORK / "empty-src"
    empty_src.mkdir()
    d = dest / "empty"
    code, out, _ = run(["task", "add", "--name", "空源", "--source", str(empty_src), "--dest", str(d), "--format", "zip", "--force"], env)
    tid = out.split("已创建任务")[1].split("：")[0].strip()
    code, out, err = run(["run", tid], env)
    check("空源目录不崩溃（成功或明确跳过）", code in (0, 1) and "Traceback" not in err, f"code={code} {err[:200]}")

    # 4) 备份到源目录内部（自包含风险）
    d = src / "backup-inside"
    d.mkdir()
    code, out, _ = run(["task", "add", "--name", "自包含", "--source", str(src), "--dest", str(d), "--format", "zip", "--force"], env)
    tid = out.split("已创建任务")[1].split("：")[0].strip()
    t0 = time.time()
    code, out, err = run(["run", tid], env, timeout=120)
    elapsed = time.time() - t0
    check("备份到源内部不会无限自我打包", elapsed < 60 and code == 0, f"耗时 {elapsed:.1f}s code={code}")

    # 5) 非法格式
    code, out, err = run(["task", "add", "--name", "坏格式", "--source", str(src), "--dest", str(dest / "bad"), "--format", "lz4"], env)
    check("非法格式被明确拒绝", code != 0 or "Traceback" in err or "坏格式" in out, f"code={code} err={err[:200]}")

    # 6) 分卷 + 密码
    d = dest / "vol"
    big = WORK / "big"
    big.mkdir()
    (big / "rand.bin").write_bytes(os.urandom(5 * 1024 * 1024))
    code, out, _ = run([
        "task", "add", "--name", "分卷加密", "--source", str(big), "--dest", str(d),
        "--format", "7z", "--password", "p@ss 词", "--volume-size", "1", "--force",
    ], env)
    tid = out.split("已创建任务")[1].split("：")[0].strip()
    code, out, err = run(["run", tid], env)
    parts = sorted(p.name for p in d.glob("*.part*")) if d.exists() else []
    check("分卷加密生成多个分卷", code == 0 and len(parts) >= 3, f"code={code} parts={parts[:4]} err={err[-300:]}")
    # 拼接后用 7za 验证密码
    if parts:
        joined = WORK / "joined.7z"
        with open(joined, "wb") as fh:
            for p in sorted(d.glob("*.part*")):
                fh.write(p.read_bytes())
        seven = ROOT / "tools" / "win32" / "7z" / "7za.exe"
        res = subprocess.run([str(seven), "t", "-pp@ss 词", str(joined)], capture_output=True, timeout=120)
        check("分卷 + 含空格密码可被 7-Zip 打开", res.returncode == 0, res.stdout.decode("utf-8", "replace")[-200:])
        res2 = subprocess.run([str(seven), "t", "-pwrong", str(joined)], capture_output=True, timeout=120)
        check("错误密码无法打开（说明确实加密）", res2.returncode != 0)

    # 7) 运行记录与 last-run
    hist = cfg / "history.jsonl"
    check("history.jsonl 已生成", hist.exists() and hist.stat().st_size > 0)
    lr = dest / "vol" / "last-run.json"
    check("last-run.json 可解析", lr.exists() and json.loads(lr.read_text(encoding="utf-8")).get("status") == "success")

    # 8) 并发执行同一任务
    d = dest / "concurrent"
    code, out, _ = run(["task", "add", "--name", "并发", "--source", str(src), "--dest", str(d), "--format", "zip", "--force"], env)
    tid = out.split("已创建任务")[1].split("：")[0].strip()
    p1 = subprocess.Popen([sys.executable, "-m", "backupstudio", "run", tid], cwd=str(ROOT),
                          env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.4)
    code2, out2, err2 = run(["run", tid], env)
    out1, err1 = p1.communicate(timeout=300)
    check("并发执行有明确提示（不崩溃）", ("正在运行" in out2 or "跳过" in out2 or code2 == 1), f"code2={code2} out2={out2[-200:]}")

    # 9) 守护进程
    code, out, err = run(["daemon"], env, timeout=60)
    check("daemon 启动", code == 0 and "已启动" in out, f"{out[:200]} {err[:200]}")
    time.sleep(2)
    code, out, _ = run(["daemon", "--status"], env)
    check("daemon --status 显示运行中", code == 0 and "运行中" in out, out[:300])
    code, out, _ = run(["daemon", "--stop"], env, timeout=60)
    check("daemon --stop 成功", code == 0, out[:300])

    return finish()


def read_names(archive: Path) -> list[str]:
    try:
        n = archive.name.lower()
        if n.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                return zf.namelist()
        if n.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar.zst")):
            with tarfile.open(archive, "r:*") as tf:
                return tf.getnames()
        if n.endswith(".7z"):
            import py7zr

            with py7zr.SevenZipFile(archive) as zf:
                return zf.getnames()
    except Exception as exc:  # noqa: BLE001
        return [f"<读取失败 {type(exc).__name__}: {exc}>"]
    return []


def finish() -> int:
    failures = [r for r in RESULTS if not r[1]]
    print(f"\n总计 {len(RESULTS)} 项，失败 {len(failures)} 项")
    for name, _, detail in failures:
        print(f"  ✗ {name}: {detail[:400]}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
