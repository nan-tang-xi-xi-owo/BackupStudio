"""S3 回归：内置 ZIP 必须真的尊重压缩级别。

## 缺陷（体验级，但影响直接）

三个内置 ZIP 写出点（``archives.ZipCompressor._info``、``py7zr_backend.Py7zrCompressor.
_zip_info``、``py7zr_backend.ZipFileCompressor._info``）都只设了 ``zi.compress_type``，
**没设 ``zi.compress_level``**（全项目此前 ``compress_level`` 零命中）。

Python 3.12+ 的 ``zipfile._ZipWriteFile.__init__`` 是这么取级别的：

```python
self._compressor = _get_compressor(zinfo.compress_type, zinfo.compress_level)
```

而 ``ZipInfo.compress_level`` 的默认值是 ``None`` → ``_get_compressor`` 落到
``zlib.Z_DEFAULT_COMPRESSION``（恒 6）。于是**级别 1 与级别 9 产出完全相同的字节**：

实测（584000 B 源，六份半可压缩日志 + 一份随机数据）：
```
ZipCompressor      level=0 ->  587782 B  types={0}
ZipCompressor      level=1 ->   17228 B  types={8}
ZipCompressor      level=9 ->   17228 B  types={8}     ← 与 level=1 完全相同
```

修复后：
```
level=1 -> 27060 B
level=9 -> 17221 B      ← 显著更小
```

## 为什么原测试没抓到

``test_extract.py::TestBuiltinZipActuallyCompresses`` 只断言
``compress_type == ZIP_DEFLATED`` 与 ``ratio < 0.05``——**级别差异完全没断言**，
属于"assert 太松"的典型。本文件补上"level 9 必须严格小于 level 1"。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


#: 用来测级别的语料：**半可压缩**（重复度适中）。
#: 全零数据会被压到极小、level 1 与 9 的差距被容器开销淹没；随机数据压不动，
#: 两者又会一样大。必须选这种"可压但没压到极限"的文本，级别差异才可观测。
_WORDS = " ".join(f"word{i:04d}" for i in range(400))



def zi_level(zi) -> int | None:
    """读 ``ZipInfo`` 的压缩级别，跨 Python 版本。

    Python 3.13+ 属性名是 ``compress_level``，3.10–3.12 是 ``_compresslevel``。
    **测试本身也必须兼容**，否则 3.12 的 CI runner 上直接 AttributeError
    （run #40 实证：Linux/macOS/Windows 三个 job 都因此红）。
    """
    if hasattr(zi, "compress_level"):
        return zi.compress_level
    return getattr(zi, "_compresslevel", None)

def _write_corpus(src: Path) -> int:
    src.mkdir(parents=True, exist_ok=True)
    for i in range(6):
        (src / f"log{i}.txt").write_bytes((_WORDS + "\n").encode() * 40)
    return sum(p.stat().st_size for p in src.iterdir() if p.is_file())


def _build_builtin(cls_name: str, src: Path, dest: Path, level: int) -> Path:
    """用指定的内置后端写一个 zip，返回产物路径。"""
    from backupstudio.archives import ArchiveSpec, Progress

    import backupstudio.archives as A
    import backupstudio.py7zr_backend as P

    cls = getattr(A, cls_name, None) or getattr(P, cls_name, None)
    assert cls is not None, f"找不到后端 {cls_name}"
    spec = ArchiveSpec(dest_path=dest, fmt="zip", level=level)
    c = cls(dest, spec, Progress())
    try:
        c.add_dir("payload")
        for p in sorted(src.iterdir()):
            if p.is_file():
                c.add_file(p, f"payload/{p.name}")
        # 覆盖 writestr 路径（add_bytes），它与 add_file 是两条独立的写入分支
        c.add_bytes(b"inline marker line\n" * 200, "payload/marker.txt")
    finally:
        c.close()
    assert dest.is_file(), f"{cls_name} 未产出 {dest}"
    return dest


_BACKENDS = ["ZipCompressor", "Py7zrCompressor", "ZipFileCompressor"]


def _skip_if_missing(name: str) -> None:
    if name.startswith("Py7zr") or name.startswith("ZipFile"):
        pytest.importorskip("py7zr", reason=f"{name} 需要 py7zr")


@pytest.mark.parametrize("cls_name", _BACKENDS)
def test_level_nine_is_strictly_smaller_than_level_one(tmp_path, cls_name):
    """**核心断言**：level=9 的产物必须严格小于 level=1。

    修复前两者位元组完全相同（实测均 17228 B），断言 ``<`` 即刻失败。
    刻意不用 ``<=``：相等正是缺陷本身。
    """
    _skip_if_missing(cls_name)
    src = tmp_path / "corpus"
    _write_corpus(src)

    p1 = _build_builtin(cls_name, src, tmp_path / f"{cls_name}-l1.zip", 1)
    p9 = _build_builtin(cls_name, src, tmp_path / f"{cls_name}-l9.zip", 9)
    s1, s9 = p1.stat().st_size, p9.stat().st_size

    assert s9 < s1, (
        f"{cls_name}: level=9 ({s9} B) 不小于 level=1 ({s1} B) —— "
        f"压缩级别被忽略（compress_level 未设置，恒用 zlib 默认 6）"
    )


@pytest.mark.parametrize("cls_name", _BACKENDS)
def test_default_level_is_not_always_six(tmp_path, cls_name):
    """至少有一个显式级别要明显不同于 zlib 默认级别（6）的产物。

    这是"级别 1..9 全部退化成 6"的另一种表述：若所有级别产出同样字节，
    那么 level=1 必然等于 level=6。
    """
    _skip_if_missing(cls_name)
    src = tmp_path / "corpus"
    _write_corpus(src)

    p1 = _build_builtin(cls_name, src, tmp_path / f"{cls_name}-a.zip", 1)
    p6 = _build_builtin(cls_name, src, tmp_path / f"{cls_name}-b.zip", 6)
    assert p1.stat().st_size != p6.stat().st_size, (
        f"{cls_name}: level=1 与 level=6 产物同为 {p1.stat().st_size} B，级别显然没生效"
    )


@pytest.mark.parametrize("cls_name", _BACKENDS)
def test_level_zero_is_stored_and_close_to_source(tmp_path, cls_name):
    """``level=0`` 必须全 STORED，体积 ≈ 源大小（不压缩）。"""
    _skip_if_missing(cls_name)
    src = tmp_path / "corpus"
    source_bytes = _write_corpus(src)

    dest = _build_builtin(cls_name, src, tmp_path / f"{cls_name}-l0.zip", 0)
    with zipfile.ZipFile(dest) as zf:
        types = {i.compress_type for i in zf.infolist() if not i.is_dir()}
    assert types == {zipfile.ZIP_STORED}, (
        f"{cls_name}: level=0 应全用 STORED，实际 compress_type={types}"
    )
    # STORED 的开销只有本地头/中央目录，允许 20% + 4 KiB 的余量
    slack = max(4096, source_bytes * 0.2)
    assert abs(dest.stat().st_size - source_bytes) <= slack, (
        f"{cls_name}: level=0 产物 {dest.stat().st_size} B 与源 {source_bytes} B 偏离过大"
    )


@pytest.mark.parametrize("cls_name", _BACKENDS)
def test_compress_level_field_is_actually_set(tmp_path, cls_name):
    """直接检查 ``ZipInfo.compress_level`` 被写成整数级别（而不是 ``None``）。

    这是最贴近根因的断言：不看产物大小，只看"有没有把字段设下去"。
    如果哪天有人重构掉 ``zip_info_level``，本用例会立刻指出问题所在。
    """
    _skip_if_missing(cls_name)
    from backupstudio.archives import ArchiveSpec, Progress

    import backupstudio.archives as A
    import backupstudio.py7zr_backend as P

    cls = getattr(A, cls_name, None) or getattr(P, cls_name, None)
    dest = tmp_path / f"{cls_name}-probe.zip"
    c = cls(dest, ArchiveSpec(dest_path=dest, fmt="zip", level=3), Progress())
    try:
        make_info = getattr(c, "_info", None) or getattr(c, "_zip_info")
        zi = make_info("probe.txt", None, False)
        assert zi_level(zi) == 3, (
            f"{cls_name}: ZipInfo 压缩级别应为 3，实际 {zi_level(zi)!r}"
            f"（None = 落到 zlib 默认 6，级别设置被忽略）"
        )
        assert zi.compress_type == zipfile.ZIP_DEFLATED
        # 目录条目：STORED，且不设 compress_level
        dir_info = make_info("probe_dir", None, True)
        assert dir_info.compress_type == zipfile.ZIP_STORED
        assert zi_level(dir_info) is None, (
            "目录条目是 STORED，不该带无意义的 compress_level"
        )
    finally:
        c.close()


def test_zip_info_level_helper_contract():
    """``archives.zip_info_level`` 的契约（三处写出点的唯一真相来源）。"""
    from backupstudio.archives import zip_deflate_level, zip_info_level

    zi = zipfile.ZipInfo("a.txt")
    zip_info_level(zi, 9)
    assert (zi.compress_type, zi_level(zi)) == (zipfile.ZIP_DEFLATED, 9)

    zi = zipfile.ZipInfo("b.txt")
    zip_info_level(zi, 0)
    assert zi.compress_type == zipfile.ZIP_STORED and zi_level(zi) is None

    zi = zipfile.ZipInfo("c/")
    zip_info_level(zi, 9, is_dir=True)
    assert zi.compress_type == zipfile.ZIP_STORED and zi_level(zi) is None, (
        "目录条目必须 STORED 且不带 compress_level"
    )

    # 越界级别必须夹到 1..9（zlib 只认这个区间；0 会变成 Z_DEFAULT_COMPRESSION=6）
    assert [zip_deflate_level(v) for v in (-3, 0, 1, 9, 42)] == [1, 1, 1, 9, 9]


def test_end_to_end_through_create_archive(tmp_path):
    """端到端：``create_archive(level=...)`` 的两档产物大小必须不同。

    前面几个用例直接调后端类；这条走产品真实入口，防止"后端修好了但
    create_archive 没把 level 透传下去"。
    """
    from backupstudio.archives import ArchiveSpec, Item, Progress, create_archive, plan_items

    src = tmp_path / "payload"
    _write_corpus(src)
    items, _ = plan_items([Item(src, "payload")])

    sizes = {}
    for level in (1, 9):
        dest = tmp_path / f"e2e-L{level}.zip"
        res = create_archive(
            ArchiveSpec(dest_path=dest, fmt="zip", level=level), items, Progress()
        )
        assert Path(res.path).is_file(), f"create_archive 未产出 {dest}"
        sizes[level] = Path(res.path).stat().st_size

    assert sizes[9] < sizes[1], (
        f"create_archive: level=9 ({sizes[9]} B) 不小于 level=1 ({sizes[1]} B) —— "
        f"level 未透传到内置 ZIP 后端"
    )
