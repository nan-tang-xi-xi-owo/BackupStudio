"""S1 回归：对「无 Size 容器」的炸弹预检不得 fail-open。

## 缺陷（安全级）

``_sum_size_lines`` 在「命令跑成功、但一行 ``Size =`` 都没解析到」时返回 ``0``
而不是 ``None``；而 ``_check_external_bomb`` 只拦 ``None``：

```python
declared = _declared_unpacked_7z(...)   # 返回 0
if declared is None:                    # 0 is not None → 不进拒绝分支
    raise ExtractionError(...)
...
if limit > 0 and declared > limit:      # 0 > limit 恒假 → 静默放行
```

于是对**声明大小为空**的容器，炸弹预检完全失效：

* 7-Zip 对流式/未知大小的容器只输出空值。实测 ``7z l -slt -ba payload.tar.zst``：
  ```
  Size =
  Packed Size =
  ```
  （``Method = header-open-only ... unknown-content-size``）——一个数字都没有。
* 结果：把 ``BACKUPSTUDIO_MAX_UNPACKED_BYTES`` 压到 256 KiB，一个 4 MiB 的
  ``.tar.zst`` 仍被完整解出来（实测 ``ok=True``）。

**不对称证据**：rar 路径的 ``_declared_unpacked_unrar`` 一直有 ``seen`` 守卫
（``return total if seen else None``），7z 路径漏了。本文件钉住两者对称。

## 修复后必须同时成立

* 零命中 → ``None`` → ``_check_external_bomb`` **拒绝**（不得当成"已确认安全"）；
* 正常的 7z / zip / tar.gz **不能**被误杀（``Size = 0`` 的空目录包也算正常）。
"""

from __future__ import annotations

import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _seven():
    from backupstudio.tools import find_7z

    return find_7z()


requires_7z = pytest.mark.skipif(_seven() is None, reason="本机无 7-Zip（S1 需要真外部进程）")


def _make_unknown_size_container(tmp_path: Path, payload: bytes) -> Path:
    """造一个 7-Zip 读不出内容大小的容器（内层 tar 的 zstd 流）。

    返回的路径**故意**以 ``.tar.zst`` 命名——这正是产品里真实的形态；
    需要走 7z 路径的用例会自己再复制成 ``.7z``（见 ``_disguise_as_7z``）。
    """
    zstandard = pytest.importorskip("zstandard")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("big.bin")
        ti.size = len(payload)
        tf.addfile(ti, io.BytesIO(payload))
    arc = tmp_path / "stream.tar.zst"
    arc.write_bytes(zstandard.ZstdCompressor(level=3).compress(buf.getvalue()))
    return arc


def _disguise_as_7z(src: Path, dest: Path) -> Path:
    """把无 Size 容器复制成 ``.7z``，逼产品走 ``_extract_external`` 的 7z 分支。

    文件名/扩展名决定分发路径；容器本体仍是那条 zstd 流，7-Zip 照样只能报空 Size。
    """
    dest.write_bytes(src.read_bytes())
    return dest


class TestZeroHitReturnsNone:
    """纯函数层：零命中必须是 ``None``，不能是 ``0``。"""

    def test_no_size_lines_at_all_returns_none(self, tmp_path, monkeypatch):
        """命令成功但输出里没有任何 ``Size =`` → ``None``。"""
        from backupstudio import extract as ex

        class _Proc:
            returncode = 0
            stdout = b"Path = x\nMethod = header-open-only\n\n"
            stderr = b""

        monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: _Proc())
        assert ex._sum_size_lines(["fake-7z", "l"]) is None, (
            "零命中必须返回 None（返回 0 会让 _check_external_bomb 静默放行）"
        )

    def test_empty_size_value_returns_none(self, tmp_path, monkeypatch):
        """``Size = ``（空值）这种真实 7-Zip 输出同样必须算"读不出来"。"""
        from backupstudio import extract as ex

        class _Proc:
            returncode = 0
            stdout = "Size = \nPacked Size = \n".encode()
            stderr = b""

        monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: _Proc())
        assert ex._sum_size_lines(["fake-7z", "l"]) is None, "空 Size 值不得当成 0"

    def test_zero_but_real_size_returns_zero(self, monkeypatch):
        """**反向红线**：真的解析到 ``Size = 0``（空目录包）要返回 0，不能变 None。

        否则每个只含空目录的备份都会被预检拒绝——把 fail-closed 修成"fail-always"。
        """
        from backupstudio import extract as ex

        class _Proc:
            returncode = 0
            stdout = b"Path = emptydir\nSize = 0\nPacked Size = 0\n"
            stderr = b""

        monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: _Proc())
        assert ex._sum_size_lines(["fake-7z", "l"]) == 0

    def test_packed_size_not_counted(self, monkeypatch):
        """``Packed Size =`` 不得计入（只有 ``Size =`` 是解压后大小）。"""
        from backupstudio import extract as ex

        class _Proc:
            returncode = 0
            stdout = b"Path = a\nSize = 100\nPacked Size = 999999\n"
            stderr = b""

        monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: _Proc())
        assert ex._sum_size_lines(["fake-7z", "l"]) == 100

    def test_nonzero_exit_returns_none(self, monkeypatch):
        from backupstudio import extract as ex

        class _Proc:
            returncode = 2
            stdout = b"Size = 5\n"
            stderr = b"boom"

        monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: _Proc())
        assert ex._sum_size_lines(["fake-7z", "l"]) is None

    def test_missing_binary_returns_none(self, tmp_path):
        from backupstudio import extract as ex

        assert ex._sum_size_lines([str(tmp_path / "no-such-7z.exe"), "l"]) is None


class TestRealUnknownSizeContainer:
    """真外部进程层：7-Zip 对 .tar.zst 报空 Size → 预检必须拒绝。"""

    @requires_7z
    def test_pure_function_on_real_container(self, tmp_path):
        from backupstudio import extract as ex

        arc = _make_unknown_size_container(tmp_path, b"A" * (2 * 1024 * 1024))
        got = ex._sum_size_lines([_seven(), "l", "-slt", "-ba", str(arc)])
        assert got is None, f"7-Zip 对该容器不报 Size，应返回 None，实际 {got!r}"

    @requires_7z
    def test_bomb_check_refuses_unknown_size(self, tmp_path):
        """``_check_external_bomb`` 必须抛 ExtractionError（不得静默 return）。"""
        from backupstudio import extract as ex

        arc = _make_unknown_size_container(tmp_path, b"A" * (2 * 1024 * 1024))
        with pytest.raises(ex.ExtractionError) as ei:
            ex._check_external_bomb(_seven(), arc, None)
        # 新文案必须说清「读不出包信息」这个真实原因（CLI Agent 实证：
        # 4 种病因的旧文案除文件名外字节级完全相同，用户无法判断该怎么办）
        text = str(ei.value)
        assert "无法读取" in text and "压缩包信息" in text, (
            f"拒绝原因应说明读不出包信息，实际：{text}"
        )

    @requires_7z
    def test_tight_limit_cannot_be_bypassed(self, tmp_path, monkeypatch):
        """核心回归：上限压到 256 KiB，4 MiB 的无 Size 容器**不得**被解压出来。

        修复前实测 ``ok=True``（静默放行，写满 4 MiB）。这里同时断言"确实没写出
        全部内容"，避免"报失败但其实已经解完了"这种假修复。
        """
        from backupstudio import extract as ex

        payload = b"B" * (4 * 1024 * 1024)
        arc = _make_unknown_size_container(tmp_path, payload)
        disguised = _disguise_as_7z(arc, tmp_path / "sneaky.7z")

        monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, str(256 * 1024))
        out = tmp_path / "out"
        ok, msg = ex.extract_archive(disguised, out)

        assert ok is False, f"无 Size 容器在 256 KiB 上限下仍被解压（S1 fail-open）：{msg}"
        assert "无法读取" in msg or "炸弹" in msg or "超过上限" in msg, msg
        leaked = [p for p in out.rglob("*") if p.is_file()]
        assert sum(p.stat().st_size for p in leaked) < len(payload), (
            f"预检虽拒绝，但内容已被写出 {[p.name for p in leaked]}"
        )

    @requires_7z
    def test_no_dead_end_advice_in_unknown_size_rejection(self, tmp_path, monkeypatch):
        """拒绝文案**不得**再给走不通的环境变量建议（CLI Agent 的 P1）。

        历史问题：旧文案说「确认可信可设 BACKUPSTUDIO_MAX_UNPACKED_BYTES=0 关闭上限」，
        但 `declared is None` 的 raise 在 `unpack_limit()` **之前**无条件抛出 ——
        结构上没有任何代码会去读那个上限。用户照做、重跑、输出一字不差。

        本用例反向锁定：**文案里不能出现那个开关**，且必须给出真实可行动的三条排查项。
        """
        from backupstudio import extract as ex

        arc = _make_unknown_size_container(tmp_path, b"C" * (1024 * 1024))
        monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, "0")
        with pytest.raises(ex.ExtractionError) as ei:
            ex._check_external_bomb(_seven(), arc, None)
        text = str(ei.value)

        assert ex.ENV_MAX_UNPACKED_BYTES not in text, (
            f"文案仍在建议一个走不通的开关（用户照做也无效）：{text}"
        )
        # 必须给出真实可行动的方向
        assert ("密码" in text) or ("损坏" in text) or ("截断" in text), (
            f"文案应给出可行动的排查方向（密码/完整性/格式），实际：{text}"
        )


class TestNormalArchivesNotKilled:
    """误伤红线：正常包必须照旧能解（``Size = 0`` 的空目录包也算正常）。"""

    @requires_7z
    def test_normal_7z_still_extracts(self, tmp_path, monkeypatch):
        import subprocess

        from backupstudio.extract import extract_archive

        src = tmp_path / "a.txt"
        src.write_text("hello\n" * 100, encoding="utf-8")
        arc = tmp_path / "normal.7z"
        seven = _seven()
        r = subprocess.run(
            [seven, "a", "-t7z", str(arc), str(src)],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=300,
        )
        if r.returncode != 0 or not arc.exists():
            pytest.skip(f"本机 7-Zip 无法建包：{(r.stdout or b'')[-160:]!r}")

        monkeypatch.delenv("BACKUPSTUDIO_MAX_UNPACKED_BYTES", raising=False)
        out = tmp_path / "out"
        ok, msg = extract_archive(arc, out)
        assert ok, f"正常 7z 被误杀：{msg}"
        assert (out / "a.txt").is_file()

    def test_normal_zip_still_extracts(self, tmp_path):
        from backupstudio.extract import extract_archive

        arc = tmp_path / "n.zip"
        with zipfile.ZipFile(arc, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("x.txt", b"payload\n" * 50)
        out = tmp_path / "out"
        ok, msg = extract_archive(arc, out)
        assert ok, f"正常 zip 被误杀：{msg}"
        assert (out / "x.txt").is_file()

    def test_normal_targz_still_extracts(self, tmp_path):
        from backupstudio.extract import extract_archive

        src = tmp_path / "a.bin"
        src.write_bytes(b"D" * 4096)
        arc = tmp_path / "n.tar.gz"
        with tarfile.open(arc, "w:gz") as tf:
            tf.add(src, arcname="a.bin")
        out = tmp_path / "out"
        ok, msg = extract_archive(arc, out)
        assert ok, f"正常 tar.gz 被误杀：{msg}"
        assert (out / "a.bin").is_file()

    @requires_7z
    def test_dir_only_archive_not_treated_as_unknown(self, tmp_path, monkeypatch):
        """只含空目录的包：``Size = 0`` 可解析 → 不得被当成"读不出来"拒绝。"""
        import subprocess

        from backupstudio import extract as ex

        d = tmp_path / "emptydir"
        d.mkdir()
        arc = tmp_path / "dironly.7z"
        seven = _seven()
        r = subprocess.run(
            [seven, "a", "-t7z", str(arc), str(d)],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=300,
        )
        if r.returncode != 0 or not arc.exists():
            pytest.skip(f"本机 7-Zip 无法建包：{(r.stdout or b'')[-160:]!r}")

        declared = ex._declared_unpacked_7z(seven, arc, None)
        assert declared == 0, f"空目录包的声明大小应为 0（可解析），实际 {declared!r}"
        # 上限压到 1 字节也**不该**在这里被拒（0 > 1 为假）
        monkeypatch.setenv(ex.ENV_MAX_UNPACKED_BYTES, "1")
        ex._check_external_bomb(seven, arc, None)  # 不抛异常即通过
