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

from .longpath import long_path
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
    """归一 ZIP 成员名并做安全性校验。

    **越界/盘符仍拒绝整包**（真攻击，不该静默跳过）。
    **Windows 保留名/非法字符则改名放行**（不是攻击，只是跨平台差异）——
    这与 `_extract_tar` 的处理保持一致（审查 Agent 指出：改名逻辑此前只加在
    `_extract_tar`，`_extract_zip` 未覆盖，导致含 `a<b.txt` 的 zip 在 Windows 上
    整包解压失败 `OSError [Errno 22]`，连旁边的正常文件也拿不到）。
    """
    norm = name.replace("\\", "/").lstrip("/")
    # 盘符判定要**严格**：只认 `X:` 或 `X:/…`（真正的绝对路径）。
    # 不能把 `e:f.txt`（文件名里带冒号）当盘符 —— 那是 Windows 非法字符问题，
    # 应该改名放行而不是拒绝整包（审查 Agent 实测：旧判定让含 `e:f.txt` 的
    # zip/tar 整包解压失败，旁边的正常文件也拿不到）。
    is_drive = len(norm) > 1 and norm[1] == ":" and (len(norm) == 2 or norm[2] == "/")
    if ".." in Path(norm).parts or is_drive:
        raise ExtractionError(f"压缩包内包含不安全路径，已拒绝解压：{name}")
    # Windows：逐段改名（保留设备名 / 非法字符 / 结尾空格点）
    if sys.platform == "win32":
        parts = norm.split("/")
        fixed: list[str] = []
        changed = False
        for seg in parts:
            reason = _windows_unsafe_reason(seg)
            if reason:
                fixed.append(_safe_component(seg))
                changed = True
            else:
                fixed.append(seg)
        if changed:
            norm = "/".join(fixed)
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
            argv = [exe, "x", "-y", f"-o{long_path(target)}", str(archive)]
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
        # 大小写冲突预扫（P1，同 tar）
        _case_renames = _dedupe_case([i.filename for i in infos]) if sys.platform == "win32" else {}
        for info in infos:
            _zf_name = info.filename
            if _case_renames and _zf_name in _case_renames:
                _zf_name = _case_renames[_zf_name]
                logging.getLogger(__name__).warning(
                    "解压时遇到大小写冲突，已改名保留：%s → %s", info.filename, _zf_name
                )
            dest = target / _safe_zip_name(_zf_name)
            if not _is_within(target, dest):
                raise ExtractionError(f"压缩包内包含不安全路径，已拒绝解压：{info.filename}")
            if info.is_dir():
                Path(long_path(dest)).mkdir(parents=True, exist_ok=True)
                continue
            Path(long_path(dest.parent)).mkdir(parents=True, exist_ok=True)
            try:
                src_fh = zf.open(info, pwd=password.encode("utf-8") if password else None)
            except RuntimeError as exc:
                # AES 条目：zipfile 不支持（NotImplementedError/RuntimeError）
                raise ExtractionError(
                    f"无法解压加密条目（{info.filename}）：AES ZIP 需要安装 7-Zip（{exc}）"
                ) from exc
            with src_fh, open(long_path(dest), "wb") as out:
                while True:
                    chunk = src_fh.read(1 << 20)
                    if not chunk:
                        break
                    unpacked += len(chunk)
                    _check_bomb(archive_size, len(infos), unpacked)
                    out.write(chunk)


def _extract_tar(archive: Path, target: Path) -> None:
    """解 tar；遇到越界/外部链接时**跳过该成员**，不废掉整包。

    ## 为什么不能整包拒绝（S0，元数据专家实测）

    旧实现对任何 `issym()/islnk()` 成员都做：
        link_target = (dest.parent / m.linkname).resolve()
        if not _is_within(target, link_target): raise

    **绝对路径的 linkname 必然越界**（`dest.parent / "/abs/path"` → `/abs/path`），
    于是一个绝对符号链接就让**整包一个文件都解不出来**。

    这在 Linux/WSL 上极其常见 —— 脚本里 `ln -s $PWD/xxx` 就是绝对链接。
    用户实测：`extract links.tar` → 「包含指向外部的链接，已拒绝解压」→
    连旁边无关的普通文件都没落地。

    ## 修法

    * 越界的**普通文件成员**（tar-slip）：仍然拒绝整包（这是真攻击，且不该静默跳过）
    * 越界的**链接成员**：只跳过该链接，其余照解，并把跳过的清单回报给用户。

    理由：链接本身不写入数据，跳过它不会产生「半截文件」；而整包拒绝会让
    用户拿不到本来完好的数据 —— 安全收益为零、可用性损失极大。
    """
    with tarfile.open(archive, "r:*") as tf:
        members = []
        dropped_links: list[str] = []
        renamed: list[str] = []
        # 只在 Windows 上做保留名/非法字符改名：Linux/macOS 上 `CON`、`a:b` 都是
        # **合法文件名**，改了反而破坏用户的目录结构。
        renamed_enabled = sys.platform == "win32"
        # 大小写冲突预扫（P1）：Windows 上 Readme.txt 与 readme.txt 是同一个文件，
        # 第二个会覆盖第一个 —— 数据静默丢失且 ok=True。先算出改名映射。
        _case_renames = _dedupe_case([m.name for m in tf.getmembers()]) if renamed_enabled else {}
        for m in tf.getmembers():
            dest = target / m.name
            if not _is_within(target, dest):
                raise ExtractionError(f"压缩包内包含不安全路径，已拒绝解压：{m.name}")
            if m.issym() or m.islnk():
                raw = m.linkname
                # 绝对链接：按「它指向包外」处理，跳过但不算攻击
                if raw.startswith(("/", "\\")) or (len(raw) > 1 and raw[1] == ":"):
                    dropped_links.append(f"{m.name} -> {raw}（绝对路径）")
                    continue
                link_target = (dest.parent / raw).resolve()
                if not _is_within(target, link_target):
                    dropped_links.append(f"{m.name} -> {raw}（指向包外）")
                    continue
            # **Windows 保留名 / 非法字符必须在解压前改名**（P0，兼容性 Agent）。
            #
            # 关键：不能等解压出来再处理 —— `open("NUL","w")` 会"成功"但数据被设备
            # 吞掉，staging 里已经是个 0 字节文件了，那时再改名救不回来。
            # 实测（含 5 个保留名的 tar）：CON/NUL/AUX/CON.txt → size=0 数据丢失，
            # normal.txt 连带整包失败。
            #
            # tarfile 允许在 extractall 之前改 `member.name`，就改这里。
            if renamed_enabled:
                parts = m.name.split("/")
                new_parts: list[str] = []
                changed = False
                for seg in parts:
                    reason = _windows_unsafe_reason(seg)
                    if reason:
                        fixed = _safe_component(seg)
                        renamed.append(f"{m.name} → {fixed}（{reason}）")
                        new_parts.append(fixed)
                        changed = True
                    else:
                        new_parts.append(seg)
                if changed:
                    m.name = "/".join(new_parts)
                    # 改名后要重新校验不越界（理论上只会更安全，但保持防御）
                    if not _is_within(target, target / m.name):
                        raise ExtractionError(f"改名后路径异常，已拒绝解压：{m.name}")
            # 应用大小写冲突改名
            if _case_renames:
                new_case = _case_renames.get(m.name)
                if new_case:
                    renamed.append(f"{m.name} → {new_case}（与另一个成员仅大小写不同）")
                    m.name = new_case
            members.append(m)
        try:
            archive_size = archive.stat().st_size
        except OSError:
            archive_size = 0
        _check_bomb(archive_size, len(members), sum(m.size for m in members if m.isfile()))
        if dropped_links:
            _note_dropped_links(dropped_links)
        # Python 3.12+ 支持 filter；3.14+ 默认 data 过滤器，这里显式指定更稳妥。
        # 用 "tar"（=fully_trusted 的旧名语义）而非 "data"：data 会
        # `mode &= 0o755; mode |= 0o600`，把只读位与 setuid 位强制抹掉
        # （元数据专家实测：0444→0644、4755→0755）。越界校验收敛到 dest 内、
        # 链接落点另行校验，安全性不依赖 data 过滤器。
        try:
            tf.extractall(target, members=members, filter="tar")  # type: ignore[arg-type]
        except TypeError:  # pragma: no cover - 3.10/3.11 无 filter 参数
            tf.extractall(target, members=members)
        # 部分平台 filter="tar" 仍不还原 mode（Windows chmod 语义受限），
        # 显式再补一遍，保证 0600/0444 这类关键位不丢。
        _restore_tar_modes(target, members)
        if renamed:
            logging.getLogger(__name__).warning(
                "已重命名 %d 个 Windows 不安全文件名（原文件数据完好，只是名字变了）：%s",
                len(renamed),
                "；".join(renamed[:5]) + ("…" if len(renamed) > 5 else ""),
            )


def _note_dropped_links(items: list[str]) -> None:
    """把「因指向包外而被跳过」的链接记进日志，让用户知道少了什么。"""
    try:
        logging.getLogger(__name__).warning(
            "已跳过 %d 个指向包外的链接（其余文件正常解出）：%s",
            len(items),
            "；".join(items[:5]) + ("…" if len(items) > 5 else ""),
        )
    except Exception:  # noqa: BLE001 - 日志失败不影响解压
        pass


def _restore_tar_modes(target: Path, members: list[tarfile.TarInfo]) -> None:
    """按 tar 头里的 mode 还原权限位（补 filter 不足）。

    只处理普通文件与目录；符号链接在 Windows 上无法 chmod，静默跳过。
    失败一律忽略：某些文件系统（FAT/exFAT/网络盘）不支持 chmod，
    不能因此让整次恢复失败。
    """
    for m in members:
        if m.issym() or m.islnk():
            continue
        mode = m.mode & 0o7777
        if not mode:
            continue
        p = target / m.name
        try:
            if p.exists():
                os.chmod(p, mode)
        except (OSError, NotImplementedError):
            continue


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


_LAST_SIZE_PROBE_ERROR = ""


def _remember_probe_error(msg: str) -> None:
    """记住最近一次体积预检失败的原因（7-Zip stderr 摘要）。"""
    global _LAST_SIZE_PROBE_ERROR
    _LAST_SIZE_PROBE_ERROR = msg


def _digest_7z_error(raw: bytes) -> str:
    """从 7-Zip 输出里提取可区分病因的一句话。

    7-Zip 的 stderr 本来就能区分四种病因，旧实现 `if returncode != 0: return None`
    把它整个扔掉了，于是「密码错 / 包损坏 / 文件不存在 / 缺密码」给用户
    **完全相同**的一句「无法预读解压后大小」，还附一条走不通的建议。

    返回空串 = 认不出来（调用方退回通用文案）。
    """
    text = raw.decode("utf-8", "replace")
    low = text.lower()
    if "wrong password" in low or "password is incorrect" in low:
        return "密码错误"
    # 7-Zip 在需要密码时会打交互提示（EV 上 stdin=DEVNULL，所以直接失败）
    if "enter password" in low or "can not open encrypted archive" in low:
        return "该压缩包需要密码（本次未提供）"
    if "cannot find archive" in low or "no such file" in low:
        return "文件不存在或无法打开"
    if "unexpected end" in low or "crc failed" in low or "data error" in low:
        return "压缩包损坏或被截断"
    if "is not supported" in low or "unsupported" in low:
        return "不支持的压缩格式"
    for line in text.splitlines():
        ln = line.strip()
        if not ln or "Copyright" in ln or ln.startswith(("7-Zip", "p7zip")):
            continue
        if ln.lower().startswith(("scanning", "opening", "updating", "creating", "extracting")):
            continue
        return ln[:80]
    return ""


def _sum_size_lines(argv: list[str]) -> int | None:
    """跑 ``argv`` 并把输出里 ``Size =`` 行的数字求和。

    返回 ``None`` = **读不出声明大小**；调用方（``_check_external_bomb``）必须据此拒绝放行。

    **零命中必须返回 None，不能返回 0**（S1，安全级）。此前的实现无论如何都返回
    ``total``（初始 0），于是「命令成功、但一行 ``Size =`` 都没解析到」被当成
    "解压后共 0 字节" → ``declared > limit`` 恒假 → **炸弹预检静默 fail-open**。

    触发场景是真实存在的：7-Zip 对**流式/未知大小的容器**只给出一行空值——
    例如 ``.tar.zst`` 报 ``Method = header-open-only ... unknown-content-size``，
    ``7z l -slt -ba`` 输出恰好是 ``Size = ``（空）与 ``Packed Size = ``（空），
    一个数字都没有。实测把上限压到 256 KiB 仍能把这个 4 MiB 的包完整解出来。

    与 rar 路径对齐：``_declared_unpacked_unrar`` 早就有 ``seen`` 守卫
    （``return total if seen else None``），7z 路径此前漏了——两条路必须对称。

    注意**不要**把"只有空目录的包"误伤成 None：那种包的条目行是 ``Size = 0``，
    能解析出 0 → ``seen=True`` → 正常返回 0（本来就该放行，0 字节炸不了盘）。

    ## 失败时记录 7-Zip 的 stderr（CLI Agent 的 P1）

    旧实现 `if proc.returncode != 0: return None` **把 stderr 整个扔掉**。
    而 7-Zip 的 stderr 恰好能区分四种病因：
        「Wrong password」     → 密码错
        「Unexpected end」/「CRC Failed」 → 包损坏/截断
        「Cannot find archive」→ 文件不存在
        「Can not open encrypted archive」→ 缺密码
    丢掉之后，上层只能统一报「无法预读解压后大小，拒绝在未预检的情况下解压」，
    还附一条**根本走不通**的建议（`BACKUPSTUDIO_MAX_UNPACKED_BYTES=0`）——
    CLI Agent 实测 4 种病因的输出**除文件名外字节级完全相同**，用户无法判断
    该改密码还是该换备份。

    现在把 stderr 摘要挂到模块级 `_LAST_SIZE_PROBE_ERROR`，供上层组装可区分的文案。
    """
    kwargs: dict = {"capture_output": True, "timeout": 300, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(argv, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _remember_probe_error(f"{type(exc).__name__}: {exc}")
        return None
    if proc.returncode != 0:
        err = _digest_7z_error((proc.stdout or b"") + (proc.stderr or b""))
        _remember_probe_error(err or f"退出码 {proc.returncode}")
        return None
    out = (proc.stdout or b"").decode("utf-8", "replace")
    total = 0
    seen = False
    for line in out.splitlines():
        line = line.strip()
        # 精确匹配 "Size ="，避免把 "Packed Size =" 也算进来
        if line.startswith("Size ="):
            raw = line[len("Size ="):].strip()
            try:
                total += int(raw)
            except ValueError:
                # 空值（`Size = `）或非数字：该条目大小未知，**不算见过**
                continue
            seen = True
    return total if seen else None


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
        # 把 7-Zip 的 stderr 摘要带上，让用户能区分「密码错 / 包损坏 / 文件不存在」
        # （CLI Agent 实测：旧文案四种病因**除文件名外字节级完全相同**）。
        # 同时删掉那条走不通的建议 —— `declared is None` 的 raise 就发生在这里，
        # 结构上根本没有代码会去读 `BACKUPSTUDIO_MAX_UNPACKED_BYTES`。
        reason = _LAST_SIZE_PROBE_ERROR
        if reason:
            raise ExtractionError(
                f"无法读取 {archive.name} 的压缩包信息：{reason}。\n"
                f"请确认：① 密码是否正确 ② 文件是否完整（未截断/未缺分卷）③ 格式是否受支持"
            )
        raise ExtractionError(
            f"无法读取 {archive.name} 的压缩包信息（7-Zip 未返回可解析的条目大小）。\n"
            f"包可能已损坏、被截断，或使用了不支持的格式变体。"
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
        argv = [exe, "x", "-y", f"-o{long_path(target)}", str(archive)]
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
            argv = [exe, "x", "-idq", "-y", str(archive), long_path(target) + os.sep]
            if password:
                argv.insert(3, f"-p{password}")
            code, out = _run(argv)
            if code != 0:
                raise ExtractionError(f"RAR 解压失败（退出码 {code}）：{out}")
            return "rar"
        seven = find_7z()
        if seven:
            _check_external_bomb(seven, archive, password)
            argv = [seven, "x", "-y", f"-o{long_path(target)}", str(archive)]
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


def _dedupe_case(names: list[str]) -> dict[str, str]:
    """检测**同一目录内仅大小写不同**的成员，给重复项一个不冲突的新名字。

    返回 ``{原名: 新名}``（只含需要改名的项）。

    ## 为什么必须处理（P1，核对 Agent 实测）

    Windows 文件系统（NTFS 默认）**大小写不敏感**：``Readme.txt`` 与
    ``readme.txt`` 是**同一个文件**。解压时第二个会覆盖第一个 —— 数据静默丢失，
    而 `extract_archive` 返回 `ok=True`、无任何告警。

    实测（tar 含 3 个仅大小写不同的成员 + keep.txt）：
        **只落地 2 个**（`Readme.txt` 与 `keep.txt`），另两个被覆盖

    ## 关键：**只对同名文件去重，目录不参与**

    第一版把目录也纳入去重集合，结果 ``src`` 目录与 ``src/a.bin`` 的 key
    计算方式不同 → 目录被误判为冲突 → ``src/a.bin`` 被改名成 ``src_1/a.bin``，
    整棵目录树错位（实测 6 个分卷测试因此失败）。

    正确做法：**按「父目录 + 文件名」精确比较**，目录本身不改名。
    """
    if sys.platform != "win32":
        return {}
    # (lower(父目录), lower(文件名)) -> 该目录下已用的 (原名 stem+ext)
    used: dict[tuple[str, str], str] = {}
    rename: dict[str, str] = {}
    for name in names:
        parts = name.split("/")
        # 目录部分原样保留（不同层级的同名目录不算冲突）
        dir_key = "/".join(parts[:-1]).lower()
        fname = parts[-1]
        if not fname:
            continue  # 目录条目本身（以 / 结尾）
        key = (dir_key, fname.lower())
        if key in used:
            # 同一目录下已有仅大小写不同的名字 → 给这个加序号
            stem, dot, ext = fname.partition(".")
            n = 1
            cand = fname
            while True:
                cand = f"{stem}_{n}{dot}{ext}"
                ckey = (dir_key, cand.lower())
                if ckey not in used:
                    break
                n += 1
                if n > 9999:  # 防御：极端情况下不死循环
                    cand = f"{stem}_{os.getpid()}{dot}{ext}"
                    break
            used[ckey] = cand
            prefix = "/".join(parts[:-1])
            rename[name] = f"{prefix}/{cand}" if prefix else cand
        else:
            used[key] = fname
    return rename



def _make_staging(target: Path, prefix: str = "bs-extract-") -> Path:
    """建解压暂存目录。

    ## 关键：目标路径接近 MAX_PATH 时，**必须**用系统临时目录

    建在 target 内是**性能优化**（同卷避免跨盘复制），但它会把 target 的路径长度
    **带进暂存目录**，而解压时要在暂存目录下再拼上**包内成员的相对路径**：

        暂存目录 226 字符 + 包内成员 172 字符 = 398 字符 → 7z 失败
        （staging 自己建得出来，因为 226 < 260；但解到里面就超了）

    实测：271 字符 target 下，staging 建在 target 内 → 7z 解压 rc=2、
    staging 里只有 `shallow.txt`，深路径成员丢了；`_merge_into` 随后也失败。

    所以判据不是「能不能建出来」，而是「建出来之后还能不能装下包内成员」。
    保守做法：**target 路径超过 200 字符就直接用系统临时目录**。
    """
    target_len = len(str(target))
    if target_len > 200:
        return Path(tempfile.mkdtemp(prefix=prefix))
    try:
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(target)))
    except OSError:
        return Path(tempfile.mkdtemp(prefix=prefix))


def extract_archive(archive: Path, target: Path, password: str | None = None) -> tuple[bool, str]:
    """把压缩包安全解压到目录，返回 (成功, 信息)。"""
    archive = Path(archive)
    target = Path(target)
    if not archive.exists():
        return False, f"文件不存在：{archive}"
    # 建目标目录要用长路径前缀（Windows 上 >260 字符传统 API 失败）。
    # 旧实现这行在 try 之外 —— 深路径时直接抛**未捕获**的
    # `FileNotFoundError [WinError 3]` 把整个 CLI 打崩（核对 Agent 实测)。
    try:
        Path(long_path(target)).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, (
            f"无法创建目标目录：{target}\n"
            f"原因：{type(exc).__name__}: {exc}\n"
            f"（Windows 上路径超过 260 字符时需要在系统里启用长路径支持，"
            f"或改用更短的目录）"
        )
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
        staging = _make_staging(target, "bs-extract-")
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
            _mc: list[str] = []
            _merge_into(staging, target, _mc)
            if _mc:
                logging.getLogger(__name__).warning(
                    "解压时遇到 %d 处类型冲突，你的原有文件已改名保留：%s",
                    len(_mc), "；".join(_mc[:5]),
                )
            return _finish(kind)
        except ExtractionError as exc:
            return False, str(exc)
        except (zipfile.BadZipFile, tarfile.TarError) as exc:
            return False, f"压缩包损坏或格式不匹配：{exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # 7z / rar：**也必须先解到暂存目录再合并**（审计 P0）。
    #
    # 早期实现直接把目标目录交给外部程序（`-o{target}` / `{target}\`），
    # 实测后果（审计用 12MiB 损坏 7z 复现）：
    #   ① 解压到一半失败 → 半截文件永久留在用户目录里（"半包污染"）；
    #   ② 更糟：目标里已有同名**已恢复好的文件**会被覆盖成损坏字节
    #      （实测 movie.mkv: 28B 好文件 → 12582912B 坏字节，md5 全变），
    #      而返回信息只说"解压失败"，不会告知目标已被改写。
    #   对照：zip/tar 路径走暂存目录，失败时目标目录一个字节都没动。
    #
    # 现在统一为「解到 staging → 扫链接/越界 → 合并到 target」，与 zip/tar 同构。
    staging = _make_staging(target, "bs-extract-")
    try:
        kind = _extract_external(archive, staging, password)
        escapes = _scan_escape(staging)
        if escapes:
            raise ExtractionError(f"压缩包含指向外部的链接，已拒绝：{escapes[:3]}")
        _mc: list[str] = []
        _merge_into(staging, target, _mc)
        if _mc:
            logging.getLogger(__name__).warning(
                "解压时遇到 %d 处类型冲突，你的原有文件已改名保留：%s",
                len(_mc), "；".join(_mc[:5]),
            )
        return _finish(kind)
    except ExtractionError as exc:
        return False, str(exc)
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        return False, f"压缩包损坏或格式不匹配：{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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
    staging = _make_staging(target, "bs-join-")
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


#: Windows **保留设备名**。不区分大小写，且**带扩展名也算**（`CON.txt` 同样是设备）。
#: 写入这些名字时 `open()` 会"成功"、`exists()` 为 True，但数据被设备吞掉 → size=0。
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
    "CONIN$", "CONOUT$",
}

#: Windows 文件名里禁止出现的字符
_WIN_BAD_CHARS = set('<>:"|?*') | {chr(i) for i in range(32)}


def _windows_unsafe_reason(name: str) -> str:
    """判断单个路径分量是否为 Windows 上无法安全创建的名字。

    返回空串 = 安全；否则返回原因（用于日志）。

    ## 为什么必须拦截（P0，兼容性 Agent 实测）

    Windows 把 `CON`/`NUL`/`AUX`/`COM1..9`/`LPT1..9` 永久保留给设备。
    `open("NUL", "w")`、`open("CON", "w")` 都会**成功返回**，但写进去的字节
    全被丢弃 —— 于是：
        * `open()` 不报错  → 我们以为写成功了
        * `exists()` 为 True（其实是设备）
        * 但 `size == 0`   → **数据全丢**

    实测（含 5 个保留名的 tar 恢复到 Windows）：
        CON / NUL / AUX / CON.txt → exists=True size=0（数据丢失）
        normal.txt               → 完全缺失（连带丢失）

    同类还有：
        * 非法字符 `< > : " | ? *` → `open()` 报 OSError，整包解压失败
        * 结尾是空格或点 → Windows 自动剥掉，导致名字冲突
    """
    if not name or name in (".", ".."):
        return ""
    # 非法字符
    bad = [c for c in name if c in _WIN_BAD_CHARS]
    if bad:
        return f"含 Windows 非法字符 {bad!r}"
    # 结尾空格/点（Windows 会静默剥掉，与同目录另一项撞名）
    if name != name.rstrip(" ."):
        return "以空格或点结尾（Windows 会静默剥掉）"
    # 保留设备名：取扩展名之前的部分判断（CON.txt 也是设备）
    stem = name.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        return f"{stem} 是 Windows 保留设备名"
    return ""


def _safe_component(name: str) -> str:
    """把 Windows 上不安全的名字改成一个可安全落盘的名字。

    策略：**保留原名可辨识部分**，加 `_renamed` 后缀并替换非法字符。
    这样用户能看出「这里原本叫什么」，不至于以为文件丢了。
    """
    out = "".join("_" if c in _WIN_BAD_CHARS else c for c in name).rstrip(" .")
    if not out:
        out = "_"
    stem, dot, ext = out.partition(".")
    if stem.upper() in _WIN_RESERVED:
        stem = f"{stem}_renamed"
    return stem + dot + ext


def _merge_into(staging: Path, target: Path, conflicts: list[str] | None = None) -> None:
    """把暂存目录内容合并到目标。

    ## 绝不静默删除用户数据（S0，代码审查 Agent 实测）

    旧实现在两类冲突上直接删用户的东西：

        # 目标里 `data` 是**用户的文件**，包里是**目录**
        if dest.exists() and not dest.is_dir():
            dest.unlink()                   # ← 用户文件被删
        ...
        # 目标里是**含 2 个文件的目录**，包里是**文件**
        if dest.is_dir():
            shutil.rmtree(dest)             # ← 用户整个目录被删

    两处都返回 `ok=True`、无任何提示。**原生 7z 在同样情况下是 rc=2 拒绝覆盖、
    用户数据一动不动**。灾难恢复场景（往已有备份目录补解压）会静默丢数据。

    ## 修法

    * 普通同名文件 → 覆盖（这是恢复的预期行为，用户就是想拿回这份文件）
    * **类型冲突**（文件 vs 目录）→ 把用户原有项改名成 `*.conflict-<n>` 保留，
      再放入包里那份，并记进 `conflicts` 让上层告知用户
    * 目录递归合并（不删）
    """
    for item in staging.iterdir():
        dest = target / item.name
        # **所有落盘操作都走长路径前缀**（S0，核对 Agent）：
        # Windows 上 target 可能超过 260 字符，`shutil.move` / `iterdir` /
        # `exists` 全都会失败，于是整包解不出来。
        lp_dest = long_path(dest)
        item_is_dir = item.is_dir() and not item.is_symlink()

        if item_is_dir:
            if os.path.exists(lp_dest) and not os.path.isdir(lp_dest):
                # 文件 ↔ 目录冲突：保留用户的文件，改名让位
                _park_conflict(dest, conflicts)
                shutil.move(str(item), lp_dest)
            elif os.path.isdir(lp_dest):
                _merge_into(item, dest, conflicts)
            else:
                shutil.move(str(item), lp_dest)
        else:
            if os.path.isdir(lp_dest):
                # 目录 ↔ 文件冲突：保留用户的目录，改名让位
                _park_conflict(dest, conflicts)
            elif os.path.exists(lp_dest):
                os.unlink(lp_dest)  # 同名文件：恢复的本意就是拿回它，覆盖合理
            shutil.move(str(item), lp_dest)


def _park_conflict(dest: Path, conflicts: list[str] | None) -> None:
    """把目标处已有的项改名保留（`<名>.conflict-<n>`），绝不删除。"""
    n = 1
    while True:
        parked = dest.with_name(f"{dest.name}.conflict-{n}")
        if not os.path.exists(long_path(parked)):
            break
        n += 1
    shutil.move(long_path(dest), long_path(parked))
    if conflicts is not None:
        conflicts.append(f"{dest.name} → {parked.name}（类型冲突，你的原文件已保留）")
