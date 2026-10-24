#!/usr/bin/env bash
# 在 macOS 上打包：生成 dist/BackupStudio.app 以及 dist/BackupStudio-<version>-<arch>.dmg
# 其中 <arch> 由 uname -m 归一化而来：arm64 / x64
#
# 前置：
#   - 已执行 python packaging/build.py（生成 dist/BackupStudio.app）
#   - 建议先打包随附的 7-Zip：tools/darwin/7z/7zz（从 https://www.7-zip.org/a/7z2409-mac.tar.xz 解出）
#
# 签名（可选）：
#   export CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   ./packaging/make-dmg.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$("${PYTHON:-python3}" -c 'import sys; sys.path.insert(0, "'"$ROOT"'"); import backupstudio; print(backupstudio.__version__)')"
# 架构后缀：arm64/aarch64 → arm64，x86_64 → x64（保证两个 macOS job 的产物不重名）
ARCH="$(uname -m)"
case "$ARCH" in
  arm64|aarch64) ARCH=arm64 ;;
  x86_64) ARCH=x64 ;;
esac
APP="$ROOT/dist/BackupStudio.app"
DMG="$ROOT/dist/BackupStudio-${VERSION}-${ARCH}.dmg"
STAGE="$ROOT/build/dmg-stage"

if [[ ! -d "$APP" ]]; then
  echo "未找到 $APP，请先运行：python packaging/build.py" >&2
  exit 1
fi

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  echo "==> 代码签名"
  codesign --deep --force --options runtime --timestamp \
    --entitlements "$ROOT/packaging/entitlements.plist" \
    --sign "$CODESIGN_IDENTITY" "$APP"
else
  echo "==> 跳过签名（未设置 CODESIGN_IDENTITY）；分发给他人时需要签名与公证"
fi

rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -a "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

# 简短的使用说明
cat > "$STAGE/使用说明.txt" <<'EOF'
BackupStudio（备份工坊）

1. 把 BackupStudio.app 拖到 Applications 文件夹。
2. 首次打开如提示“无法验证开发者”，请右键 → 打开，或到 系统设置 → 隐私与安全性 中允许。
3. 打开后可在「设置」里开启开机自启；关闭窗口时会驻留菜单栏继续按计划备份。

默认快捷键：⌘N 新建任务，⌘B 立即备份，F5 刷新，⌘, 打开设置。
EOF

echo "==> 生成 DMG"
# hdiutil 偶发 "Resource busy"：多为残留挂载点或 Spotlight 索引占用。
# 先卸载与本卷同名的残留挂载，失败则退避重试（CI 上实测出现过一次）。
for _ in 1 2 3; do
  if hdiutil create -volname "BackupStudio" -srcfolder "$STAGE" -ov -format UDZO "$DMG"; then
    break
  fi
  echo "==> hdiutil 失败，清理残留挂载后重试（第 $_ 次）" >&2
  hdiutil detach "/Volumes/BackupStudio" -force 2>/dev/null || true
  sleep 3
done
[[ -f "$DMG" ]] || { echo "DMG 创建失败：$DMG" >&2; exit 1; }

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  codesign --force --timestamp --sign "$CODESIGN_IDENTITY" "$DMG"
  if [[ -n "${NOTARY_PROFILE:-}" ]]; then
    echo "==> 公证"
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
  fi
fi

echo "==> 完成：$DMG"
