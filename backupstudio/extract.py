"""解压模块加固。

安全要点：
1. **路径穿越（zip-slip / tar-slip）**：任何成员若解析后落在目标目录之外，一律拒绝；
2. **符号链接攻击**：先解到临时目录，再整体搬入目标，避免链接指向外部后被后续条目写入；
3. 兼容：7z/rar 由外部程序解压，解压后同样做一次「越界文件」扫描并清理。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from .tools import find_7z, find_unrar


class ExtractionError(RuntimeError):
    pass


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def _safe_zip_name(name: str) -> str:
    norm = name.replace("\\", "/").lstrip("/")
    if ".." in Path(norm).parts or (len(norm) > 1 and norm[1] == ":"):
        raise ExtractionError(f"压缩包内包含不安全路径，已拒绝解压：{name}")
    return norm


def _run(argv: list[str], timeout: int = 3600) -> tuple[int, str]:
    kwargs: dict = {"capture_output": True, "timeout": timeout}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(argv, **kwargs)
    except OSError as exc:
        return -1, str(exc)
    out = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    return proc.returncode, out.strip()[-800:]


def _extract_zip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            dest = target / _safe_zip_name(info.filename)
            if not _is_within(target, dest):
                raise ExtractionError(f"压缩包内包含不安全路径，已拒绝解压：{info.filename}")
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)


def _extract_tar(archive: Path, target: Path) -> None:
    with tarfile.open(archive, "r:*") as tf:
        members = []
        for m in tf.getmembers():
            dest = target / m.name
            if not _is_within(target, dest):
                raise ExtractionError(f"压缩包内包含不安全路径，已拒绝解压：{m.name}")
            if m.issym() or m.islnk():
                link_target = (dest.parent / m.linkname).resolve()
                if not _is_within(target, link_target):
                    raise ExtractionError(f"压缩包内包含指向外部的链接，已拒绝解压：{m.name} -> {m.linkname}")
            members.append(m)
        # Python 3.12+ 支持 filter；3.14+ 默认 data 过滤器，这里显式指定更稳妥
        try:
            tf.extractall(target, members=members, filter="data")  # type: ignore[arg-type]
        except TypeError:  # pragma: no cover - 老版本 Python
            tf.extractall(target, members=members)


def _extract_external(archive: Path, target: Path, password: str | None) -> str:
    name = archive.name.lower()
    if name.endswith((".7z", ".001")):
        exe = find_7z()
        if not exe:
            raise ExtractionError("需要 7-Zip 才能解压 .7z")
        argv = [exe, "x", "-y", f"-o{target}", str(archive)]
        if password:
            argv.insert(3, f"-p{password}")
        code, out = _run(argv)
        if code != 0:
            raise ExtractionError(f"7-Zip 解压失败（退出码 {code}）：{out}")
        return "7z"
    if name.endswith(".rar"):
        exe = find_unrar()
        if exe:
            argv = [exe, "x", "-idq", "-y", str(archive), str(target) + os.sep]
            if password:
                argv.insert(3, f"-p{password}")
            code, out = _run(argv)
            if code != 0:
                raise ExtractionError(f"RAR 解压失败（退出码 {code}）：{out}")
            return "rar"
        seven = find_7z()
        if seven:
            argv = [seven, "x", "-y", f"-o{target}", str(archive)]
            if password:
                argv.insert(3, f"-p{password}")
            code, out = _run(argv)
            if code != 0:
                raise ExtractionError(f"7-Zip 解压失败（退出码 {code}）：{out}")
            return "7z"
        raise ExtractionError("需要 UnRAR 或 7-Zip 才能解压 .rar")
    raise ExtractionError("不支持的压缩格式")


def _extract_zst_to_dir(archive: Path, target: Path) -> str:
    """.tar.zst：优先用 zstd 解成 tar 再走安全 tar 解压；没有 zstd 时退回 7-Zip。"""
    zstd = shutil.which("zstd")
    if zstd:
        with tempfile.TemporaryDirectory(dir=str(target)) as tmp:
            tar_path = Path(tmp) / "payload.tar"
            code, out = _run([zstd, "-d", "-q", "-o", str(tar_path), str(archive)])
            if code != 0:
                raise ExtractionError(f"zstd 解压失败：{out}")
            _extract_tar(tar_path, target)
        return "tar"
    seven = find_7z()
    if seven:
        code, out = _run([seven, "x", "-y", f"-o{target}", str(archive)])
        if code != 0:
            raise ExtractionError(f"7-Zip 解压失败（退出码 {code}）：{out}")
        return "7z"
    raise ExtractionError("需要 zstd 或 7-Zip 才能解压 .tar.zst")


def _scan_escape(base: Path) -> list[str]:
    """检查目标目录内是否存在指向外部的符号链接。"""
    bad: list[str] = []
    for root, dirs, files in os.walk(base, followlinks=False):
        for name in dirs + files:
            p = Path(root) / name
            if p.is_symlink():
                try:
                    resolved = p.resolve()
                except OSError:
                    continue
                if not _is_within(base, resolved):
                    bad.append(str(p))
    return bad


def extract_archive(archive: Path, target: Path, password: str | None = None) -> tuple[bool, str]:
    """把压缩包安全解压到目录，返回 (成功, 信息)。"""
    archive = Path(archive)
    target = Path(target)
    if not archive.exists():
        return False, f"文件不存在：{archive}"
    target.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()

    def _finish(kind: str) -> tuple[bool, str]:
        escapes = _scan_escape(target)
        if escapes:
            return False, f"压缩包含指向外部的链接，已拒绝：{escapes[:3]}"
        return True, f"已解压到 {target}（{kind}）"

    # zip / tar：先解到暂存目录，检查链接与越界后再合并（防 zip-slip 与链接逃逸）
    if name.endswith(".zip") or name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar.zst")):
        staging = Path(tempfile.mkdtemp(prefix="bs-extract-", dir=str(target)))
        try:
            if name.endswith(".zip"):
                _extract_zip(archive, staging)
                kind = "zip"
            elif name.endswith(".tar.zst"):
                kind = _extract_zst_to_dir(archive, staging)
            else:
                _extract_tar(archive, staging)
                kind = "tar"
            escapes = _scan_escape(staging)
            if escapes:
                raise ExtractionError(f"压缩包含指向外部的链接，已拒绝：{escapes[:3]}")
            _merge_into(staging, target)
            return _finish(kind)
        except ExtractionError as exc:
            return False, str(exc)
        except (zipfile.BadZipFile, tarfile.TarError) as exc:
            return False, f"压缩包损坏或格式不匹配：{exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # 7z / rar：由外部程序直接解到目标目录
    try:
        kind = _extract_external(archive, target, password)
        return _finish(kind)
    except ExtractionError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _merge_into(staging: Path, target: Path) -> None:
    """把暂存目录内容合并到目标（同名文件覆盖）。"""
    for item in staging.iterdir():
        dest = target / item.name
        if item.is_dir() and not item.is_symlink():
            if dest.exists() and not dest.is_dir():
                dest.unlink()
            if dest.is_dir():
                _merge_into(item, dest)
            else:
                shutil.move(str(item), str(dest))
        else:
            if dest.is_dir():
                shutil.rmtree(dest)
            elif dest.exists():
                dest.unlink()
            shutil.move(str(item), str(dest))
