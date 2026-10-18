"""强制压缩 / 7-Zip 不可用时的后备测试。"""

from __future__ import annotations

import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backupstudio import archives  # noqa: E402
from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig  # noqa: E402
from backupstudio.engine import TaskEngine  # noqa: E402


def main() -> int:
    # 模拟「没有 7-Zip」的环境
    archives.find_7z = lambda: None  # type: ignore[assignment]
    archives.rar_available = lambda: False  # type: ignore[assignment]
    tmp = Path(tempfile.mkdtemp(prefix="bs-fb-"))
    try:
        src = tmp / "data"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("hello " * 500, encoding="utf-8")
        (src / "sub" / "b.bin").write_bytes(bytes(range(256)) * 300)
        engine = TaskEngine()
        for label, opts in [
            ("zip-fallback", ArchiveOptions(fmt="zip")),
            ("7z-forced", ArchiveOptions(fmt="7z", force=True)),
            ("7z-forced-aes", ArchiveOptions(fmt="7z", force=True, password="pw123", level=7)),
            ("7z-forced-vol", ArchiveOptions(fmt="7z", force=True, multivolume="7z-split", volume_value=10, volume_unit="KB")),
            ("rar-forced", ArchiveOptions(fmt="rar", force=True)),
            ("7z-no-force", ArchiveOptions(fmt="7z")),
        ]:
            dest = tmp / f"out-{label}"
            task = BackupTask(name=label, sources=[SourceConfig(str(src))], destination=str(dest), options=opts)
            res = engine.run(task)
            files = sorted(p.name for p in dest.glob("*")) if dest.exists() else []
            print(f"{label:<16} {res.record.status:<9} backend={res.record.backend:<16} files={files[:3]} errs={res.record.errors[:1]}")
            # 校验可读性
            main_archive = Path(res.record.archive) if res.record.archive else None
            if main_archive and main_archive.exists():
                try:
                    if main_archive.suffix == ".7z":
                        import py7zr

                        with py7zr.SevenZipFile(main_archive, password=opts.password or None) as z:
                            print("      7z names:", z.getnames()[:3])
                    elif main_archive.suffix in (".zip", ".rar") and not opts.password:
                        with zipfile.ZipFile(main_archive) as z:
                            print("      zip names:", z.namelist()[:3])
                    elif main_archive.suffix.startswith(".tar"):
                        with tarfile.open(main_archive) as tf:
                            print("      tar names:", tf.getnames()[:3])
                except Exception as exc:
                    print("      READ ERROR:", exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
