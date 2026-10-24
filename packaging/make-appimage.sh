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

# ---------------------------------------------------------------- 版本锁定
# 审查 R-5：原先固定取 releases/download/continuous（每天漂移），同一个
# commit 在不同日子会打出不同 AppImage。现锁定到 release tag，可用环境变量
# APPIMAGETOOL_VERSION 覆盖。
APPIMAGETOOL_VERSION="${APPIMAGETOOL_VERSION:-1.9.1}"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_VERSION}/appimagetool-${ARCH}.AppImage"

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
# B4：wayland 优先、xcb 兜底（原值 xcb;wayland 在纯 Wayland 桌面上反而先失败）
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland;xcb}"
exec "$HERE/usr/lib/backupstudio/BackupStudio" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# B4：.desktop 里 Exec/TryExec 都是 `backupstudio`，但 AppDir 之前只有
# usr/lib/backupstudio/BackupStudio，**没有 usr/bin/backupstudio** →
# TryExec 失效（桌面环境判定"程序不存在"而不显示图标/无法从菜单启动）。
# 补一个启动器脚本，让 Exec/TryExec 真正可解析。
cat > "$APPDIR/usr/bin/backupstudio" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")/../.."
exec "$HERE/usr/lib/backupstudio/BackupStudio" "$@"
EOF
chmod +x "$APPDIR/usr/bin/backupstudio"

cp "$ROOT/packaging/icon.png" "$APPDIR/io.github.backupstudio.BackupStudio.png"
cp "$ROOT/packaging/icon.png" "$APPDIR/usr/share/icons/hicolor/512x512/apps/io.github.backupstudio.BackupStudio.png"
cp "$ROOT/packaging/io.github.backupstudio.BackupStudio.desktop" "$APPDIR/"
cp "$ROOT/packaging/io.github.backupstudio.BackupStudio.desktop" "$APPDIR/usr/share/applications/"
cp "$ROOT/packaging/io.github.backupstudio.BackupStudio.metainfo.xml" "$APPDIR/usr/share/metainfo/"
cp "$ROOT/packaging/icon.png" "$APPDIR/.DirIcon"

# ---------------------------------------------------------------- appimagetool
mkdir -p "$TOOLS"
if [[ ! -x "$TOOLS/appimagetool" ]]; then
  echo "==> 下载 appimagetool ${APPIMAGETOOL_VERSION}"
  curl -L --fail --retry 3 --retry-delay 2 -o "$TOOLS/appimagetool" "$APPIMAGETOOL_URL"
  chmod +x "$TOOLS/appimagetool"
fi

# 校验：下载/缓存命中后都跑一次 --version，既证明二进制可执行，
# 也把实际版本打进日志（trace 用）。注意 release 产物自报的是 git hash
# 而非 tag（"appimagetool, 1-alpha (git version <hash>)"），因此这里不做
# 字符串断言；锁定效果来自上面的 URL。
"$TOOLS/appimagetool" --version

echo "==> 生成 AppImage"
ARCH="$ARCH" APPIMAGE_EXTRACT_AND_RUN=1 "$TOOLS/appimagetool" --no-appstream "$APPDIR" "$OUT"
echo "==> 完成：$OUT"
