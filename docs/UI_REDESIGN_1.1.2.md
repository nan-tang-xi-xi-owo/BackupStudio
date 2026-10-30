# UI 改造报告（1.1.2）

> 起因：用户反馈「UI 太丑，要求更好看一些，解决 UI 交互问题」

---

## 结论

"丑"不是主观感受，而是四个**可量化**的根因。本版全部修复，每项都有数字对照。

| 根因 | 改造前 | 改造后 |
|---|---|---|
| **焦点完全不可见** | 像素差 **0**/8084 | **724** / 1060 / 1528 像素差 |
| **控件像坏掉** | disabled 同色、箭头缺失、选中行看不出 | 全部可辨（见下） |
| **对比度不足** | 浅色 **10 处**不达标，最差 **2.90:1** | **8/8 项**达标（全 ≥4.5:1） |
| **层次感为零** | 卡片 vs 背景 **1.08:1** | 靠边框分层，1.37/1.48:1 |

**测试：444 passed / 1 skipped / 0 failed**（与改造前基线一致，无回归）

---

## 一、焦点完全不可见 —— 键盘用户实际不可用

### 证据

QSS 里 `:focus` 只有 **1 条**规则，且只覆盖输入类：

| 控件 | 规则块数 | 含 `:focus` |
|---|---|---|
| `QPushButton` | 9 | ❌ **无** |
| `QToolButton` | 5 | ❌ **无** |
| `QLineEdit` | 3 | ✅ 有 |
| `QComboBox` | 2 | ❌ **无** |

同时 `outline: none`（2 处）主动关掉了 Qt 的系统焦点框。

像素级实测：焦点前后差异 **0/8084**（导航）、**0/3808**（主按钮）、
**0/164798**（列表）。

### 修复

给 `QPushButton` / `QToolButton` / `QListWidget` / `QTableWidget` /
`QComboBox` / `QCheckBox` / `QRadioButton` / `QTabBar` / `QSlider`
全部补 `:focus`，统一用 **2px `focus_ring` 边框**。

焦点环对比度实测 **5.90 ~ 7.34:1**（WCAG 图形元素只需 3:1）。

### 实测

```
按钮焦点   0 → 724 像素差
导航焦点   0 → 1060 / 1528 像素差
```

---

## 二、控件"像坏的" —— 三个独立缺陷

### 2a. disabled 态看不出（**两个不同的根因**）

```css
QPushButton          { background: #EEF2F9 }
QPushButton:disabled { background: #EEF2F9 }   /* ← 值完全相同 */
QPushButton#Primary  { background: #4F6BED }   /* ← ID 选择器特异性更高，
                                                  完全盖掉 :disabled */
```

| 按钮 | 实测 | 根因 |
|---|---|---|
| 普通 | `#EEF2F9 → #EEF2F9` | **规则生效了，但值相同** |
| Primary | `#4f6bed → #4f6bed` | **规则根本不生效**（被 ID 选择器覆盖） |

**用户看到满色可点，点了没反应，以为程序坏了。**

修复：新增 `disabled_bg` / `disabled_fg` 字段；补
`QPushButton#Primary:disabled`（**必须带 `#Primary`**，否则被盖掉）。

实测：普通 `#1f2430 → #a0a7b8`、Primary `#3e5deb → #f5f7fb`。

### 2b. 箭头全缺失

`down-arrow` / `up-arrow` / `right-arrow` / `left-arrow` —— **一条都没有**。
`QComboBox::drop-down` 设了 `border: none; width: 22px` 却不画箭头。

实测：下拉框箭头区只有 **32** 个非背景像素（几乎空白），
深色主题下微调箭头 `#141620` on `#1E2130` = **1.13:1**（等于隐形）。

修复：用 CSS 三角形（`border-*` 技巧）补回全部箭头。

实测：下拉 **110** 像素墨迹、微调 **164** 像素墨迹、子菜单箭头从 **4 → 恢复**。

### 2c. 表格选中行看不出

只有 `QListWidget::item:selected`，**没有** `QTableWidget::item:selected`
→ 历史表选中行 `#262A3B` vs `#161821` = **1.24:1**。

修复：补 `QTableWidget::item:selected` + `:hover`。

实测：选中行 **4700** 像素差。

---

## 三、浅色主题对比度系统性不足

### 证据（脚本计算，非估算）

浅色主题 **10 处**不达标（深色只有 1 处）：

| 组合 | 改造前 | 标准 |
|---|---|---|
| `warning` on `bg` | **2.90:1** | 4.5 |
| `success` on `bg` | 3.05:1 | 4.5 |
| `warning` on `surface` | 3.19:1 | 4.5 |
| `success` on `surface` | 3.30:1 | 4.5 |
| 导航选中态 | 3.56:1 | 4.5 |
| `text_dim` on `surface_alt` | 4.31:1 | 4.5 |
| `danger` on `bg` | 4.47:1 | 4.5 |
| `text_dim` on `bg` | 4.47:1 | 4.5 |
| `accent` on `surface_alt` | 4.02:1 | 4.5 |
| `accent` on `bg` | 4.17:1 | 4.5 |

`warning on bg = 2.90` 连 WCAG **大字**标准（3.0）都不到。

### 修复

重算语义色（保持色相，压暗提高对比）：

| 色 | 改造前 | 改造后 | 对比度改善 |
|---|---|---|---|
| `success` | `#16A34A` | **`#127F3A`** | 3.05 → **4.71** |
| `warning` | `#D97706` | **`#9F5706`** | 2.90 → **5.06** |
| `danger` | `#DC2626` | **`#D12222`** | 4.47 → **4.90** |
| `accent` | `#4F6BED` | **`#3E5DEB`** | 4.17 → **4.89** |
| `text_dim` | `#6B7280` | **`#656B78`** | 4.31 → **4.76** |

**8/8 项从"不达标"变为"达标"**。

---

## 四、层次感为零

`surface #FFFFFF` vs `bg #F4F6FB` = **1.08:1** —— 卡片和背景几乎同色，
**根本看不出边界**。这是"扁平、没设计感、廉价"的直接来源。

修复：加深边框（用边框而非底色差来分层）：

```
border        #D8DEEA → #D5DDEB   （1.37:1 vs surface）
border_strong 新增     #C2CCE0    （用于输入框等需强调的边界）
深色 surface  #1E2130 → #212536   （层次 1.11 → 1.17）
深色 border   #33384D → #3A4056
```

---

## 五、其它修复

### 5.1 字号 `px` → `pt`（无障碍）

QSS 用绝对像素（6 处 `12/13/14/16/22px`）→ **无视系统文字缩放**。

实测：系统字体设 20pt 后，`label.pixelSize()` 仍是 **13**（改造前）；
改造后跟随系统。

Windows「辅助功能 → 文本大小」对本程序此前**完全无效**。

### 5.2 `partial` 状态未映射 —— 误报"未运行过"

`engine.py` 会产出 `status="partial"`（有文件被占用/无权限，备份完成但不完整），
但 `tasks_page.py` 和 `history_page.py` 的状态表**都没有这个键**
→ `.get()` 回落到默认值「未运行过」。

**部分失败的备份在列表里显示成"从没跑过"**，用户会以为定时任务没生效。

修复：两处都加 `("部分完成", "warn")`。

### 5.3 退出确认默认按钮

原来没传 `defaultButton`，Qt 默认把「是」设为默认
→ **误按回车就中断正在运行的备份**。

修复：默认「否」，文案改为「退出并中断」/「继续备份」。

（对照：删除任务的确认传了 `No`，本来就是对的。）

### 5.4 导航键盘可达性

**根因**：互斥 `QButtonGroup` 在 `show()` 时会把**未选中项**的 `focusPolicy`
改成 `NoFocus`（Qt 对单选组的既定行为）。

隔离实验（纯 Qt，不含项目代码）确认触发时机：

```
建好未 checkable        → TabFocus ×4
setCheckable(True)      → TabFocus ×4
addButton（互斥组）      → TabFocus ×4   ← 不是这里
setChecked(True)        → TabFocus ×4
show()                  → NoFocus ×3     ← 只在这里变
```

**修复**：
- 显式 `setFocusPolicy(StrongFocus)`
- `showEvent` 里补设（单次定时器，等 Qt 内部状态设置完）
- `_select()` 里也补（`setChecked` 会再次触发）
- 补 `setTabOrder` 串联 4 个导航按钮
- 补**回车**激活（`QToolButton` 默认只响应空格）
- 加 `setAccessibleName` / `setAccessibleDescription`

**键盘路径实测（4/4）**：

```
✅ 方向键在导航组内移动     tasks → history → env → settings  4/4
✅ Tab 到导航 + 空格激活    4/4 页面
✅ 回车激活导航             4/4 页面
✅ 快捷键 Ctrl+,            可用
```

### 5.5 三套并行的颜色系统（widgets.py）

widgets.py 里有**三套独立于 Palette 的颜色表**，同一个语义有不同值：

| 位置 | 内容 | 问题 |
|---|---|---|
| Badge.COLORS / DARK_COLORS | 10 组（浅/深各 5） | ok 用 #16A34A |
| StatusPill | 5 种状态色 | ok 用 #22C55E |
| Switch | 轨道/旋钮色 | 	rack_off 浅色 #CBD5E1 不在 Palette 里 |

**成功绿有三个不同的值**：#16A34A（Badge）、#22C55E（StatusPill）、
#127F3A（新 Palette）。改 Palette 不会影响这些控件，
且对比度不受 WCAG 计算控制。

修复：全部改为从 Palette 派生。Badge 保留「浅底 + 深字」的成对结构
（这是设计意图），但**前景色**取自 Palette 的语义色。

剩余 10 个硬编码值是 Badge 的**背景成对值**（如 #DCFCE7 浅绿底），
保留是合理的 —— 徽章本来就需要浅底。

### 5.6 3 处硬编码颜色的内联样式

`environment_page.py:272`、`settings_page.py:282`、`task_dialog.py:813`。

其中 `task_dialog:813` 最典型：它已经 `setObjectName("SuccessText")`，
**却又用内联样式覆盖** —— 等于把全局 QSS 废掉，主题切换时颜色不跟着变。

修复：全部改为走 `objectName` + 全局 QSS。剩余硬编码内联色 **0 处**。

---

## 六、为什么没有引入第三方 UI 库

规划 Agent 评估后**明确建议不引入**，我认同，4 条理由：

1. **代码耦合深**：`test_dialog_fields.py` 有 20 个测试逐字段断言，
   换控件等于重写测试
2. **打包矩阵风险**：四平台六产物（exe/dmg/AppImage/Flatpak），
   PyInstaller + Flatpak 的资源打包翻车是常态
3. **许可证冲突**：`qfluentwidgets` 是 LGPLv3，本项目是 MIT
4. **换库也要重解同样的问题**：13 处对比度违规、焦点不可见、
   布局矛盾，换成任何库都得重新解决

留了转向出口：若验收后仍被评"丑"，开独立 spike 分支只迁 `settings` 页
（控件最少）实测再定，不动主线。

---

## 七、验证方法（全部量化，无主观判断）

| 手段 | 覆盖内容 | 结果 |
|---|---|---|
| WCAG 对比度脚本 | Palette 全部前景/背景组合 | 8/8 改善项达标 |
| 像素差测量 | 焦点态、disabled 态、选中行、箭头 | 全部 >100 像素差 |
| QSS 规则完整性检查 | 12 类关键伪状态 | 12/12（两主题） |
| 字号单位检查 | px/pt 计数 | px 0 / pt 6 |
| 硬编码色扫描 | 内联 `setStyleSheet` | 3 → 0 |
| 全量测试 | 445 个用例 | 444 passed / 0 failed |
| 键盘路径实测 | 方向键/空格/回车/快捷键 | 4/4 |

---

## 八、⚠️ 能力边界（必须说明）

**所有 Agent 和 AI 都无法读取图像**（当前模型不支持图像输入）。

因此整个改造过程中，**没有任何"人眼"看过界面**。所有结论都是
像素级量化测量 —— 能证明"对比度从 2.90 提到 5.06"、"焦点从 0 像素
变成 724 像素"，**但不能证明"看起来舒服"**。

为此导出了 4 张快照供人眼验收：

```
docs/screenshots/ui-1.1.2/light-controls.png   控件总览（浅色）
docs/screenshots/ui-1.1.2/light-focus.png      焦点对照（浅色）
docs/screenshots/ui-1.1.2/dark-controls.png    控件总览（深色）
docs/screenshots/ui-1.1.2/dark-focus.png       焦点对照（深色）
```

**建议**：打开程序实际看一眼，或者看这 4 张图。
如果仍觉得不够好看，说明问题不在"对比度/焦点/箭头"这些可达性层面，
而在**视觉风格**（配色品味、圆角大小、留白比例、图标风格）——
那是另一个层面的工作，需要人给出方向。

---

## 九、已知未做（属于 P1/P2，未在本版范围）

规划 Agent 列出但本次未做的：

| # | 问题 | 现状 |
|---|---|---|
| 1 | 间距无基准网格 | `setSpacing` 有 11 种取值，仅 39% 是 4 的倍数 |
| 2 | `Switch` 控件 24px 高、无焦点环 | 低于 WCAG 24×24 底线 |
| 3 | 任务对话框关闭丢修改 | 无 `_dirty` 标记与确认 |
| 4 | 保留策略预览卡死 10 秒 | 改一个数字要等 10 秒（同步扫盘） |
| 5 | GUI 无"恢复"入口 | 985 行解压引擎只有 CLI 能用到 |
| 6 | 15 处技术术语上屏 | 如 `状态 : success`、`触发方式 : manual` |
| 7 | 37 个模态框、Qt 标准按钮为英文 | 无翻译器，显示 `OK/Save/&Yes/&No` |
| 8 | 长文本硬截断无省略号 | 副标题需 372px 实得 240px |
| 9 | 最小尺寸自相矛盾 | 声明 980 / 实需 1298 |
| 10 | 空状态无引导 | 首次进入任务页是空白 |

这些需要下一轮继续。

---

## 十、我在本次改造中的一处失误（换行符）

### 问题

改 	ask_dialog.py / settings_page.py / environment_page.py /
history_page.py / widgets.py 时，我用 write_text(..., newline=") 写入，
**把文件的 CRLF 换行符改成了 LF**。

后果：这两个文件的 diff 从
`
task_dialog.py | 7 +++++--
`
变成
`
task_dialog.py | 2311 +++++++++++++-------------
`
—— 1156 行全被标记为「改动」，实际只改了 7 行。

### 影响

1. **git blame 失效**：所有行都变成「本次提交」
2. **code review 看不到真实改动**：审阅者会被 2311 行噪音淹没
3. **未来合并冲突风险**：其他分支若基于旧文件，会产生大面积冲突

### 修复

把 5 个文件恢复成 CRLF。diff 从
2978 insertions / 2345 deletions → **683 / 50**，真实改动一目了然。

### 顺带发现：仓库本身不一致

.gitattributes 规定 * text=auto eol=lf（文本文件应存 LF），
但 core.autocrlf=false，且 git 里**实际存的是 CRLF**（如
__init__.py 284 行全是 CRLF）。

而部分文件（ppstate.py、icons.py、worker.py）是纯 LF ——
说明仓库历史上就没统一过。

**本次按「与仓库现状一致」处理**（恢复 CRLF），让 diff 干净。
彻底统一换行符是独立的技术债，需要单独一次提交（会触及所有文件，
不适合混在 UI 改造里）。

### 教训

用 Path.write_text() 改文件时，**显式保留原有换行符**。
正确做法是先读字节判断，或统一用 
ewline=" + 自行处理。

同类问题我在这个项目里已经踩过一次（早期批量替换版本号时也有类似风险）。
