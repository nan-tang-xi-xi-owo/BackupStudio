"""生成应用图标：icon.ico (Windows)、icon.icns (macOS)、icon.png / SVG (Linux)。"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging"
PKG = ROOT / "backupstudio"
SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)


def render_pngs(svg: str, out_dir: Path) -> dict[int, Path]:
    from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[int, Path] = {}
    for size in SIZES:
        img = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        p = out_dir / f"icon_{size}.png"
        img.save(str(p))
        paths[size] = p
    return paths


def write_ico(pngs: dict[int, Path], target: Path) -> None:
    """把多张 PNG 打包成 ICO（PNG 压缩条目，Vista+ 支持）。"""
    entries = [s for s in (16, 24, 32, 48, 64, 128, 256) if s in pngs]
    blobs = [pngs[s].read_bytes() for s in entries]
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    dir_entries = b""
    for size, blob in zip(entries, blobs):
        width = 0 if size >= 256 else size
        dir_entries += struct.pack(
            "<BBBBHHII", width, width, 0, 0, 1, 32, len(blob), offset
        )
        offset += len(blob)
    target.write_bytes(header + dir_entries + b"".join(blobs))
    print("wrote", target)


def write_icns(pngs: dict[int, Path], target: Path) -> None:
    """用 iconutil 生成 icns（需要 macOS）；其它平台退回 PNG 拼接的简易 icns。"""
    iconset = OUT / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    mapping = {
        16: ["icon_16x16.png"],
        32: ["icon_16x16@2x.png", "icon_32x32.png"],
        64: ["icon_32x32@2x.png"],
        128: ["icon_128x128.png"],
        256: ["icon_256x256@2x.png", "icon_128x128@2x.png"],
        512: ["icon_512x512.png", "icon_256x256@2x.png"],
        1024: ["icon_512x512@2x.png"],
    }
    for size, names in mapping.items():
        if size not in pngs:
            continue
        for name in names:
            (iconset / name).write_bytes(pngs[size].read_bytes())
    if sys.platform == "darwin":
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(target)], check=False)
    else:
        # 简易 icns：只放 512/1024 两种 PNG 格式（ic10 / ic09）
        chunks = b""
        for code, size in (("ic10", 1024), ("ic09", 512), ("ic08", 256), ("ic07", 128)):
            if size not in pngs:
                continue
            data = pngs[size].read_bytes()
            chunks += code.encode() + struct.pack(">I", len(data) + 8) + data
        target.write_bytes(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)
    print("wrote", target)


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from backupstudio.gui import APP_ICON_SVG

    (OUT / "icon.svg").write_text(APP_ICON_SVG, encoding="utf-8")
    # 运行时图标（冻结程序不依赖 QtSvg，直接读取这些 PNG）
    tray_dir = PKG / "assets" / "tray"
    tray_dir.mkdir(parents=True, exist_ok=True)
    for size in (16, 24, 32, 48, 64, 128, 256):
        (tray_dir / f"app-{size}.png").write_bytes(pngs[size].read_bytes())
    print("wrote", tray_dir)
    write_ico(pngs, OUT / "icon.ico")
    write_icns(pngs, OUT / "icon.icns")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
