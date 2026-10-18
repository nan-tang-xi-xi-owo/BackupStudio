#!/bin/sh
# 生成 RPM 源码包并构建：./backupstudio.spec -> dist/rpm/
#
# 只在 Fedora/RHEL/CentOS Stream/openEuler 等 dnf 系发行版上使用。
# 依赖：rpm-build、python3-devel、python3dist(pyside6-*)、python3dist(pyzmq) 等系统包。
set -eu
VERSION="$(python3 -c 'import sys; sys.path.insert(0, "."); import backupstudio; print(backupstudio.__version__)')"
TOPDIR="${TOPDIR:-$PWD/build/rpmbuild}"
NAME="backupstudio-${VERSION}"

rm -rf "$TOPDIR"
mkdir -p "$TOPDIR"/{BUILD,RPMS,SOURCES,SPECS,SRPMS,BUILDROOT}

# 源码包（只打 Python 包与打包脚本）
tar --exclude='./build' --exclude='./dist' --exclude='./.git' \
    --exclude='./tools' --exclude='__pycache__' --exclude='*.pyc' \
    --transform "s|^\./|${NAME}/|" \
    -czf "$TOPDIR/SOURCES/${NAME}.tar.gz" ./backupstudio ./packaging ./pyproject.toml \
        ./README.md ./LICENSE* ./run_backupstudio.py

cp packaging/rpmbuild/backupstudio.spec "$TOPDIR/SPECS/"
export _version="$VERSION"

rpmbuild -ba "$TOPDIR/SPECS/backupstudio.spec" \
  --define "_topdir $TOPDIR" \
  --define "_version $VERSION"

echo "==> RPM 输出："
find "$TOPDIR/RPMS" -name '*.rpm' -print
