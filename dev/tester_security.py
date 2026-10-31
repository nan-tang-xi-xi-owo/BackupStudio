"""测试员脚本（安全性/稳健性）：损坏配置、恶意导入、解压路径穿越、错误密码、超大数量文件。"""

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
WORK = ROOT / "build" / "tester-security"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail and not ok else ""))


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    cfg = WORK / "config"
    cfg.mkdir(parents=True)
    env = {**os.environ, "BACKUPSTUDIO_CONFIG_DIR": str(cfg)}

    def run(args: list[str], timeout: int = 300) -> tuple[int, str, str]:
        p = subprocess.run([sys.executable, "-m", "backupstudio", *args], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    # 1) 损坏的 tasks.json
    (cfg / "tasks.json").write_text("{ this is not json", encoding="utf-8")
    code, out, err = run(["task", "list"])
    check("损坏的配置不会崩溃", code == 0 and "Traceback" not in err, f"code={code} {err[-200:]}")
    check("损坏配置被备份为 .corrupt", any(p.name.endswith(".corrupt") for p in cfg.glob("*")))
    code, _, _ = run(["task", "add", "--name", "恢复后", "--source", str(ROOT / "backupstudio"), "--dest", str(WORK / "d")])
    check("损坏后可重新创建任务", code == 0)

    # 2) 恶意导入：路径穿越 / 超大数 / 缺字段
    evil = {
        "tasks": [
            {"name": "穿越", "destination": "../../Windows/System32", "sources": [{"path": "C:\\Windows"}]},
            {"name": "缺字段"},
            {"level": 999, "options": {"fmt": "unknown", "level": -5, "volume_value": "abc"}},
            "不是字典",
        ]
    }
    evil_file = WORK / "evil.json"
    evil_file.write_text(json.dumps(evil, ensure_ascii=False), encoding="utf-8")
    code, out, err = run(["task", "list"])
    # 通过 TaskStore 直接导入，检查稳健性
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'.');"
         "from backupstudio.store import TaskStore;"
         f"s=TaskStore(r'{cfg / 'tasks.json'}'); print('imported', s.import_bundle(r'{evil_file}'))"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    check("恶意/畸形导入不崩溃", probe.returncode == 0 and "Traceback" not in probe.stderr,
          (probe.stdout + probe.stderr)[-400:])

    # 3) 解压路径穿越（zip slip）
    from backupstudio.extract import extract_archive

    evil_zip = WORK / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../../pwned.txt", "x")
        zf.writestr("normal.txt", "ok")
    target = WORK / "extract-target"
    ok, msg = extract_archive(evil_zip, target)
    escaped = (WORK / "pwned.txt").exists() or (WORK.parent.parent / "pwned.txt").exists()
    check("ZIP 路径穿越被阻止或未逃逸目标目录", not escaped, f"ok={ok} msg={msg}")

    evil_tar = WORK / "evil.tar"
    with tarfile.open(evil_tar, "w") as tf:
        info = tarfile.TarInfo("../../pwned2.txt")
        data = b"x"
        info.size = len(data)
        import io

        tf.addfile(info, io.BytesIO(data))
    ok2, msg2 = extract_archive(evil_tar, WORK / "extract-target2")
    escaped2 = (WORK / "pwned2.txt").exists()
    check("tar 路径穿越被阻止或未逃逸目标目录", not escaped2, f"ok={ok2} msg={msg2}")

    # 4) 解压错误密码
    src = WORK / "src"
    src.mkdir()
    (src / "a.txt").write_text("秘密数据" * 100, encoding="utf-8")
    code, out, _ = run(["task", "add", "--name", "加密", "--source", str(src), "--dest", str(WORK / "enc"),
                        "--format", "7z", "--password", "rightpw", "--force"])
    tid = out.split("已创建任务")[1].split("：")[0].strip()
    code, out, err = run(["run", tid])
    arc = next((WORK / "enc").glob("*.7z"), None)
    ok3, msg3 = extract_archive(arc, WORK / "dec", "wrongpw") if arc else (True, "no archive")
    check("错误密码解压失败且给出提示", not ok3 and "失败" in msg3, f"{msg3[:200]}")
    ok4, msg4 = extract_archive(arc, WORK / "dec2", "rightpw") if arc else (False, "")
    extracted = list((WORK / "dec2").rglob("a.txt"))
    check("正确密码可解压", ok4 and bool(extracted), f"{msg4[:200]} files={[str(p) for p in extracted]}")

    # 5) 大量小文件（性能与稳定性）
    many = WORK / "many"
    many.mkdir()
    for i in range(1500):
        (many / f"f{i:04d}.txt").write_text(str(i), encoding="utf-8")
    code, out, _ = run(["task", "add", "--name", "海量小文件", "--source", str(many), "--dest", str(WORK / "many-out"),
                        "--format", "tar.gz", "--force"])
    tid = out.split("已创建任务")[1].split("：")[0].strip()
    t0 = time.time()
    code, out, err = run(["run", tid], timeout=600)
    dt = time.time() - t0
    check(f"1500 个小文件备份正常（耗时 {dt:.1f}s）", code == 0 and "Traceback" not in err, err[-300:])

    failures = [r for r in RESULTS if not r[1]]
    print(f"\n总计 {len(RESULTS)} 项，失败 {len(failures)} 项")
    for name, _, detail in failures:
        print(f"  ✗ {name}: {detail[:400]}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
