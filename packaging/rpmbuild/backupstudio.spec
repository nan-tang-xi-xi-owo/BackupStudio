# 由 packaging/make-rpm.sh 调用：rpmbuild -ba backupstudio.spec --define "_topdir ..." --define "_version x.y.z"
%global _version %{?_version}%{!?_version:1.0.0}

Name:           backupstudio
Version:        %{_version}
Release:        1%{?dist}
Summary:        Scheduled folder backups with multi-format compression
Summary(zh_CN): 定时文件夹备份与多格式压缩工具
License:        MIT
URL:            https://github.com/backupstudio/backupstudio
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  rpm-build

%description
BackupStudio backs up folders (or selected files inside a folder) on a schedule
and compresses them with ZIP, 7z, RAR or tar family formats, with password
protection and split volumes. It runs a tray icon, a background scheduler and a
command line interface.

%description -l zh_CN
备份工坊（BackupStudio）可以按间隔/每天/每周/每月自动把文件夹（或文件夹中指定的文件）
压缩备份到自定义位置，支持 ZIP、7z、RAR、tar、tar.gz、tar.bz2、tar.xz、tar.zst，
支持密码加密与分卷，附带托盘图标、后台守护进程和命令行接口。

%package -n python3-%{name}
Summary:        Python library and CLI for BackupStudio
Requires:       python3dist(pyside6-essentials) >= 6.5
Requires:       python3dist(pyside6-addons) >= 6.5
Requires:       python3dist(pyzmq) >= 25
Requires:       python3dist(py7zr) >= 0.21
Requires:       python3dist(rarfile) >= 4.1
Requires:       python3dist(zstandard) >= 0.22
Suggests:       p7zip

%description -n python3-%{name}
Python 3 模块与命令行工具（backupstudio）。

%prep
%setup -q

%build
%pyproject_wheel

%install
%pyproject_install
mkdir -p %{buildroot}%{_datadir}/applications \
         %{buildroot}%{_datadir}/icons/hicolor/512x512/apps \
         %{buildroot}%{_datadir}/metainfo
install -Dm644 packaging/io.github.backupstudio.BackupStudio.desktop \
    %{buildroot}%{_datadir}/applications/io.github.backupstudio.BackupStudio.desktop
install -Dm644 packaging/icon.png \
    %{buildroot}%{_datadir}/icons/hicolor/512x512/apps/io.github.backupstudio.BackupStudio.png
install -Dm644 packaging/io.github.backupstudio.BackupStudio.metainfo.xml \
    %{buildroot}%{_datadir}/metainfo/io.github.backupstudio.BackupStudio.metainfo.xml

%files
%{_bindir}/backupstudio
%{_bindir}/backupstudio-gui
%{_datadir}/applications/io.github.backupstudio.BackupStudio.desktop
%{_datadir}/icons/hicolor/512x512/apps/io.github.backupstudio.BackupStudio.png
%{_datadir}/metainfo/io.github.backupstudio.BackupStudio.metainfo.xml

%files -n python3-%{name}
%{python3_sitelib}/backupstudio
%{python3_sitelib}/backupstudio-*.dist-info

%changelog
* Sat Oct 24 2026 BackupStudio contributors <noreply@example.com> - 1.1.0-1
- Trust batch: retention is task-scoped, tar-family verification detects corruption,
  split volumes restore in-app, encryption state is probed, cross-process locking hardened

* Sat Oct 18 2026 BackupStudio contributors <noreply@example.com> - 1.0.0-1
- Initial release
