"""端到端压缩冒烟测试脚本（开发期使用）。"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig  # noqa: E402
from backupstudio.engine import TaskEngine  # noqa: E402
from backupstudio.tools import find_7z, find_rar  # noqa: E402


def make_tree(root: Path, n_files: int = 40) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "notes.txt").write_text("BackupStudio 测试数据\n" * 400, encoding="utf-8")
    sub = root / "sub"
    (sub / "deep").mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        (sub / f"file_{i:03d}.bin").write_bytes(bytes(range(256)) * (50 + i * 10))
    (sub / "deep" / "x.log").write_text("log line\n" * 5000, encoding="utf-8")
    (root / "empty").mkdir(exist_ok=True)


def case(engine: TaskEngine, tmp: Path, label: str, **opt_kwargs) -> None:
    src = tmp / "data"
    if not src.exists():
        make_tree(src)
    dest = tmp / f"backups-{label}"
    options = ArchiveOptions(**opt_kwargs)
    task = BackupTask(name=f"t-{label}", sources=[SourceConfig(str(src))], destination=str(dest), options=options)
    res = engine.run(task)
    status = res.record.status
    files = sorted(p.name for p in dest.iterdir()) if dest.exists() else []
    print(f"{label:<22} {status:<8} vols={res.record.volumes} size={res.record.archive_bytes:<10} verify={res.record.verification:<7} {files[:4]}{'...' if len(files) > 4 else ''}")
    if status != "success":
        print("   ", (res.record.errors or [res.message])[:2])


def main() -> int:
    print("7z:", find_7z())
    print("rar:", find_rar())
    tmp = Path(tempfile.mkdtemp(prefix="bs-e2e-"))
    engine = TaskEngine()
    try:
        case(engine, tmp, "7z-plain", fmt="7z")
        case(engine, tmp, "7z-aes", fmt="7z", password="s3cret!", multivolume="7z-split", volume_value=20, volume_unit="KB")
        case(engine, tmp, "7z-split", fmt="7z", multivolume="7z-split", volume_value=15, volume_unit="KB")
        case(engine, tmp, "zip-plain", fmt="zip")
        case(engine, tmp, "zip-zipcrypto", fmt="zip", password="pw", encrypt_mode="zipcrypto")
        case(engine, tmp, "zip-split", fmt="zip", multivolume="split", volume_value=15, volume_unit="KB")
        case(engine, tmp, "tar", fmt="tar")
        case(engine, tmp, "tar.gz", fmt="tar.gz")
        case(engine, tmp, "tar.xz", fmt="tar.xz", level=9)
        case(engine, tmp, "tar.zst", fmt="tar.zst")
        case(engine, tmp, "tar.gz-split", fmt="tar.gz", multivolume="split", volume_value=10, volume_unit="KB")
        case(engine, tmp, "rar-forced", fmt="rar", force=True)
        case(engine, tmp, "rar-plain", fmt="rar")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
