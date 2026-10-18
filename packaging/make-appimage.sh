#!/usr/bin/env bash
# 把 PyInstaller 产物打成 AppImage（在任何 x86_64 Linux 上运行）。
#
# 前置：
#   - 已执行 python packaging/build.py（生成 dist/BackupStudio/）
#   - 已安装 libfuse2（运行时解压 AppImage 需要）
#
# 产物：dist/BackupStudio-<version>-x86_64.AppImage
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$("${PYTHON:-python3}" -c 'import sys; sys.path.insert(0, "'"$ROOT"'"); import backupstudio; print(backupstudio.__version__)')"
ARCH="$(uname -m)"
APPDIR="$ROOT/build/AppDir"
TOOLS="$ROOT/build/appimage-tools"
OUT="$ROOT/dist/BackupStudio-${VERSION}-${ARCH}.AppImage"

echo "==> 打包 $VERSION ($ARCH)"

if [[ ! -d "$ROOT/dist/BackupStudio" ]]; then
  echo "未找到 dist/BackupStudio，请先运行：python packaging/build.py" >&2
  exit 1
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/backupstudio" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/512x512/apps" "$APPDIR/usr/share/metainfo"

cp -a "$ROOT/dist/BackupStudio/." "$APPDIR/usr/lib/backupstudio/"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export PATH="$HERE/usr/bin:$PATH"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb;wayland}"
exec "$HERE/usr/lib/backupstudio/BackupStudio" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cp "$ROOT/packaging/icon.png" "$APPDIR/io.github.backupstudio.BackupStudio.png"
cp "$ROOT/packaging/icon.png" "$APPDIR/usr/share/icons/hicolor/512x512/apps/io.github.backupstudio.BackupStudio.png"
cp "$ROOT/packaging/io.github.backupstudio.BackupStudio.desktop" "$APPDIR/"
cp "$ROOT/packaging/io.github.backupstudio.BackupStudio.desktop" "$APPDIR/usr/share/applications/"
cp "$ROOT/packaging/io.github.backupstudio.BackupStudio.metainfo.xml" "$APPDIR/usr/share/metainfo/"
cp "$ROOT/packaging/icon.png" "$APPDIR/.DirIcon"

# ---------------------------------------------------------------- appimagetool
mkdir -p "$TOOLS"
if [[ ! -x "$TOOLS/appimagetool" ]]; then
  echo "==> 下载 appimagetool"
  curl -L --fail -o "$TOOLS/appimagetool" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
  chmod +x "$TOOLS/appimagetool"
fi

echo "==> 生成 AppImage"
ARCH="$ARCH" APPIMAGE_EXTRACT_AND_RUN=1 "$TOOLS/appimagetool" --no-appstream "$APPDIR" "$OUT"
echo "==> 完成：$OUT"
