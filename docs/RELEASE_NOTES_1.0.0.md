# BackupStudio 1.0.0

跨平台定时备份与多格式压缩工具 —— 首个公开版本。

## 下载

| 平台 | 文件 | 说明 |
| --- | --- | --- |
| Windows 10/11 (x64) | `BackupStudio-1.0.0-windows-x64.zip` | 解压后运行 `BackupStudio.exe`（已内置 7-Zip 命令行） |
| macOS 12+ (Apple Silicon / Intel) | `BackupStudio-1.0.0-macos-(arm64\|x64).dmg` | 拖入「应用程序」；未签名版本请右键 → 打开 |
| Linux x86_64 | `BackupStudio-1.0.0-x86_64.AppImage` | `chmod +x` 后直接运行（需 libfuse2） |
| Linux (通用) | `io.github.backupstudio.BackupStudio.flatpak` | `flatpak install --user *.flatpak` |

> 校验值见本页下方的 `SHA256SUMS.txt`。

## 亮点

- **8 种压缩格式**：zip / 7z / rar / tar / tar.gz / tar.bz2 / tar.xz / tar.zst
- **备份整个文件夹，或文件夹里指定的文件**
- **自定义时间**：每隔 N 分钟/小时/天、每天、每周、每月，也可以随时手动备份
- **密码加密**（AES-256 / ZipCrypto）与 **分卷压缩**
- **自动检测操作系统与缺失的压缩软件**，按发行版给出安装命令
- **系统托盘 + 后台守护进程**：关掉窗口也照常按计划备份
- **保留策略**：按份数/天数/总大小自动清理旧备份
- **备份后自动校验**，运行记录可导出 CSV / JSON

## 本版修复（自测发现）

- 修复打包版 **托盘图标不显示**
- 修复数据目录不可写时 **程序静默退出**（现在自动回退并提示）
- 修复「停止」按钮 **无法中断压缩**
- 修复「强制压缩」生成的 ZIP **实际是 tar 容器**
- 加固解压逻辑，阻止 **zip-slip / tar-slip** 与指外部链接
- 修复 CLI `--version` 退出码、日志中文乱码，新增 `task show`

## 系统依赖

| 功能 | 需要 |
| --- | --- |
| ZIP / 7z | Windows 包已内置 7za；其它平台建议安装 7-Zip（`p7zip-full` / `p7zip` / `7zip`），ZIP 的 AES-256 需完整版 `7z.exe` |
| RAR | 需自行安装 WinRAR / RAR（专有软件，未随包分发） |
| tar 系 | 系统自带 tar（Windows 10+ 自带 bsdtar）；`.tar.zst` 需 `zstd` |
| 加密 tar | 系统 `gpg` |
| 托盘通知（Linux） | `libnotify` / `notify-send` |

## 从源码运行

```bash
pip install -r requirements.txt
python -m backupstudio          # 图形界面
python -m backupstudio info     # 环境与压缩支持
```

完整说明见仓库 [README](https://github.com/nan-tang-xi-xi-owo/BackupStudio#readme)。

## 许可

MIT。第三方组件（Qt/PySide6 LGPL-3.0、7-Zip LGPL+unRAR 限制、RAR 专有）见 `THIRD-PARTY-NOTICES.txt`。
