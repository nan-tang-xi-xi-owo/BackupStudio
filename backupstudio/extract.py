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


# 解压防护上限（审查 R1-N7：zip 炸弹实测 981× 无阻拦）
_MAX_EXPANSION_RATIO = 1000  # 解压总量 / 压缩包大小 上限
_MAX_TOTAL_BYTES = 100 * 1024**3  # 绝对上限 100 GiB
_MAX_ENTRIES = 500_000


def _check_bomb(archive_size: int, entry_count: int, unpacked: int) -> None:
    if entry_count > _MAX_ENTRIES:
        raise ExtractionError(f"疑似压缩炸弹：条目数 {entry_count} 超限，已中止")
    limit = min(_MAX_TOTAL_BYTES, max(archive_size, 1) * _MAX_EXPANSION_RATIO)
    if unpacked > limit:
        raise ExtractionError(
            f"疑似压缩炸弹：解压总量 {unpacked // (1024**2)} MB 超过上限，已中止"
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
    """.tar.zst：先解出内层 tar，再走安全 tar 解压路径。

    顺序：内置 `zstandard` 模块（项目已作为可选依赖，最稳）→ `zstd` CLI → 7-Zip。
    审查 M22 全格式覆盖时发现：本机没有 zstd CLI 时旧实现直接 `7z x` 整包，7za 只剥掉
    zstd 一层、留下一个同名 ``.tar`` 文件，却返回"已解压"——**假成功**（用户以为恢复完成，
    实际一个成员都没解出来）。所以 7za 那一档现在只在能拿到内层 tar 时才认成功。
    """
    inner = Path(tempfile.mkdtemp(prefix="bs-zst-", dir=str(target))) / "payload.tar"
    try:
        produced = False
        try:  # 1) 内置 zstandard：流式解压，带膨胀上限（压缩炸弹防护不因这层失守）
            import zstandard as _zstd
        except ImportError:
            _zstd = None
        if _zstd is not None:
            limit = min(_MAX_TOTAL_BYTES, max(archive.stat().st_size, 1) * _MAX_EXPANSION_RATIO)
            dctx = _zstd.ZstdDecompressor()
            written = 0
            with open(archive, "rb") as fh, open(inner, "wb") as out:
                try:
                    # read_across_frames：多帧拼接的 .zst（split 后再压缩之类）也要读全，
                    # 否则只取第一帧 → 内层 tar 被截断 → 解出半个包
                    reader = dctx.stream_reader(fh, read_across_frames=True)
                except TypeError:  # pragma: no cover - 老版本 zstandard 不认这个关键字
                    reader = dctx.stream_reader(fh)
                # 读到 EOF 才会校验帧内 checksum（与 M18 校验口径一致：读透才检得出损坏）
                with reader as src:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limit:
                            raise ExtractionError(
                                f"疑似压缩炸弹：解压总量 {written // (1024**2)} MB 超过上限，已中止"
                            )
                        out.write(chunk)
            produced = True
        if not produced:
            zstd = shutil.which("zstd")
            if zstd:  # 2) 外部 zstd
                code, out = _run([zstd, "-d", "-q", "-o", str(inner), str(archive)])
                if code != 0:
                    raise ExtractionError(f"zstd 解压失败：{out}")
                produced = True
        if not produced:  # 3) 7-Zip 兜底，并把留下的内层 tar 续解（否则就是假成功）
            seven = find_7z()
            if not seven:
                raise ExtractionError("需要 zstandard / zstd 或 7-Zip 才能解压 .tar.zst")
            code, out = _run([seven, "x", "-y", f"-o{inner.parent}", str(archive)])
            if code != 0:
                raise ExtractionError(f"7-Zip 解压失败（退出码 {code}）：{out}")
            cands = [p for p in inner.parent.glob("*.tar") if p.is_file()]
            if not cands:
                raise ExtractionError(
                    "7-Zip 解压 .tar.zst 后未得到内层 tar（可能只剥了一层），已中止以避免假成功"
                )
            inner = cands[0]
        _extract_tar(inner, target)
        return "tar"
    finally:
        shutil.rmtree(inner.parent, ignore_errors=True)


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
