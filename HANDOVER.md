# 项目交接文档 —— BackupStudio（备份工坊）

> 生成时间：2026-11-13
> 交接原因：更换设备，由新 AI 接替
> 本文档目标：让接手的 AI 在人不在场的情况下，能独立继续开发

---

## 0. 三十秒速览

| 项目 | 值 |
|---|---|
| 项目根目录 | `C:\APP\deepseek\work\定时备份及其压缩项目` |
| 应用名 | BackupStudio（备份工坊） |
| 当前版本 | **1.2.0** |
| 技术栈 | Python 3.15 + PySide6-Essentials 6.12.0 + PyInstaller 6.22.3 |
| 许可证 | MIT |
| GitHub 仓库 | https://github.com/nan-tang-xi-xi-owo/BackupStudio |
| 当前分支 | `merge/1.1-review` @ `62475ae` |
| 远端 main | `26ba861`（**已含全部内容，包括本文档**） |
| 测试基线 | **483 passed / 0 failed** |
| 已发布版本 | v1.1.0、v1.1.1、**v1.2.0** |
| 磁盘剩余 | 85.7 GB |

---

## 1. GitHub Token：已存到项目内

**状态：可用，位置已加固。**

### 现状

Token 是 40 字符的 OAuth token（`gho_` 前缀），现在存在**两处**：

```
C:\APP\deepseek\work\定时备份及其压缩项目\.gh-token   ← ✅ 主用（稳定）
%TEMP%\gh-tok.txt                                    ← 备份（易被清理）
```

**`.gh-token` 已被 `.gitignore` 挡住**，已做三重验证：

```powershell
git check-ignore -v .gh-token     # → .gitignore:24:.gh-token
git ls-files --error-unmatch .gh-token   # → error: did not match（未追踪）
git add -n .gh-token              # → paths are ignored by .gitignore（被拦截）
git status --ignored              # → !! .gh-token（标记为 ignored）
```

> ⚠️ **这个仓库是公开的**（`private=False`）。**绝对不要** `git add -f .gh-token`
> 或把 token 写进任何被追踪的文件。`.gitignore` 里已加规则：
> ```
> .gh-token
> gh-tok.txt
> gh-token.txt
> *.token
> .env
> .env.*
> ```

### 脚本里怎么读 token

统一用这个模式（先找项目内，再退回 %TEMP%）：

```python
from pathlib import Path
import tempfile

PROJ = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目")
TOK_FILE = PROJ / ".gh-token"
if not TOK_FILE.is_file():
    TOK_FILE = Path(tempfile.gettempdir()) / "gh-tok.txt"
TOK = TOK_FILE.read_text().strip()
```

### 如果 token 又失效了，怎么找回

优先查 **Windows 凭据管理器**（GCM 存的是 OAuth token，不会过期那么快）：

```powershell
cmdkey /list | findstr github
# 应看到：Target: LegacyGeneric:target=git:https://github.com

$gcm = 'C:\APP\Git\ucrt64\bin\git-credential-manager.exe'
$env:GCM_INTERACTIVE='never'
$inp = "protocol=https`nhost=github.com`nusername=nan-tang-xi-xi-owo`n`n"
$out = $inp | & $gcm get 2>&1
($out | Where-Object { $_ -match '^password=' }) -replace '^password=',''
```

如果凭据管理器里也没了，就重新授权：

```powershell
$env:GCM_INTERACTIVE='always'
$env:GIT_TERMINAL_PROMPT='1'
& 'C:\APP\Git\cmd\git.exe' push origin HEAD:main
# 会弹出 "Connect to GitHub" 窗口，按提示走 device code 授权
# 拿到后写回 .gh-token：
#   Set-Content .gh-token $token -NoNewline
```

**注意**：必须**在前台**执行（不要在后台 job 里跑），否则
`bash: /dev/tty: No such device or address`，窗口弹不出来 —— 这是本机踩过的坑。

### 各操作当前可用性

| 操作 | 能否做 |
|---|---|
| `git fetch` / `git ls-remote`（读） | ✅ 能（公开仓库，无需 token） |
| `git clone`（读） | ✅ 能 |
| `git push`（写） | ⚠️ 能，但**网络极不稳**（见第 1.5 节，建议改走 API） |
| GitHub API 写操作（建 release、上传 asset） | ✅ 能（API 快且稳） |

---

## 1.5 ⚠️ 推送必须走 Trees API，不要用 git push

**这是本机最耗时的一个坑，务必先读。**

实测网络：

| 端点 | 响应时间 | 稳定性 |
|---|---|---|
| `github.com`（`git push` 走这个） | **19.7 秒** | 频繁 `Connection was reset` / `SSL/TLS handshake failed` |
| `api.github.com`（API 走这个） | **0.7 秒** | 稳定 |

`git push` 实测失败信息：

```
schannel: failed to receive handshake, SSL/TLS connection failed
Recv failure: Connection was reset
fatal: unable to access ... Connection timed out after 300053 milliseconds
```

**所以推送要用 GitHub Trees API**：读远端 tree → 逐文件比对 blob sha → 只上传变化的 → 建新 tree/commit → PATCH ref。

脚本模板（本次用的，可复用）：

```python
import base64, json, subprocess, tempfile
from pathlib import Path

TOK = (Path(tempfile.gettempdir()) / "gh-tok.txt").read_text().strip()
API = "https://api.github.com/repos/nan-tang-xi-xi-owo/BackupStudio"
REPO = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目")

def api(method, url, payload=None):
    cmd = ["curl.exe", "-s", "-X", method,
           "-H", f"Authorization: token {TOK}", "-H", "User-Agent: bs",
           "-H", "Accept: application/vnd.github+json", "--max-time", "180"]
    tmp = None
    if payload is not None:
        fd, tmp = tempfile.mkstemp(suffix=".json"); os.close(fd)
        Path(tmp).write_text(json.dumps(payload), encoding="utf-8")
        cmd += ["-H", "Content-Type: application/json", "--data-binary", f"@{tmp}"]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True)
    return json.loads(r.stdout.decode("utf-8", "replace") or "{}")

# 1) 取远端 ref → commit → tree（recursive 拿全部 blob sha）
ref = api("GET", f"{API}/git/ref/heads/main")
rsha = ref["object"]["sha"]
tr = api("GET", f"{API}/git/trees/{rsha}?recursive=1")
remote_files = {e["path"]: e["sha"] for e in tr["tree"] if e["type"] == "blob"}

# 2) 本地逐文件算 blob sha（git hash-object 与 GitHub 算法一致）
changed = []
for rel in subprocess.run(["git","ls-files"], cwd=REPO, capture_output=True,
                          text=True, encoding="utf-8").stdout.split():
    q = REPO / rel
    if q.is_file():
        blob = subprocess.run(["git","hash-object",str(q)], cwd=REPO,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()
        if remote_files.get(rel) != blob:
            changed.append(rel)

# 3) 上传 blob → 建 tree → 建 commit → 改 ref
base_tree = api("GET", f"{API}/git/commits/{rsha}")["tree"]["sha"]
entries = []
for rel in changed:
    bl = api("POST", f"{API}/git/blobs", {
        "content": base64.b64encode((REPO/rel).read_bytes()).decode(),
        "encoding": "base64"})
    entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": bl["sha"]})
newtree = api("POST", f"{API}/git/trees", {"base_tree": base_tree, "tree": entries})
msg = subprocess.run(["git","log","-1","--pretty=%B"], cwd=REPO,
                     capture_output=True, text=True, encoding="utf-8").stdout.strip()
nc = api("POST", f"{API}/git/commits",
         {"message": msg, "tree": newtree["sha"], "parents": [rsha]})
api("PATCH", f"{API}/git/refs/heads/main", {"sha": nc["sha"], "force": False})
```

**这套做法的副作用**：远端 commit sha 与本地不同（内容 tree 相同）。所以下一次 `git log origin/main..HEAD` 会把**所有历史提交**都列出来，看着像"72 个提交未推送"，但实际差异可能只有 1-2 个文件。**判断是否同步要看 `git diff --stat origin/main HEAD`，不要看提交数。**


---

## 2. 仓库状态（本地 vs 远端）

```
本地 HEAD (merge/1.1-review) = 62475ae
远端 main                    = 26ba861   ← 已含全部内容，含本文档
远端 fix/1.1-review          = 1843644
远端 fix/1.1-ui              = a20a47c   ← 别人的 PR #4，未合并
```

### 关键结论：本地与远端内容**完全一致**（已逐字节核实）

```
git diff --stat origin/main HEAD   → （空）

逐文件比对：
✅ HANDOVER.md          29502B = 29502B  一致
✅ packaging/installer.iss.in  2942B = 2942B  一致
```

`main` 上确认存在这些新文件（`git cat-file -e origin/main:<path>` 核实）：

```
✓ backupstudio/gui/styles.py      六种风格定义
✓ backupstudio/gui/mcu.py         Material You 65 色
✓ backupstudio/gui/glass.py       液态玻璃折射算法
✓ backupstudio/gui/titlebar.py    自绘标题栏
✓ backupstudio/gui/winframe.py    路线 C 原生框架位
✓ backupstudio/gui/winchrome.py   原生标题栏深色兜底
✓ HANDOVER.md                     本文档
```

版本号（远端）：`__version__ = "1.2.0"`、`pyproject.toml` 的 `version = "1.2.0"`、CHANGELOG 有 `## 1.2.0`。

> **注意**：远端 `main` 的 commit sha 是 `26ba861`，本地是 `62475ae` —— **sha 不同但 tree 内容相同**。这是 Trees API 推送的必然结果（见第 1.5 节）。**判断是否同步要看 `git diff --stat`，不要看 commit sha 或提交数。**

### 本地分支说明

| 分支 | 说明 |
|---|---|
| `merge/1.1-review` | **当前分支，就是 HEAD** |
| `main` | 本地 main 是旧的（`5111f12`），**不要用**，以 `origin/main` 为准 |
| `fix/1.1-review` | 本地这个分支也是旧的（`0420bf6`），不用管 |
| `wip/zip-direct-pack-abandoned` | 已废弃的 7-Zip ZIP 直打实验，仅存档 |

> **接手后建议**：在新设备上 `git clone` 后直接以 `origin/main` 为基线工作，不要管上面这些本地分支 —— 它们只在旧设备上有意义。

---

## 3. 项目结构与核心文件

```
定时备份及其压缩项目/
├── backupstudio/                 核心代码（约 2.1 MB）
│   ├── __init__.py               __version__ = "1.2.0"  ← 版本唯一真源
│   ├── app.py                    QApplication 启动、CLI 分发
│   ├── engine.py                 备份引擎
│   ├── archives.py               压缩格式后端（zip/7z/rar/tar*）
│   ├── extract.py                解压 + 炸弹防护
│   ├── store.py                  配置持久化
│   ├── config.py                 AppSettings 数据类
│   ├── paths.py                  配置/运行/日志目录 + 可写性探测
│   ├── filelock.py               跨进程锁
│   ├── staging.py                暂存区
│   ├── manifest.py               清单
│   ├── longpath.py               长路径支持
│   ├── capabilities.py           能力探测
│   ├── tools.py                  7-Zip 工具封装（含 lru_cache）
│   ├── notify.py                 系统通知
│   └── gui/                      ← 本次改动的重点
│       ├── __init__.py           Palette + LIGHT/DARK + stylesheet()
│       ├── styles.py             ★ 六种风格 Style + styled_palette()
│       ├── mcu.py                ★ Material You（HCTLAB 近似 CAM16-HCT）
│       ├── glass.py              ★ 液态玻璃：盒式模糊 + 位移折射 + 色散
│       ├── titlebar.py           ★ 自绘标题栏控件
│       ├── winframe.py           ★ 路线 C：原生框架位 + NCCLCSIZE + NCHITTEST
│       ├── winchrome.py          ★ 原生标题栏深色（无自绘时的兜底）
│       ├── main_window.py        主窗口 + 懒加载页面
│       ├── appstate.py           AppState（theme_changed 信号）
│       ├── widgets.py            Card/Badge/Switch/ElideLabel/Combo 等
│       ├── tasks_page.py         备份任务页（含批量删除）
│       ├── history_page.py       运行记录页（分页收纳）
│       ├── environment_page.py   环境与压缩支持页
│       ├── settings_page.py      设置页（外观风格 + Material You）
│       ├── task_dialog.py        新建/编辑任务对话框
│       ├── icons.py              线性图标（Qt 直绘）
│       └── worker.py             后台线程
├── tests/                        28 个测试文件
│   └── test_ui_1_1_2.py          ★ 35 条 UI 回归测试（本次新增）
├── packaging/                    打包素材与脚本
│   ├── build.py                  ★ 主打包脚本（PyInstaller）
│   ├── make-appimage.sh          ★ AppImage 打包
│   ├── make-windows-installer.ps1  Setup.exe 打包
│   ├── installer.iss.in          ★ Inno Setup 模板
│   ├── io.github.backupstudio.BackupStudio.yml   Flatpak manifest
│   ├── gen-flatpak-manifest.py   manifest 校验/更新
│   ├── icon.ico / icon.icns / icon.png / icon.svg
│   └── io.github.backupstudio.BackupStudio.desktop / .metainfo.xml
├── docs/
│   ├── KNOWN-ISSUES.md           已知问题
│   ├── RELEASE_REPORT_1.1.1.md
│   ├── UI_REDESIGN_1.1.2.md
│   └── screenshots/              实拍截图（styles/ 子目录有六风格样张）
├── dist/                         产物（587.7 MB，gitignored）
├── dev/                          验证脚本（大部分已清理）
└── .github/workflows/build.yml   CI
```

★ = 本次（1.2.0）新增或大改

---

## 4. 测试：怎么跑，基线是多少

### 正确跑法（**必须加 7-Zip 到 PATH**）

```powershell
cd "C:\APP\deepseek\work\定时备份及其压缩项目"
$env:PYTHONUTF8='1'
$env:PATH="C:\APP\deepseek\work\定时备份及其压缩项目\tools\win32\7z;"+$env:PATH
New-Item -ItemType Directory -Force build\t | Out-Null
& 'C:\Python315\python.exe' -m pytest tests -q --basetemp='build\t'
```

### 基线

```
483 passed / 0 failed    ← 这是验收线
```

### 三个必须知道的坑

**坑 1：不加 7-Zip 到 PATH 会有 12 个假失败**

失败点在 `archives.py:1499`，报 `System ERROR`。这不是代码 bug，是测试环境问题。

**坑 2：`--basetemp` 必须指向工作区内、且父目录要预先建好**

```powershell
New-Item -ItemType Directory -Force build\t | Out-Null   # 必须先建
```
否则报 `FileNotFoundError`，会显示成 30 个 error（不是 test failed）。

**坑 3：`pytest-qt` 没装**

现有测试自己建 `QApplication` + 设 `QT_QPA_PLATFORM=offscreen`。写新 UI 测试要沿用这个模式：

```python
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
```

**注意**：`offscreen` 平台**不处理焦点**（`setFocus()` 恒返回 False）。真要测焦点得用 `minimal` 平台。

---

## 5. 本次（1.2.0）做了什么

### 5.1 六种外观风格

`backupstudio/gui/styles.py`。与「明暗主题」**正交**：风格管几何质感（圆角/立体/渐变/透明度），配色管 light/dark。

| key | 名称 | 关键特征 |
|---|---|---|
| `win` | Windows 11 | 8px 圆角、1px 描边、靛蓝 `#3E5DEB` |
| `mac` | macOS | 14px 大圆角、**无描边靠投影分层**、系统蓝 `#007AFF` |
| `98` | Windows 98 | 全直角、3D 凸起边框、灰 `#C0C0C0`、深蓝 `#000080` |
| `skeuo` | 拟物 | 2px 厚边 + 渐变 + 强高光、暖皮革 `#8B5A2B` |
| `mag` | Material You | 卡片无边框、药丸按钮、动态配色 |
| `glass` | 液态玻璃 | 半透明 + 位移折射 + 多彩光斑背景 |

设置页「外观风格」下拉切换。`Palette` 有 20 个字段，`styled_palette(p, key)` 按风格返回独立配色。

**量化验证**：六风格两两渲染差异 55%~99.6%（修复前 `mac vs win` 仅 5.9%）。

### 5.2 Material You 动态配色

`backupstudio/gui/mcu.py`。按 AOSP 规格：**单一源色 → 5 套色调板 × 13 档 = 65 色**。

色度/色相规则（照文档）：

| 板 | CAM16 色度 | 色相 |
|---|---|---|
| `accent1` | 40（tone 尾数 0/10/50）/ 48（其余） | 同源色 |
| `accent2` | 16 | 同源色 |
| `accent3` | 32 | **+60°** |
| `neutral1` | 4 | 同源色 |
| `neutral2` | 8 | 同源色 |

支持四种 `theme_style`：`TONAL_SPOT` / `VIBRANT` / `EXPRESSIVE` / `SPRITZ`。

**重要限制（必须如实告知用户）**：用 **CIELAB-LCh 近似** AOSP 的 **CAM16-HCT**，色度按 `LAB_CHROMA_PER_CAM16 = 0.85` 折算。**观感接近但非逐位一致** —— 同一源色得到的十六进制值与 Pixel 手机不同。要逐位一致需移植 `material-color-utilities` 的 CAM16 求解器（约 400 行，含色适应 + 色域映射迭代）。

**另一处偏离**：AOSP 文档表写浅色主题 `background = neutral1_10`，但 tone 10 是近黑（L\*=10），与浅色矛盾。已按实际配色常识实现：**浅色背景用 tone 1000（近白）、深色用 tone 10（近黑）**，代码注释里标了这处偏离。

### 5.3 液态玻璃（真折射）

`backupstudio/gui/glass.py` + `widgets.py` 的 `Card.paintEvent`。

QSS 没有 `backdrop-filter`、没有 `blur()`，所以自绘：

1. 采样控件**背后**的窗口像素（`window().render()`）
2. **降采样到最长边 96px** 再算（计算量降到约 1/20）
3. 三次盒式模糊（半径 3/2/1）
4. **位移场重采样 = 真折射**（`displace()`，边缘弯折，中心不动）
5. RGB 三通道水平错位 1px（色散）
6. 顶部斜向高光 + 内高光边 + 圆角裁切
7. 平滑放大回原尺寸

**关键参数（别乱改，都是实测定下来的）**：
- 模糊层不透明度 **0.55**（原来是 0.92 → 会把背景糊成实心，看起来是磨砂白板）
- tint alpha **0.10**（深色 0.14）
- 折射 `strength=1.0`、`radius=18`、`edge = min(w,h)//3`
- 只给**顶层** `Card`（`objectName()=="Card"`）上玻璃，`CardFlat` 保持实心

配合 `main_window.py` 的 `_Backdrop`：玻璃风格下主窗口自绘 4 个径向渐变光斑（紫/青/粉/橙）。**折射需要背后有明暗变化才看得出**，纯色背景模糊后还是纯色。

### 5.4 自绘标题栏（Windows，路线 C）

**为什么必须自绘**（本机 Win10 19044 实测 `DwmSetWindowAttribute` 返回码）：

```
DWMWA_USE_IMMERSIVE_DARK_MODE (20)   S_OK          ✅ 只有这个能用
DWMWA_WINDOW_CORNER_PREFERENCE (33)  E_INVALIDARG  ❌
DWMWA_BORDER_COLOR (34)              E_INVALIDARG  ❌
DWMWA_CAPTION_COLOR (35)             E_INVALIDARG  ❌
DWMWA_TEXT_COLOR (36)                E_INVALIDARG  ❌
```

即 **Win10 只能用 DWM 切深浅色，完全无法给标题栏自定义颜色**。

**路线演进**（记录下来，别再走弯路）：
1. 先试 DWM 属性 → 不可行（见上表）
2. 退到「纯 Frameless + Qt 自绘」（路线 B）→ 实测 `WM_NCHITTEST` **全部返回 HTCLIENT**，没有原生边缘缩放与 Aero Snap
3. 最终用**路线 C**（`winframe.py`）：把 `WS_CAPTION|WS_THICKFRAME|WS_SYSMENU|WS_MINIMIZEBOX|WS_MAXIMIZEBOX` **OR 回去** + `WM_NCCALCSIZE` 归零 + 自己实现 `WM_NCHITTEST` 分区

路线 C 实测结果：

```
窗口样式  0x960B0000 → 0x96CF0000
标题栏中央 →  2 HTCAPTION    （走系统 SC_DRAGMOVE 原生拖拽回路）
上边缘     → 12 HTTOP        ← 边缘判定必须优先于标题带
左上角     → 13 HTTOPLEFT
左边缘     → 10 HTLEFT
右边缘     → 11 HTRIGHT
下边缘     → 15 HTBOTTOM
客户区     →  1 HTCLIENT
```

**只在 Windows 启用**（`prefer_frameless()`）。macOS 保留系统标题栏（无边框会丢红绿灯按钮、全屏动画），Linux 各桌面环境标题栏规则不一。可用环境变量 `BACKUPSTUDIO_FRAMELESS=0/1` 强制开关。

### 5.5 性能优化

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 首屏（玻璃风格） | 12.9s | 1.41s |
| 切到「环境与压缩支持」 | 12.8s | 0.55s |
| 切到「设置」 | 9.3s | 0.22s |
| 切到「运行记录」 | 3.5s | 0.29s |
| 打开运行记录 | 卡顿 | 0.20s |

手段：
- 玻璃绘制降采样（96px）+ 只给顶层卡片 + 背景光斑缓存
- 运行记录**分页收纳**：`history.jsonl` 有 4651 条，默认只渲染最近 **100** 条，底部「显示更多」(+200) / 「全部显示」
- 关掉 `QHeaderView.ResizeToContents`（它对每列测量**所有行**，行多时极慢），改固定列宽 + `ElideMiddle`
- `AppState.history(limit=3000)`（原 300）

### 5.6 可用性

- **任务页一键勾选 + 批量删除**：右上「批量管理」→ 每项出现复选框 → 底部 `[全选] 已选 N/M [删除选中] [退出批量]`，二次确认明确写「备份文件不会被删除」

### 5.7 修掉的真 bug（每条都有实拍/像素证据）

| 缺陷 | 根因 |
|---|---|
| **切风格没区别** | `Card.set_glass()` 设了内联 `background: transparent`，关闭时**没清除**。内联优先级高于 QSS → 一旦用过玻璃，卡片永远透明。且 `_install_glass` 只处理顶层 Card，14 张 `CardFlat` 永久残留 |
| **点下拉时窗口被拖走**（"莫名往下滑"） | `mousePressEvent` 拖动判定只查上界 `y <= TITLE_H`，**没有下界**。滚动区内容滚出可视区后控件 y 为负数，点击被误判成"按在标题栏" |
| **下拉弹层黑块** | 弹层是**独立的顶层 QFrame**，只写 `QComboBox QAbstractItemView` 覆盖不到。补 `combobox-popup: 0` + `QComboBox QFrame` |
| **设置页外观区被截断** | 5 组控件塞进一个 `QHBoxLayout`，水平布局不换行 |
| **玻璃在滚动区看不出折射** | `QScrollArea` 的 viewport 不透明，采样到纯色底 |
| **滚动条是贴边的深色竖条** | 10px 实色 `border_strong` 贴窗口边缘，视觉突兀 |
| **标题栏配色不跟随** | `stylesheet()` 内部调 `styled_palette()` 但**不回写**调用者的 palette |
| **标题文字重复** | Qt 把 `setApplicationDisplayName()` 追加到窗口标题 |
| **NameError 致鼠标移过即崩** | `RESIZE_MARGIN`/`TITLE_H` 写在 `__init__` 内成为局部名，`_edge_at` 引用时抛错 |
| **`SetWindowLongPtrW` 溢出** | 未声明 `argtypes`，ctypes 按 int 处理 → `OverflowError: int too long to convert`，子类化静默失败 |

### 5.8 其他

- **统一换行符为 LF**：28 文件 / 8289 处 CRLF。仓库 `.gitattributes` 早就规定 `* text=auto eol=lf`，但历史文件实际存 CRLF，是跨分支合并反复冲突的根因
- **合入 main 的 S1 安全回归测试 3 条**：`test_declared_size_returns_none_not_zero` / `test_extract_refuses_no_size_container` / `test_zero_size_archive_still_allowed` + 辅助方法 `_make_no_size_archive`。代码早就有 S1 修复，本次补齐测试保护

---

## 6. 未完成事项（按优先级）

> **P0 已完成**：原来第 6 节的第一项是「推送那 8 行」，已于 2026-11-13 完成推送（远端 main = `26ba861`）。该项内容即 `packaging/installer.iss.in` 的 `VersionInfoVersion` 修复，Setup.exe 的 `FileVersion` 已从空白变为 `1.2.0.0`。

### P1：Flatpak —— 本机无解，需改 BIOS

**结论：这台机器的 Flatpak 做不出来，是硬件层限制。**

排查链条（都实测过）：

1. Flatpak 构建依赖 `bwrap` 沙箱，沙箱需要 Linux **user namespaces**
2. 实测 `unshare --user` → `unshare: unshare failed: Invalid argument`
3. 查 WSL 版本：`wsl --list --verbose` → **Ubuntu VERSION = 1**（是 WSL 1，不是 WSL 2）
4. WSL 1 内核 `4.4.0-19041-Microsoft` 不支持 namespaces
5. 想升级 WSL 2：`wsl --set-version Ubuntu 2` → 失败，提示缺虚拟化
6. 查虚拟化状态：
   ```
   HypervisorPresent             = False
   VirtualizationFirmwareEnabled = False
   SecondLevelAddressTranslation = False
   ```
   **BIOS/固件没开启硬件虚拟化（VT-x/AMD-V）**

试过但无效的补救：
- `chmod u+s /usr/bin/bwrap`（设 setuid）→ 仍报同样错误
- 修 `/etc/machine-id`（原为空导致 dpkg 失败）→ 让 flatpak 1.16.6 装上了，但 bwrap 还是不行

**要做 Flatpak 需要用户在 BIOS 开启虚拟化**，然后：
```powershell
wsl --set-version Ubuntu 2
wsl -d Ubuntu -u root -- bash -c "apt-get install -y flatpak && flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo"
wsl -d Ubuntu -u root -- bash -c "flatpak install -y flathub org.freedesktop.Platform//24.08 org.freedesktop.Sdk//24.08"
# 然后 flatpak-builder 构建
```

manifest 本身已就绪且校验通过：
```powershell
& 'C:\Python315\python.exe' packaging\gen-flatpak-manifest.py --check
# → manifest 校验通过：io.github.backupstudio.BackupStudio.yml（2 个模块）
```

### P2：dmg —— 需 macOS 真机

`packaging/make-dmg.sh` 已存在，但无法交叉编译。v1.1.1 的两个 dmg 是用户手工补的。

### P3：GUI 缺「恢复」入口

`extract.py` 有完整的解压引擎（985 行），但 GUI 没有恢复页，只能走 CLI：
```
BackupStudio.exe extract <包> <目标>
```

### P4：其他已知问题

- macOS / Linux 未在真机验证无边框路径（按平台保留系统标题栏，所以理论上不受影响）
- 所有平台产物**未签名**（Windows 无代码签名证书，macOS 未公证）→ 用户会看到安全警告
- Aero Snap 无法用合成输入自动验证，**需真人手测**（拖标题栏到屏幕顶端/左右边缘）
- 液态玻璃在低端机器上有可感知开销（每张顶层卡片需模糊 + 折射）

---

## 7. 已发布产物与 Release 状态

### v1.2.0 Release 已发布（4 个资产）

https://github.com/nan-tang-xi-xi-owo/BackupStudio/releases/tag/v1.2.0

| 文件 | 大小 | SHA256 |
|---|---|---|
| `BackupStudio-Setup-1.2.0.exe` | 37.0 MB | `4EF2E6095547EDDC8CAF4C4CA8ED6A8F2C25B330F93EE2C1A193680B55F524C7` |
| `BackupStudio-1.2.0-windows-x64.zip` | 51.7 MB | `7335608B0D74C2D109250B9A7153851E7C2AC76832C27E2CDD150B91C35836EC` |
| `BackupStudio-1.2.0-x86_64.AppImage` | 70.6 MB | `5B821ADC33FD9A6063840219831C1757A9253749E3ABA032A98374AD7587A9CC` |
| `backupstudio_1.2.0_amd64.deb` | 104.7 MB | `644D63A835A3D9A19D888D7E1C8F72106E648A27230B1F2FAD9EA1B03396D643` |

四个都是 `state=uploaded`，已用不带 token 的公开下载验证过 `200 OK`。

### 本地 dist/ 有全部产物（587.7 MB，gitignored）

```
dist/BackupStudio/                              解包后的 Windows 版
dist/BackupStudio-1.2.0-windows-x64.zip
dist/BackupStudio-1.2.0-x86_64.AppImage
dist/BackupStudio-Setup-1.2.0.exe
dist/backupstudio_1.2.0_amd64.deb
```

> **注意**：`dist/BackupStudio/` 当前是 **Windows** 版（FileVersion 1.2.0）。如果要在 WSL 里构建 Linux 版，会覆盖它 —— 记得先备份或重新构建。

---

## 8. 打包方法（三平台）

### 8.1 Windows（PyInstaller + Inno Setup）

```powershell
cd "C:\APP\deepseek\work\定时备份及其压缩项目"
Get-Process BackupStudio -EA SilentlyContinue | Stop-Process -Force
Remove-Item dist -Recurse -Force -EA SilentlyContinue
Remove-Item build\version_info.txt -Force -EA SilentlyContinue   # 清旧缓存
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
& 'C:\Python315\python.exe' packaging\build.py        # 约 90-180 秒
```

**版本号有两个来源，必须同步**：
- `backupstudio/__init__.py` 的 `__version__`
- `pyproject.toml` 的 `version`

`packaging/build.py::_app_version()` 的读取优先级是：`import backupstudio` → `pyproject.toml` → `__init__.py`。**`pyproject.toml` 优先于 `__init__.py`**，这是本次踩过的坑（exe 显示 1.1.1、程序内显示 1.2.0）。

`build/version_info.txt` 是缓存文件，改版本号后要删掉重建。

#### Setup.exe

**Inno Setup 装在本机非标准路径**：`C:\APP\Inno Setup 7\ISCC.exe`

```powershell
& powershell -NoProfile -ExecutionPolicy Bypass `
  -File packaging\make-windows-installer.ps1 `
  -ISCC 'C:\APP\Inno Setup 7\ISCC.exe'
```
约 55-65 秒，产物 `dist\BackupStudio-Setup-<version>.exe`。

脚本默认只搜 `Inno Setup 6` 的路径，所以**必须显式传 `-ISCC`**。

### 8.2 Linux（WSL）

**WSL 环境现状**：
- Ubuntu 26.04.1 LTS / x86_64 / **WSL 版本 1**
- Python 3.14.4 + pip 25.1.1
- PySide6 6.12.0、PyInstaller 6.22.3 已装
- 有 `dpkg-deb`，无 `flatpak`/`appimagetool`/`fuse`（flatpak 后来装了但不可用，见 P1）

**以 root 运行**（不需要 sudo 密码）：
```powershell
wsl -d Ubuntu -u root -- bash -c "命令"
```

#### 构建 PyInstaller 产物

```bash
cd "/mnt/c/APP/deepseek/work/定时备份及其压缩项目"
rm -rf dist/BackupStudio build/pyinstaller
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python3 packaging/build.py
```

#### AppImage

**三个坑（都已绕过）**：

1. **中文文件名会让 appimagetool 崩**
   ```
   Option parsing failed: Invalid byte sequence in conversion input
   ```
   已修：`build.py` 按平台决定文件名（Linux 用 `QUICKSTART.txt`）。

2. **appimagetool 无法下载 runtime**（不跟随 GitHub 302）
   ```
   Failed to download runtime: server returned status code 302
   ```
   已修：`make-appimage.sh` 自己下载 runtime + `--runtime-file`。

3. **FUSE 不可用**（WSL 里没 fuse 设备）
   ```bash
   export APPIMAGE_EXTRACT_AND_RUN=1   # 必须
   ```

**可用的打包方式**（工具和 AppDir 都放纯 ASCII 路径）：
```bash
# 1) 工具复制到 /tmp/aitool
mkdir -p /tmp/aitool
cp build/appimage-tools/appimagetool /tmp/aitool/
cp build/appimage-tools/runtime-x86_64 /tmp/aitool/
chmod +x /tmp/aitool/appimagetool

# 2) AppDir 复制到 ASCII 路径
cp -a build/AppDir /tmp/bs-ai/AppDir

# 3) 打包
cd /tmp/bs-ai
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 /tmp/aitool/appimagetool \
  --no-appstream --runtime-file /tmp/aitool/runtime-x86_64 \
  /tmp/bs-ai/AppDir /tmp/bs-ai/BackupStudio-1.2.0-x86_64.AppImage
```

#### .deb

**WSL 的 wslfs 让 tar 失败**（这是最麻烦的坑）：

```bash
stat -f -c '%T' /tmp     # → wslfs
stat -c '%i' /tmp/xxx    # → 3377699721359975（56 位 inode）
```

`tar` 的 32 位 inode 字段溢出：
```
tar: ./usr/share/metainfo: Cannot stat: Function not implemented
dpkg-deb: error: tar -cf subprocess failed with exit status 2
```

连带 `dpkg-deb -x` 也解不开自己打的包。

**绕过方案：用 Python `tarfile` 手工构建 deb**（ar + control.tar.gz + data.tar.gz）。

另外 **PyInstaller 产物里的 `.so` 是符号链接**（`libpyside6`/`libshiboken6`/`libzmq`/`libsodium` 等，指向 `shiboken6/`、`PySide6/` 子目录），从 Windows 挂载点复制**必须用 `cp -rL`** 解引用，否则得到 35 字节的损坏链接。

### 8.3 跨平台注意

- `.gitattributes` 规定 `* text=auto eol=lf`，改文件时**保持 LF**
- 曾经因为写文件时把 CRLF 改成 LF，导致 `task_dialog.py` 的 diff 从 7 行变 2311 行
- PyInstaller spec 产物在 `build/pyinstaller/`，`--clean` 会清空该目录

---

## 9. 用户偏好与工作方式（重要）

以下是从 `C:\APP\deepseek\work\AGENTS.md` 和长期交互中总结的：

### 沟通

- **用中文汇报**
- **区分「确认事实」与「推测」**，给可核查证据（行号、sha、退出码、测试数字）
- 发现此前口径有误必须**明确订正**并说明原因
- 用户对成本敏感（说过"烧了我4块钱"），但也说过 token 不是约束 —— 优先保证质量，别为省 token 缩水验证

### 编排

- **优先用 `subagent` / `subagent_fork`，不要用 `workflow`**
  - 原因：`workflow` 结果只在全部 agent 跑完后一次性返回，一旦被中止，已跑完的 agent 结果全拿不到
- 并行发起时**在同一条 assistant 消息里**同时发出多个 `subagent` 调用
- 每次并发**不超过 10 个**，分批推进
- 子 Agent 报错**必须重跑该批**，不得缩水、不得把活推回主线程
- 派活必须写清磁盘纪律（临时目录、体积上限、结束即删）—— 历史上出过 50/90 GB 临时文件事故

### 质量

- 任何"已完成/已修复"的声明，**必须有不依赖该 Agent 自述的独立证据**
- 冲突结论要交叉裁决（派第三方或主线程亲自复核）
- 用户反馈的 UI 问题，**优先用真实截图 + 裁图放大定位**，不要凭猜测改

### 用户的验收习惯

- 会自己截图发来看问题，**给的是真实现象，比程序化测量可靠**
- 说"卡"、"丑"、"不协调"这类主观描述时，要**量化**（测耗时、量像素差）
- 会追问"在哪去了" —— 说明需求被漏做时直接承认，别绕

---

## 10. 环境速查

| 项目 | 路径/值 |
|---|---|
| Python | `C:\Python315\python.exe`（3.15） |
| 项目内 7-Zip | `tools\win32\7z\`（`7za.exe` + `7za.dll`） |
| Inno Setup | `C:\APP\Inno Setup 7\ISCC.exe` |
| WSL 发行版 | Ubuntu（**VERSION 1**），`wsl -d Ubuntu -u root` |
| GitHub 账号 | `nan-tang-xi-xi-owo` |
| GitHub token | ❌ **已丢失**，见第 1 节 |
| 代理/网速 | 访问 GitHub 很慢（appimagetool 14 MB 下了 20 分钟） |

### 常用命令

```powershell
# 跑测试
cd "C:\APP\deepseek\work\定时备份及其压缩项目"
$env:PYTHONUTF8='1'
$env:PATH="C:\APP\deepseek\work\定时备份及其压缩项目\tools\win32\7z;"+$env:PATH
New-Item -ItemType Directory -Force build\t | Out-Null
& 'C:\Python315\python.exe' -m pytest tests -q --basetemp='build\t'

# 启动源码版
& 'C:\Python315\python.exe' -m backupstudio

# 启动打包版
& 'dist\BackupStudio\BackupStudio.exe'

# 自检（8 种压缩格式探测）
& 'dist\BackupStudio\BackupStudio.exe' selftest

# 查看远端状态（不需要 token）
git ls-remote --heads origin

# 看本地与远端差异
git fetch origin main && git diff --stat origin/main HEAD
```

---

## 11. 接手后的建议顺序

1. **先读第 1 节**确认 token 状态；顺手把 `%TEMP%\gh-tok.txt` 挪到不易丢失的位置
2. `git clone` 或 `git fetch origin && git checkout -B work origin/main` 建立干净基线
   （**不要**管旧设备上的本地分支，以 `origin/main` 为准）
3. 跑一遍测试，确认 **483 passed / 0 failed**（记得加 7-Zip 到 PATH，见第 4 节）
4. 起一次源码版（`python -m backupstudio`）和打包版（`dist\BackupStudio\BackupStudio.exe`），
   肉眼确认六种风格都能切、标题栏跟随、玻璃有折射
5. 读第 5 节，理解这次 1.2.0 改了什么、为什么这么改（尤其别踩回已修过的坑）
6. 找用户确认剩余优先级：
   - Flatpak 要不要去 BIOS 开虚拟化？
   - dmg 有没有 Mac 可用？
   - GUI 恢复入口要不要做？

---

## 12. 遗留的临时产物

已在本轮清理：
- `build/` 下所有临时文件（version_info.txt 缓存、pyinstaller 工作区、测试 basetemp），现在 **0 项**
- `__pycache__`、`.pytest_cache`
- 根目录 `build_pack*.log`

**未清理（有意保留）**：
- `dist/`（587.7 MB，4 个产物 + 解包目录）—— 新设备上没有，需要重新构建
- `dev/` 66 个验证脚本（186.5 KB）—— 都是 git 追踪的，克隆时会带下来
- `docs/screenshots/`（实拍截图，文档引用）
- WSL 里 `/var/lib/flatpak`（约 2 GB，flatpak 用不了可删：
  `wsl -d Ubuntu -u root -- rm -rf /var/lib/flatpak`）

---

## 13. 交接总结（一句话版）

**代码和产物都完整可用，v1.2.0 已发布 4 个平台的包，本地与远端 main 内容完全一致（已逐字节核实）；剩下的是 Flatpak（需用户去 BIOS 开虚拟化）和 dmg（需 Mac）。**

---

*本文档由上一任 AI 在 2026-11-13 生成，同日更新（推送完成后的状态订正）。
 如有疑问，先查本文档，再查 `docs/KNOWN-ISSUES.md`，最后查 git log。*

---

*本文档由上一任 AI 在 2026-11-13 生成。如有疑问，先查本文档，再查 `docs/KNOWN-ISSUES.md`，最后查 git log。*
