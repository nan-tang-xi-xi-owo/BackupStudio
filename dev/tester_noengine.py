"""模拟「干净环境」：系统里没有任何 7-Zip / RAR，验证所有强制压缩路径。

CI（GitHub runner）就是这个环境，本机因为有随包 7za 会掩盖问题。
用法：python dev/tester_noengine.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 在导入引擎前就把外部工具「藏起来」
import backupstudio.archives as archives  # noqa: E402
import backupstudio.tools as tools  # noqa: E402
from backupstudio import engine as engine_mod  # noqa: E402

tools.find_7z = lambda: None  # type: ignore[assignment]
tools.find_rar = lambda: None  # type: ignore[assignment]
tools.find_unrar = lambda: None  # type: ignore[assignment]
archives.find_7z = lambda: None  # type: ignore[assignment]
archives.find_rar = lambda: None  # type: ignore[assignment]
archives.rar_available = lambda: False  # type: ignore[assignment]
archives.py7zr  # 确认可用

WORK = ROOT / "build" / "noengine"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail and not ok else ""))


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    cfg = WORK / "config"
    os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(cfg)
    src = WORK / "src"
    (src / "子目录").mkdir(parents=True)
    (src / "子目录" / "报告😀.txt").write_text("数据\n" * 200, encoding="utf-8")
    (src / "blob.bin").write_bytes(os.urandom(3 * 1024 * 1024))

    from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
    from backupstudio.engine import TaskEngine

    engine = TaskEngine()
    cases = [
        ("zip 强制（无 7-Zip）", "zip", {}, zipfile.is_zipfile),
        ("7z 强制（无 7-Zip）", "7z", {}, None),
        ("rar 强制（无 RAR）", "rar", {}, zipfile.is_zipfile),
        ("tar.zst", "tar.zst", {}, None),
        ("zip 加密强制", "zip", {"password": "pw", "encrypt_mode": "zipcrypto"}, zipfile.is_zipfile),
    ]
    for label, fmt, extra, validator in cases:
        dest = WORK / f"out-{fmt}-{len(extra)}"
        task = BackupTask(
            name="干净环境", sources=[SourceConfig(str(src))], destination=str(dest),
            options=ArchiveOptions(fmt=fmt, force=True, **extra),
        )
        result = engine.run(task)
        status = result.record.status
        arc = Path(result.record.archive) if result.record.archive else None
        ok = status == "success" and arc is not None and arc.exists()
        detail = "" if ok else f"status={status} errors={result.record.errors[:1]}"
        if ok and validator is not None:
            try:
                ok = bool(validator(arc))
                detail = f"容器校验失败：{arc.name}"
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
        if ok and fmt== "7z":
            import py7zr

            with py7zr.SevenZipFile(arc) as zf:
                names = zf.getnames()
            ok = any("报告" in n for n in names)
            detail = "" if ok else f"7z 内容异常：{names}"
        check(label, ok, detail)

    # 分层验证：zip 产出必须是 ZIP 且含中文名
    zdir = next(WORK.glob("out-zip-0"))
    zz = next(zdir.glob("*.zip"))
    with zipfile.ZipFile(zz) as zf:
        names = zf.namelist()
    check("强制 ZIP 含中文/emoji 文件名", any("报告😀" in n for n in names), str(names[:5]))

    failures = [r for r in RESULTS if not r[1]]
    print(f"\n总计 {len(RESULTS)} 项，失败 {len(failures)} 项")
    for name, _, detail in failures:
        print(f"  ✗ {name}: {detail[:400]}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
