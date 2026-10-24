"""解压模块加固。

安全要点：
1. **路径穿越（zip-slip / tar-slip）**：任何成员若解析后落在目标目录之外，一律拒绝；
2. **符号链接攻击**：先解到临时目录，再整体搬入目标，避免链接指向外部后被后续条目写入；
3. 兼容：7z/rar 由外部程序解压，解压后同样做一次「越界文件」扫描并清理；
4. 通用分卷（产品 split 产物 ``X.zip.part01/02/…``）：先按**数值卷序**流式拼接成整包，
   再按内层主名分发到上述 zip/tar 安全解压路径——拼接只是"还原本来的包"，防护不绕过。
   （审查 M22 / W-A4：产品能造分卷却不能拆，CLI 直接报"不支持的压缩格式"。）
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from .tools import find_7z, find_unrar

log = logging.getLogger("backupstudio.extract")


class ExtractionError(RuntimeError):
    pass


# 通用切片分卷名：``<内层主名>.partNN``（NN 为数字；产品写 ``part01``，卷数 >99 时自然变宽）
_PART_VOLUME_RE = re.compile(r"^(?P<base>.+)\.part(?P<num>\d+)$", re.IGNORECASE)

# 拼接后按内层名分发时用得到的容器后缀（产品 split 的主名必然带格式扩展名：
# zip/7z/rar/tar/tar.gz/tar.bz2/tar.xz/tar.zst）。后缀不认识就不拼接，直接沿用原有
# 分发——省得对无关的 ``xxx.part01`` 文件做一遍整包写入的无用功。
_SPLIT_CONTAINER_SUFFIXES = (
    ".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
    ".tar.xz", ".txz", ".tar.zst",
)


# 解压防护上限（审查 A4）。
#
# 旧实现把比例判定写成 ``min(100 GiB, 包大小 × 1000)`` 的**硬上限**，会拒绝本产品自己产出的
# 正常备份：8 MiB 全零日志用 deflate 压到 8 KB = 1014×，用户灾难恢复时被自己的防护挡住，
# 且没有任何逃生阀（实测见 raw/bomb_self.txt）。
#
# 新策略：**只以绝对上限为主**，比例仅作为「比绝对上限更松的兜底」而不参与收紧：
#   * _MAX_TOTAL_BYTES      —— 解压总量绝对上限（默认 100 GiB），可通过
#                              ``--max-unpacked-bytes`` / BACKUPSTUDIO_MAX_UNPACKED_BYTES 放宽；
#   * _MAX_EXPANSION_RATIO  —— 默认 10000（而非 1000）：正常备份几乎不可能超过，
#                              且可通过 BACKUPSTUDIO_MAX_UNPACK_RATIO 放宽到 0 = 关闭比例判定；
#   * _MAX_ENTRIES          —— 条目数上限，可通过 BACKUPSTUDIO_MAX_ENTRIES 放宽。
_MAX_EXPANSION_RATIO = 10000  # 解压总量 / 压缩包大小（仅在未设绝对上限时兜底）
_MAX_TOTAL_BYTES = 100 * 1024**3  # 绝对上限 100 GiB
_MAX_ENTRIES = 500_000

# 环境变量名（GUI/CLI 都可设；CLI 还提供 --max-unpacked-bytes 覆盖）
ENV_MAX_UNPACKED_BYTES = "BACKUPSTUDIO_MAX_UNPACKED_BYTES"
ENV_MAX_UNPACK_RATIO = "BACKUPSTUDIO_MAX_UNPACK_RATIO"
ENV_MAX_ENTRIES = "BACKUPSTUDIO_MAX_ENTRIES"


def _env_int(name: str, default: int) -> int:
    """读环境变量里的非负整数；非法值退回默认（不因用户笔误把防护关掉/炸掉）。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return val if val >= 0 else default


def _limits() -> tuple[int, int, int]:
    """当前生效的 (绝对上限字节, 比例, 条目数上限)。0 表示该项不限制。"""
    total = _env_int(ENV_MAX_UNPACKED_BYTES, _MAX_TOTAL_BYTES)
    ratio = _env_int(ENV_MAX_UNPACK_RATIO, _MAX_EXPANSION_RATIO)
    entries = _env_int(ENV_MAX_ENTRIES, _MAX_ENTRIES)
    return total, ratio, entries


def unpack_limit(archive_size: int) -> int:
    """解压总量允许的上限（字节）。0 = 不限制。

    **绝对上限优先**：只有在没设绝对上限（<=0）时才用比例兜底。这样默认行为是
    「100 GiB 以内一律放行」，比例只防"小包解出 TB"的极端情形。
    """
    total, ratio, _ = _limits()
    if total > 0:
        return total
    if ratio > 0:
        return max(archive_size, 1) * ratio
    return 0


def _check_bomb(archive_size: int, entry_count: int, unpacked: int) -> None:
    total, ratio, max_entries = _limits()
    if max_entries > 0 and entry_count > max_entries:
        raise ExtractionError(
            f"疑似压缩炸弹：条目数 {entry_count} 超过上限 {max_entries}，已中止。"
            f"（确认无误可设 {ENV_MAX_ENTRIES} 放宽，或 0 关闭）"
        )
    limit = unpack_limit(archive_size)
    if limit <= 0:
        return
    if unpacked > limit:
        hint = (
            f"（确认无误可设 {ENV_MAX_UNPACKED_BYTES} 放宽，或 0 关闭绝对上限、"
            f"用 {ENV_MAX_UNPACK_RATIO} 调节比例）"
        )
        raise ExtractionError(
            f"疑似压缩炸弹：解压总量 {unpacked // (1024**2)} MB 超过上限 "
            f"{limit // (1024**2)} MB，已中止。{hint}"
        )


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


def _with_scc_utf8(argv: list[str]) -> list[str]:
    """给 7-Zip 系可执行文件插入 `-sccUTF-8`（子命令之后、其它参数之前）。

    中文 Windows 上 7za 默认按 GBK 输出错误信息，而本模块按 UTF-8 解码 → 用户看到
    乱码（审查 W-A3-2 / M2b）。实测 `-sccUTF-8` 可让 7za 全量输出 UTF-8。
    UnRAR / zstd 等不支持该开关，按可执行文件名排除；重复调用幂等。
    """
    if not (sys.platform == "win32" and len(argv) >= 2):
        return argv
    if "-sccUTF-8" in argv or not Path(argv[0]).name.lower().startswith("7z"):
        return argv
    return [argv[0], argv[1], "-sccUTF-8", *argv[2:]]


def _run(argv: list[str], timeout: int = 3600) -> tuple[int, str]:
    # stdin 隔离：7za 在无密码读加密归档时会等待交互输入（实测挂起至 timeout——
    # 审查 T8）；DEVNULL 让它立即失败（rc=255）。
    argv = _with_scc_utf8(argv)
    kwargs: dict = {"capture_output": True, "timeout": timeout, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(argv, **kwargs)
    except OSError as exc:
        return -1, str(exc)
    out = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    return proc.returncode, out.strip()[-800:]


def _extract_zip(archive: Path, target: Path, password: str | None = None) -> None:
    """ZIP 解压。有密码时优先 7-Zip（AES 支持），退化到 zipfile 的 ZipCrypto。"""
    try:
        archive_size = archive.stat().st_size
    except OSError:
        archive_size = 0
    if password:
        exe = find_7z()
        if exe:
            # 审查 F：这条外部解压路径此前**零防护**（加密 ZIP 炸弹会照写满盘）
            _check_external_bomb(exe, archive, password)
            argv = [exe, "x", "-y", f"-o{target}", str(archive)]
            argv.insert(3, f"-p{password}")
            code, out = _run(argv)
            if code != 0:
                raise ExtractionError(f"7-Zip 解压加密 ZIP 失败（退出码 {code}）：{out}")
            return
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        declared_total = sum(i.file_size for i in infos)
        _check_bomb(archive_size, len(infos), declared_total)
        unpacked = 0
        for info in infos:
            dest = target / _safe_zip_name(info.filename)
            if not _is_within(target, dest):
                raise ExtractionError(f"压缩包内包含不安全路径，已拒绝解压：{info.filename}")
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                src_fh = zf.open(info, pwd=password.encode("utf-8") if password else None)
            except RuntimeError as exc:
                # AES 条目：zipfile 不支持（NotImplementedError/RuntimeError）
                raise ExtractionError(
                    f"无法解压加密条目（{info.filename}）：AES ZIP 需要安装 7-Zip（{exc}）"
                ) from exc
            with src_fh, open(dest, "wb") as out:
                while True:
                    chunk = src_fh.read(1 << 20)
                    if not chunk:
                        break
                    unpacked += len(chunk)
                    _check_bomb(archive_size, len(infos), unpacked)
                    out.write(chunk)


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
        try:
            archive_size = archive.stat().st_size
        except OSError:
            archive_size = 0
        _check_bomb(archive_size, len(members), sum(m.size for m in members if m.isfile()))
        # Python 3.12+ 支持 filter；3.14+ 默认 data 过滤器，这里显式指定更稳妥
        try:
            tf.extractall(target, members=members, filter="data")  # type: ignore[arg-type]
        except TypeError:  # pragma: no cover - 老版本 Python
            tf.extractall(target, members=members)


def _declared_unpacked_7z(exe: str, archive: Path, password: str | None) -> int | None:
    """用 ``7z l -slt`` 预读 7z 的**声明**解压总量。

    返回 None = 读不出来（不是错误：调用方按"无法预检"处理，仍受超时与后续防护约束）。
    ``-slt`` 输出形如::

        Path = a.bin
        Size = 1048576
        Packed Size = 1024

    只取 ``Size`` 行并求和；目录条目不出现 ``Size``，自然不计入。

    注意：``-slt``/``-ba`` 是 **7-Zip 的参数**，UnRAR 不认（会报 ``Unknown option: ba``
    并返回 None → 预检静默放行）。rar 必须走 :func:`_declared_unpacked_unrar`。
    """
    argv = [exe, "l", "-slt", "-ba", str(archive)]
    if password:
        argv.insert(2, f"-p{password}")
    return _sum_size_lines(argv)


def _declared_unpacked_unrar(exe: str, archive: Path, password: str | None) -> int | None:
    """用 UnRAR 的 ``lt``（列表技术信息）预读 rar 的声明解压总量。

    UnRAR 没有 ``-slt``/``-ba``，必须用 ``lt``。安静标志要用 ``-idc``（不显示注释），
    **不能用 ``-idq``**——``-idq`` 会把列表本身也压掉（实测输出为空，解析必然是 None）。

    实测输出形如::

        Archive: bomb.rar
        Details: RAR 5

                Name: zeros.bin
                Type: File
                Size: 4194304
         Packed size: 188
               Ratio: 0%
               CRC32: 1147406A

    只取 ``Size:``（注意 ``Packed size:`` 是小写 s 且前缀不同，不会误匹配）。
    返回 None = 读不出来（调用方**必须拒绝放行**，不能让预检形同虚设）。
    """
    argv = [exe, "lt", "-idc", str(archive)]
    if password:
        argv.insert(2, f"-p{password}")
    kwargs: dict = {"capture_output": True, "timeout": 300, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(argv, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):  # unrar lt 对"某些警告"返回 1，仍带完整列表
        return None
    out = (proc.stdout or b"").decode("utf-8", "replace")
    total = 0
    seen = False
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            seen = True
            continue
        if not line.startswith("Size:"):
            continue
        try:
            total += int(line[len("Size:"):].strip())
        except ValueError:
            continue
    return total if seen else None


def _sum_size_lines(argv: list[str]) -> int | None:
    """跑 ``argv`` 并把输出里 ``Size =`` 行的数字求和；跑不起来/非 0 退出返回 None。"""
    kwargs: dict = {"capture_output": True, "timeout": 300, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(argv, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or b"").decode("utf-8", "replace")
    total = 0
    for line in out.splitlines():
        line = line.strip()
        # 精确匹配 "Size ="，避免把 "Packed Size =" 也算进来
        if line.startswith("Size ="):
            try:
                total += int(line[len("Size ="):].strip())
            except ValueError:
                continue
    return total


def _count_entries_7z(exe: str, archive: Path, password: str | None) -> int | None:
    """用 ``7z l -slt`` 数条目数（每段一个 ``Path = ``）。

    用于条目数上限（审查 A4 的 ``max_entries`` 此前是 ``pass`` 空壳）。
    """
    argv = [exe, "l", "-slt", "-ba", str(archive)]
    if password:
        argv.insert(2, f"-p{password}")
    kwargs: dict = {"capture_output": True, "timeout": 300, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(argv, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or b"").decode("utf-8", "replace")
    return sum(1 for line in out.splitlines() if line.strip().startswith("Path ="))


def _check_external_bomb(
    exe: str,
    archive: Path,
    password: str | None = None,
    *,
    unrar: bool = False,
) -> None:
    """外部解压前的**绝对上限**预检（审查 A4：此前 7z/rar 完全没有炸弹防护）。

    只信"绝对上限"（默认 100 GiB，可用 BACKUPSTUDIO_MAX_UNPACKED_BYTES 放宽/关闭）：

    * 声明值超限 → 直接拒绝（不必真解一遍把磁盘写满）；
    * 声明值可读且未超限 → 放行（7z 解压后仍受 timeout 约束）；
    * 读不出来 → **拒绝放行**（审查 E：此前静默 return，预检形同虚设）。
      加密包确实读不出大小，调用方用 ``password`` 传对密码即可；
      若仍读不出，宁可让用户显式放宽上限，也不默认放行未预检的解压。

    ``unrar=True`` 时用 ``unrar lt`` 解析（UnRAR 不认 7z 的 ``-slt``）。
    """
    _, _, max_entries = _limits()
    if unrar:
        declared = _declared_unpacked_unrar(exe, archive, password)
        entries = None
    else:
        declared = _declared_unpacked_7z(exe, archive, password)
        entries = _count_entries_7z(exe, archive, password)
    if declared is None:
        raise ExtractionError(
            f"无法预读 {archive.name} 的解压后大小，拒绝在未预检的情况下解压。"
            f"（加密包请先提供正确密码；确认可信可设 {ENV_MAX_UNPACKED_BYTES}=0 关闭上限）"
        )
    if max_entries > 0 and entries is not None and entries > max_entries:
        raise ExtractionError(
            f"疑似压缩炸弹：条目数 {entries} 超过上限 {max_entries}，已中止。"
            f"（确认无误可设 {ENV_MAX_ENTRIES} 放宽，或 0 关闭）"
        )
    limit = unpack_limit(archive.stat().st_size if archive.exists() else 0)
    if limit > 0 and declared > limit:
        raise ExtractionError(
            f"疑似压缩炸弹：解压后总量 {declared // (1024**2)} MB 超过上限 "
            f"{limit // (1024**2)} MB，已中止。"
            f"（确认无误可设 {ENV_MAX_UNPACKED_BYTES} 放宽，或 0 关闭）"
        )


def _extract_external(archive: Path, target: Path, password: str | None) -> str:
    name = archive.name.lower()
    if name.endswith((".7z", ".001")):
        exe = find_7z()
        if not exe:
            raise ExtractionError("需要 7-Zip 才能解压 .7z")
        _check_external_bomb(exe, archive, password)
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
            # UnRAR 不认 7z 的 -slt/-ba，必须走 lt（审查 E：此前解析失败被静默放行）
            _check_external_bomb(exe, archive, password, unrar=True)
            argv = [exe, "x", "-idq", "-y", str(archive), str(target) + os.sep]
            if password:
                argv.insert(3, f"-p{password}")
            code, out = _run(argv)
            if code != 0:
                raise ExtractionError(f"RAR 解压失败（退出码 {code}）：{out}")
            return "rar"
        seven = find_7z()
        if seven:
            _check_external_bomb(seven, archive, password)
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
    """.tar.zst：先解出内层 tar，再走安全 tar 解压路径（两条修复是互补的，缺一即错）。

    顺序：内置 `zstandard` 模块（项目已作为可选依赖，最稳）→ `zstd` CLI → 7-Zip。

    融合的两条独立缺陷：
    * 上游 M22 —— **假成功**：本机没有 zstd CLI 时旧实现直接 `7z x` 整包，7za 只剥掉
      zstd 一层、留下一个同名 ``.tar`` 文件，却返回"已解压"（用户以为恢复完成，实际
      一个成员都没解出来）。因此任何一档都**必须真正拿到内层 tar 并解出成员**才算成功。
    * 本分支 F —— **零防护**：这条路径此前没有任何炸弹检查。单纯加
      ``_check_external_bomb`` 不够 —— 实测 7-Zip 对 ``.tar.zst`` 只报告外层流
      （``Method = header-open-only ... unknown-content-size``），``7z l -slt`` 的
      ``Size`` 是空值 → 预检读到 0 → 放行。所以内置解压与 7z 回退都改成
      **边写边计数**，超限立即中止。

    另外 ``read_across_frames=True``（上游）保证多帧拼接的 .zst 也被读全，否则内层 tar
    被截断 → 解出半个包。
    """
    # 1) 首选：进程内 zstandard 流式解压（能精确计数，且不额外起进程）
    try:
        import zstandard
    except ImportError:
        zstandard = None
    if zstandard is not None:
        limit = unpack_limit(archive.stat().st_size if archive.exists() else 0)
        unpacked = 0
        with tempfile.TemporaryDirectory(dir=str(target)) as tmp:
            tar_path = Path(tmp) / "payload.tar"
            try:
                with open(archive, "rb") as src_fh, open(tar_path, "wb") as out_fh:
                    try:
                        # read_across_frames：多帧拼接的 .zst 也要读全，否则内层 tar 被截断
                        reader = zstandard.ZstdDecompressor().stream_reader(
                            src_fh, read_across_frames=True
                        )
                    except TypeError:  # pragma: no cover - 老版本不认这个关键字
                        reader = zstandard.ZstdDecompressor().stream_reader(src_fh)
                    with reader as src:
                        while True:
                            chunk = src.read(1 << 20)
                            if not chunk:
                                break
                            unpacked += len(chunk)
                            if limit > 0 and unpacked > limit:
                                raise ExtractionError(
                                    f"疑似压缩炸弹：解压后总量超过上限 {limit // (1024**2)} MB，已中止。"
                                    f"（确认无误可设 {ENV_MAX_UNPACKED_BYTES} 放宽，或 0 关闭）"
                                )
                            out_fh.write(chunk)
            except ExtractionError:
                raise
            except Exception as exc:  # noqa: BLE001 - zstd 帧损坏等（校验和错也要报出来）
                raise ExtractionError(f"zstd 解压失败：{type(exc).__name__}: {exc}") from exc
            if tar_path.stat().st_size == 0:
                raise ExtractionError("zstd 解压后内层 tar 为空，已中止以避免假成功")
            _extract_tar(tar_path, target)
        return "tar"

    # 2) 外部 zstd CLI：同样必须先拿到内层 tar 再解（不能只剥一层就报成功）
    zstd = shutil.which("zstd")
    if zstd:
        with tempfile.TemporaryDirectory(dir=str(target)) as tmp:
            tar_path = Path(tmp) / "payload.tar"
            code, out = _run([zstd, "-d", "-q", "-o", str(tar_path), str(archive)])
            if code != 0:
                raise ExtractionError(f"zstd 解压失败：{out}")
            if not tar_path.is_file() or tar_path.stat().st_size == 0:
                raise ExtractionError(
                    "zstd 解压 .tar.zst 后未得到内层 tar，已中止以避免假成功"
                )
            _extract_tar(tar_path, target)
        return "tar"

    # 3) 7-Zip 回退：无法预读大小（外层是 zstd 流），改为 `e -so` 流式解压 + 实时计数。
    seven = find_7z()
    if seven:
        limit = unpack_limit(archive.stat().st_size if archive.exists() else 0)
        _stream_extract_7z(seven, archive, target, limit)
        return "7z"
    raise ExtractionError("需要 zstandard / zstd 或 7-Zip 才能解压 .tar.zst")


def _stream_extract_7z(exe: str, archive: Path, target: Path, limit: int) -> None:
    """用 ``7z e -so archive`` 把内容流式写到 ``target``，边写边计数，超限即杀进程。

    用于 7-Zip 无法给出条目清单/大小的场合（.tar.zst 外层是 zstd 流）。
    staging 目录由调用方清理，所以中止时不会留下"半包"污染最终目录。
    """
    import subprocess as _sp

    kwargs: dict = {
        "stdout": _sp.PIPE,
        "stdin": _sp.DEVNULL,
        "stderr": _sp.PIPE,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(_sp, "CREATE_NO_WINDOW", 0)
    proc = _sp.Popen([exe, "e", "-so", "-y", str(archive)], **kwargs)
    unpacked = 0
    # 7z e -so 会把 tar 流原样输出；这里先把整个 tar 写到临时文件，再走安全 tar 解压。
    tmp_tar = target / ".bs-stream.tar"
    try:
        with open(tmp_tar, "wb") as out_fh:
            while True:
                chunk = proc.stdout.read(1 << 20)
                if not chunk:
                    break
                unpacked += len(chunk)
                if limit > 0 and unpacked > limit:
                    proc.kill()
                    proc.wait(timeout=10)
                    raise ExtractionError(
                        f"疑似压缩炸弹：解压后总量超过上限 {limit // (1024**2)} MB，已中止。"
                        f"（确认无误可设 {ENV_MAX_UNPACKED_BYTES} 放宽，或 0 关闭）"
                    )
                out_fh.write(chunk)
        code = proc.wait(timeout=60)
        if code != 0:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()[-400:]
            raise ExtractionError(f"7-Zip 解压失败（退出码 {code}）：{err}")
        _extract_tar(tmp_tar, target)
    finally:
        with contextlib.suppress(OSError):
            tmp_tar.unlink()


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

    # 通用切片分卷（X.zip.partNN）：先按数值卷序拼接成整包，再按内层名递归分发
    # （审查 M22：产品 split 产物此前直接报"不支持的压缩格式"——能造不能拆）
    split_result = _extract_split(archive, target, password)
    if split_result is not None:
        return split_result

    # zip / tar：先解到暂存目录，检查链接与越界后再合并（防 zip-slip 与链接逃逸）
    if name.endswith(".zip") or name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar.zst")):
        staging = Path(tempfile.mkdtemp(prefix="bs-extract-", dir=str(target)))
        try:
            if name.endswith(".zip"):
                _extract_zip(archive, staging, password)
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


def _split_group(archive: Path) -> tuple[str, list[tuple[int, Path]], list[tuple[int, Path]]] | None:
    """``X.zip.part07`` → ("X.zip", [(1, …part01), …], [(1, …part1 重复卷, …)])；非分卷名返回 None。

    卷序按**数值**排序（审查 W-A4 §三：`part100` 必须排在 `part11` 之后，字典序必错）。
    同组判定用主名全等（大小写不敏感），因此 `a.zip.part01` 不会把 `a-backup.zip.part01`
    的卷混进来。返回 None 表示这个文件不是 `.partNN` 命名（7z 原生 `.001` 与 rar 原生
    `xxx.part1.rar` 都不以 `.partNN` 结尾，因此仍走原有外部程序路径）。

    同一编号出现多个文件时（用户找回了 ``part02`` 却没删掉改名的 ``part2``），只取**产品口径**
    的那个（补零到 2 位），其余作为重复卷返回并提示——两份都拼进去必得"压缩包损坏"式的误导。
    """
    m = _PART_VOLUME_RE.match(archive.name)
    if not m:
        return None
    base = m.group("base")
    by_num: dict[int, list[Path]] = {}
    try:
        siblings = sorted(archive.parent.iterdir())
    except OSError:  # 目录不可读（权限/网络异常）：至少把手上这卷算进去，交给后续缺卷判定
        siblings = [archive]
    lowered = base.lower()
    for p in siblings:
        if not p.is_file():
            continue
        mm = _PART_VOLUME_RE.match(p.name)
        if mm and mm.group("base").lower() == lowered:
            by_num.setdefault(int(mm.group("num")), []).append(p)
    if not by_num:  # pragma: no cover - iterdir 成功时理论上一定能匹配到自己
        by_num = {int(m.group("num")): [archive]}
    volumes: list[tuple[int, Path]] = []
    dupes: list[tuple[int, Path]] = []
    for num in sorted(by_num):
        cands = by_num[num]
        canonical = f"{base}.part{num:02d}".lower()
        chosen = next((c for c in cands if c.name.lower() == canonical), cands[0])
        volumes.append((num, chosen))
        dupes.extend((num, c) for c in cands if c != chosen)
    return base, volumes, dupes


def _join_split(volumes: list[tuple[int, Path]], joined: Path) -> None:
    """按卷序流式拼接整包（1 MiB 一块，不把整包读进内存；审查 W-A4 §四：RSS 仅 +11MB）。"""
    with open(joined, "wb") as out:
        for _, p in volumes:
            with open(p, "rb") as fh:
                shutil.copyfileobj(fh, out, 1 << 20)


def _extract_split(archive: Path, target: Path, password: str | None) -> tuple[bool, str] | None:
    """通用切片分卷（``X.<ext>.partNN``）：完整性检查 → 目标同卷临时目录拼接 → 按内层名解压。

    返回 None 表示该文件不是分卷命名（交回原有分发）。拼接只是"还原本来的那一个包"，
    之后仍按内层主名走 zip/tar/7z/rar 的安全路径，zip-slip / 链接逃逸 / 压缩炸弹防护
    一律不绕（审查 M22：产品能造分卷却不能拆）。
    """
    group = _split_group(archive)
    if group is None:
        return None
    base, volumes, dupes = group
    if base in (".", "..") or os.sep in base or (os.altsep and os.altsep in base):
        # 内层主名不是普通文件名（`..part01` 之类），拼过去等于往父目录写——直接交回原有分发
        return None
    if not base.lower().endswith(_SPLIT_CONTAINER_SUFFIXES):
        # 内层主名不是我们能分发的容器（浏览器残留、视频分段之类）。直接交回原有分发，
        # 别为了一个打不开的名字先把整包拼出来——那等于白占一份磁盘峰值（审查 W-A4 §四）。
        return None
    note = ""
    if dupes:
        chosen_by_num = {n: p.name for n, p in volumes}
        pairs = ", ".join(f"{c.name}（与 {chosen_by_num.get(num, '?')} 重复）" for num, c in dupes[:3])
        note = f"（注意：同编号卷重复，已按产品命名取用：{pairs}）"
    try:
        result = _join_and_extract(base, volumes, archive, target, password)
        if result is not None and note:
            ok, msg = result
            msg = f"{msg} {note}" if ok else f"{msg}{note}"
            return ok, msg
        return result
    except ExtractionError as exc:
        return False, f"{exc}{note}"
    except OSError as exc:
        return False, f"分卷处理失败（磁盘空间或权限问题）：{exc}{note}"
    except Exception as exc:  # noqa: BLE001 与 extract_archive 尾部一致：不把 traceback 抛给用户
        return False, f"{type(exc).__name__}: {exc}{note}"


def _join_and_extract(base: str, volumes: list[tuple[int, Path]], archive: Path,
                      target: Path, password: str | None) -> tuple[bool, str]:
    if volumes[0][0] != 1:
        listed = ", ".join(p.name for _, p in volumes[:5])
        more = "…" if len(volumes) > 5 else ""
        return False, (
            f"分卷组不完整：缺少 part01（现有卷：{listed}{more}）"
            f"——请从第一卷开始选择，或找回缺失的首卷"
        )
    nums = [n for n, _ in volumes]
    expected = max(nums)
    missing_count = expected - len(nums)  # volumes 已按编号去重，差值即缺卷数
    if missing_count > 0:
        own = _PART_VOLUME_RE.match(archive.name)
        # 补零位数按用户所选卷报名（part01 / part001）；上限 6 位，避免离谱编号撑出超长串
        pad = min(6, max(2, len(own.group("num")) if own else 2))
        # **惰性**取前几个缺号：`range(1, expected+1)` 可能极大（part01 + part1000000
        # 这种名字巧合），列表化会把内存/时间打爆——所以这里用 islice，绝不物化。
        head = list(itertools.islice((n for n in range(1, expected + 1) if n not in set(nums)), 6))
        listed = ", ".join(f"part{n:0{pad}d}" for n in head)
        more = " 等" if missing_count > len(head) else ""
        return False, (
            f"分卷缺失：{listed}{more}（共缺 {missing_count} 卷）——请找回被移动/删除的卷后重试"
        )
    sizes = [p.stat().st_size for _, p in volumes]
    total_size = sum(sizes)
    # 产品的通用切片除最后一卷外必然等大。不等**不一定**是坏包（用户也可能用 split -C
    # 之类切出非等大卷，按数值序拼接依然正确），所以这里不 fail-closed，只在失败时把
    # "卷大小不一致"作为归因提示附上——tar 系没有内容校验和（审查 W-A4 §一），
    # 截断/换过的卷最容易表现成"解出一堆看不对的东西"，这句提示能省掉一整轮排查。
    uneven = [volumes[i][1].name for i in range(len(sizes) - 1) if len(sizes) > 1 and sizes[i] != sizes[0]]
    size_hint = (
        f"（提示：{', '.join(uneven[:3])} 与首卷大小不同——请确认这些卷是否完整、是否属于同一组分卷）"
        if uneven else ""
    )
    try:
        free = shutil.disk_usage(str(target)).free
    except OSError:
        free = -1
    if 0 <= free < total_size:
        # 审查 W-A4 §四：拼接峰值 = 整包一份落在目标盘。目标盘连一份整包都放不下时提前拒绝，
        # 而不是写到一半 ENOSPC（只卡"绝对放不下"，留足正常波动余量以免误报）。
        return False, (
            f"目标磁盘空间不足：拼接 {len(volumes)} 个分卷需要 {total_size // (1024 * 1024) + 1} MB "
            f"临时空间（剩余 {free // (1024 * 1024)} MB）——请把 --to 指到空间充足的磁盘后重试"
        )
    staging = Path(tempfile.mkdtemp(prefix="bs-join-", dir=str(target)))
    try:
        joined = staging / base
        _join_split(volumes, joined)
        # 以内层真名递归分发（zip / tar* / 7z / rar 全覆盖）；finally 清理拼接产物
        ok, msg = extract_archive(joined, target, password)
        if not ok:
            extra = ""
            if len(volumes) == 1:
                # 产品 split 至少产 2 卷：目录里只找到 1 卷时，几乎必然是"其它卷还没找回来"
                extra = "（本目录仅匹配到 1 卷：若这是分卷组的一部分，请把同组其它卷放到同一目录后重试）"
            msg = f"{msg}{extra}{size_hint}"
        return ok, msg
    finally:
        # 只删自己写的东西：unlink 拼接件 + 目录为空才 rmdir。用 shutil.rmtree 有个理论坑——
        # 归档若含与暂存目录同名的顶层条目，合并后那目录里已是用户解出的文件，整树删会连成果一起吃掉。
        with contextlib.suppress(OSError):
            joined.unlink()
        with contextlib.suppress(OSError):
            staging.rmdir()


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
