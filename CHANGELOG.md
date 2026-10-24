# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## 1.1.0 — 2026-10-24

信任批次（可信备份身份层）：只有「卷齐全 + 校验通过」的归档才算一份备份。

### 修复（数据安全）
- **7z 假通过**：无 7-Zip 时不再用 `getnames()`（只读头）冒充校验，改为 py7zr `testzip()`
  读载荷并比对 CRC；API 不可用时返回「跳过」而不是「通过」。损坏的 7z 现在稳定判失败。
- **分卷归组回退**：恢复 `stem.7z.001/.002` 与 `base.part1.rar/part2.rar` 两种分卷的归组
  （此前 `.001` 完全不可见、`partN.rar` 被拆成多组，可能只删掉 part1 留下残缺包）。
  同时保证 `a.7z` 与 `a.7z.bak` 不会被误并为一份。
- **状态机修正**：`committing` 改为真实的两阶段提交（校验前先落盘一份临时清单），
  崩溃不再留下"永不被淘汰"的无清单残包；校验失败 → `partial`、未校验 → `unverified`，
  两者都不再算「可信备份」，且都会进入保留策略候选（不再永久占盘）。
- **炸弹防护不再自伤**：解压上限从「1000× 比例硬拒」改为「绝对上限（默认 100 GiB）+ 可放宽」，
  默认比例放宽到 10000×；新增 `--max-unpacked-bytes` / `--max-unpack-ratio` 与环境变量
  `BACKUPSTUDIO_MAX_UNPACKED_BYTES` / `BACKUPSTUDIO_MAX_UNPACK_RATIO`。
  8 MiB 全零日志（1014×）这类**本产品自己产出的正常备份**不再被拒绝。
  同时给 7z / rar 外部解压补上同样的绝对上限预检（此前完全没有防护）。

### 修复（CI / 打包）
- Windows 打包脚本在 cp1252 控制台下崩溃（`UnicodeEncodeError`）：新增 `packaging/_console.py`，
  在模块导入时即切换到 UTF-8；workflow 同时设 `PYTHONIOENCODING=utf-8`
  （`PYTHONUTF8` 会被 `PYTHONIOENCODING` 覆盖，只设前者不够）。
- `.desktop` 的 `Categories` 去掉未注册值 `Backup`，改用合法的 `Compression`（appimagetool 不再拒绝）。
- AppImage 补 `/usr/bin/backupstudio` 启动器（此前 `Exec`/`TryExec` 指向的文件不存在），
  `QT_QPA_PLATFORM` 改为 `wayland;xcb`。
- macOS runner 迁移：`macos-14` → `macos-15`、`macos-13`（已退役）→ `macos-15-intel`。
- Flatpak：镜像改用 `ghcr.io/flathub-infra/flatpak-github-actions:freedesktop-24.08`
  （原 tag 从未发布）；manifest 修掉 JSON 尾逗号、去掉 `--no-deps`、构建期允许网络。
- dmg 产物名加架构后缀，避免 arm64 / x64 互相覆盖。
- 版本号统一到 1.1.0，`packaging/build.py` 改为引用 `backupstudio.__version__`。
- 依赖钉上界：`PySide6-Essentials>=6.8,<6.13`、`pyinstaller>=6.3,<7`（消除不可复现构建）。

### 测试
- 新增 A1–A4 的回归用例（见 `tests/test_verify.py`、`test_manifest.py`、`test_extract.py`）。


## 1.0.0 — 2026-10-18

首个公开版本。

### 新增

- **多格式压缩备份**：ZIP、7z、RAR、tar、tar.gz、tar.bz2、tar.xz、tar.zst；
  每种格式都有首选引擎与内置后备引擎，外部程序不可用时可在「强制压缩」下自动降级。
- **灵活的备份来源**：整个文件夹，或文件夹内 **指定文件 / 子文件夹**（可多选）。
- **自定义备份位置**与文件名模板（`{name}` `{date}` `{time}` `{datetime}`）。
- **调度**：仅手动、每隔 N 分钟/小时/天、每天、每周（可多选星期）、每月 N 日；
  托盘菜单可随时「立即备份」。
- **密码保护**：7z/rar 使用 AES-256（7z 同时加密文件名），zip 支持 AES-256 / ZipCrypto；
  界面会标注 ZipCrypto 强度较弱。
- **分卷压缩**：7-Zip 原生 `.001/.002`、RAR 原生 `part1.rar`、通用字节切片 `.part01…`
  （任意格式可用；内容不足一卷时自动还原为单个完整压缩包）。
- **系统检测**：Windows（版本与构建号）、macOS 版本、Linux **发行版 ID / 名称 / 版本 / 同族 / 桌面环境 / 包管理器**。
- **依赖检测与安装建议**：`环境与压缩支持` 页与 `backupstudio info`、`doctor` 命令，
  按发行版给出 `apt / dnf / pacman / zypper / apk / brew / winget` 安装命令。
- **保留策略**：按份数、天数、总大小自动清理旧备份（可删除或移动到回收目录）。
- **校验**：备份完成后自动校验压缩包完整性，并在记录中展示重建出的文件清单。
- **托盘与后台运行**：托盘状态图标（空闲/运行中/成功/失败）、气泡通知与免打扰时段、
  关闭窗口驻留托盘；独立 **后台守护进程** 即使关闭主界面也按计划备份（本地 IPC 通信）。
- **开机自启**：Windows 注册表、macOS LaunchAgent、Linux XDG autostart / systemd 用户服务。
- **界面**：PySide6 实现，浅色/深色/跟随系统主题、卡片式布局、进度条、状态徽章；
  运行记录页可导出 CSV / JSON；任务配置可导出/导入（可选是否包含密码）。
- **命令行**：`gui / start / tray / task / run / info / doctor / selftest / extract / daemon`。
- **分发**：Windows exe（含便携 zip 与 Inno Setup 安装包脚本）、macOS `.app` → `.dmg`、
  Linux AppImage、Flatpak，以及 Fedora/RHEL 系 RPM spec；GitHub Actions 一键产出全部产物。

### 修复与加固（发布前自测发现）

- **托盘图标缺失**：打包时图标资源被压进 `base_library.zip` 导致运行时读不到，改为显式数据文件随包；
  托盘图标改为随包 PNG（不再依赖运行时 SVG 渲染），并增加每 30 秒自动补回与首次启动提示。
- **启动静默退出**：数据目录不可写时程序直接退出；现在配置/运行目录自动分层回退
  （配置 → %LOCALAPPDATA% → 临时目录；运行目录 → 临时目录），并写紧急日志、弹窗说明。
- **「停止」按钮无效**：取消信号未传入引擎；现在写入阶段与压缩阶段（含 7-Zip/RAR 子进程）都可中断，
  取消后不残留半成品。
- **强制 ZIP 不是真 ZIP**：内置引擎曾把 tar 容器写成 `.zip`；现在强制 ZIP 产出标准 ZIP（中文/emoji 文件名保留）。
- **解压安全**：重写解压模块，阻止 zip-slip / tar-slip 与指向外部的符号链接，先解到暂存目录再合并；
  错误密码与损坏包给出明确提示。
- **导入/配置健壮性**：允许 `str` 路径、跳过畸形任务条目、单个任务损坏不影响其它任务。
- **CLI**：`--version/--help` 退出码修正、日志中文编码修正、新增 `task show`。

### 已知限制

- RAR 为专有格式：需要用户自行安装 WinRAR / RAR；未注册版只能解压。
  无 RAR 引擎时「强制压缩」会用内置引擎生成「7z 内容 + .rar 扩展名」，结果中会明确提示。
- ZIP 的 AES-256 加密需要完整版 7-Zip（`7z.exe`）；随包精简版 `7za` 仅支持 ZipCrypto。
- tar 系格式原生不支持加密与分卷，加密需系统 `gpg`，分卷使用通用切片。
- 备份密码以明文保存在配置文件中（文件权限 0600），以便无人值守加密备份。
