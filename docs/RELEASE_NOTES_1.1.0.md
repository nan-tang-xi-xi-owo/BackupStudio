# BackupStudio 1.1.0

跨平台定时备份与多格式压缩工具 —— 「可信批次」：把“备份可信、出事能找回来”从口号变成可验证的行为。

## 下载

| 平台 | 文件 | 说明 |
| --- | --- | --- |
| Windows 10/11 (x64) | `BackupStudio-1.1.0-windows-x64.zip` | 解压后运行 `BackupStudio.exe`（已内置 7-Zip 命令行） |
| Windows 10/11 (x64) | `BackupStudio-Setup-1.1.0.exe` | 图形化安装程序 |
| macOS 12+ (Apple Silicon) | `BackupStudio-1.1.0-arm64.dmg` | 拖入「应用程序」；未签名版本请右键 → 打开 |
| macOS 12+ (Intel) | `BackupStudio-1.1.0-x64.dmg` | 同上 |
| Linux x86_64 | `BackupStudio-1.1.0-x86_64.AppImage` | `chmod +x` 后直接运行（需 libfuse2） |
| Linux (通用) | `BackupStudio.flatpak` | `flatpak install --user *.flatpak` |

> 校验值见本页下方的 `SHA256SUMS.txt`。

## 本版要点

### 数据安全（会真的丢数据的问题）

- **保留策略不再跨任务误删**：多个任务备份到同一目录时，每个任务只认领自己的备份，
  不再互相删除对方的历史包。
- **校验失败不再挤掉好备份**：保留清理只在本轮备份成功时执行，一次损坏不会让目录里
  只剩下坏包。
- **崩溃残包自动清扫**：强杀或断电留下的临时文件与暂存目录，在下次启动时清理；
  仍在运行的任务不受影响。
- **文件命名更可靠**：任务名含 `.zip` / `.7z` 等字样时，不再出现两轮备份共用一个
  记录、导致旧包永远清不掉的问题；保留份数的“差一”问题一并修正。

### 校验与恢复

- **tar 系校验不再假通过**：tar.gz / tar.bz2 / tar.xz / tar.zst 的内容损坏（如位翻转）
  现在能被真正检出，此前会一律报告“校验通过”。tar.zst 同时启用帧校验和。
- **分卷备份可直接恢复**：`.part01/02/…` 分卷可由程序自行识别、按数值顺序拼接并解压，
  缺卷时会精确提示缺哪一卷。
- **tar.zst 解压不再假成功**：没有 zstd 命令行时，曾只剥一层压缩就报告“已解压”，
  实际没有解出内容。
- **分卷加密状态实测**：分卷产物先拼接再检测加密；无法判定时如实记为“未加密”并给出
  告警，绝不再把未加密的包记成已加密。

### 加密与密码

- **7z + ZipCrypto 组合在创建前明确拒绝**：此前会静默丢弃密码、产出明文包。
- **ZIP 密码含中文等非 ASCII 字符时提前拦截**：给出可行动的中文提示并建议改用 7z，
  不再以乱码报错收场（这是 7-Zip 的格式限制）。
- **中文报错不再乱码**：所有 7-Zip 子进程统一按 UTF-8 输出。
- **加密方式不再被静默改写**：编辑任务时，zip 的 AES-256 不会被悄悄降级为较弱的
  ZipCrypto；tar + gpg 等组合也不再被改成必然失败的配置。

### 锁与并发

- **跨进程锁加固**：修掉锁接管竞态下两个进程同时持有同一任务锁（会产出坏包）、
  空锁或半截锁导致任务永久卡死、进程号被复用时锁永不释放等问题。
- **配置文件原子写入**：多进程并发保存不再损坏配置。
- **后台服务 IPC 加固**：收到畸形请求后不再永久失去响应，停止/重载命令始终可用。
- **界面与外部变更同步**：界面打开期间由命令行或计划任务新增/修改的任务会实时同步，
  不再被界面下一次保存覆盖掉。

### 界面

- **不再无限重试弹窗**：密码/参数类失败不再反复弹出“改用内置引擎重试”（曾两分钟内
  弹出数百次）；仅在用户主动点击「立即备份」时允许重试。
- **编辑任务保真**：不再丢第二个备份来源；7z + 分卷等命令行/导入产生的合法组合，
  在界面中打开再保存不会被拒绝。

### 打包与发布

- 修复 Windows 安装包构建失败（Inno 注释语法与中文语言文件缺失）。
- 修复 Flatpak 与 AppImage 打包任务；macOS Intel 构建迁至仍受支持的运行环境。
- 发布产物按平台/架构区分命名，macOS 的 arm64 与 x64 安装镜像不再互相覆盖。

## 已知限制

- 裸 `.tar` 格式本身不带校验信息，位翻转检测需要逐文件哈希，留待后续版本。
- **全平台产物均未做代码签名；macOS 版未公证（notarization）。**
  本版 Windows 的 `BackupStudio.exe` / `7za.exe` / `7zr.exe` 与安装包经
  `Get-AuthenticodeSignature` 检查均为 `NotSigned`；macOS CI 日志明示「跳过签名
  （未设置 CODESIGN_IDENTITY）」。原因是没有购买 Apple Developer Program 与
  Windows 代码签名证书，**并非构建配置遗漏**。
  我们不会用自签名证书伪装已签名版本，而是如实告知。首次运行的预期提示：
  - Windows：SmartScreen「Windows 已保护你的电脑」→ 点「更多信息」→「仍要运行」。
  - macOS：「Apple 无法检查是否包含恶意软件」→ 右键（Control 点按）→「打开」，
    或执行 `xattr -dr com.apple.quarantine /Applications/BackupStudio.app`。
  - Linux/Flatpak：不受二进制签名影响，AppImage 需先 `chmod +x` 并安装 `libfuse2`。
  运行前请用本页 `SHA256SUMS.txt` 核对校验值；详见 README 的「平台信任提示」章节。
- Split 分卷、加密包等组合对旧版本产物的兼容性以实际格式为准，导入旧包前建议先在
  副本上验证。

## 已知问题

完整的现象、影响范围与**临时规避步骤**见 **[KNOWN-ISSUES.md](KNOWN-ISSUES.md)**。

本版发布时仍有以下三条残留缺陷（均已在后续修复，并带回归测试）：

| 编号 | 级别 | 一句话描述 | 状态 |
| --- | --- | --- | --- |
| S2 | 数据丢失 | 「清单还在、卷全丢」的幽灵组会被算作一份备份，`keep_last=N` 时可能**只保留 N−1 份真备份** | 已修复，见 KNOWN-ISSUES |
| S1 | 安全 | 对声明大小为空的流式容器（如 7-Zip 眼中的 `.tar.zst`），炸弹预检会**静默放行** | 已修复，见 KNOWN-ISSUES |
| S3 | 体验 | 内置 ZIP 后端忽略压缩级别，级别 1 与 9 产出**完全相同**的字节 | 已修复，见 KNOWN-ISSUES |

若你使用的是 1.1.0 的正式二进制产物，请先阅读 KNOWN-ISSUES 的「临时规避」小节，
尤其是 S2 —— 保守做法是把清理方式保持在默认的 `move`（先进回收站），
并定期核对 `<备份目录>/.backupstudio/manifests/` 下的清单与盘上文件是否对得上。

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
