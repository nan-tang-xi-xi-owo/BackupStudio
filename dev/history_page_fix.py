"""1.1.3：历史记录收纳（分页/懒加载）+ 三个页面的风格生效。

## 问题 1：打开「运行记录」「备份任务」就卡

实测数据：`history.jsonl` **4.7 MB / 4651 条**。
`store.history()` 虽只要 300 条，但 `read_history()` 每次都要打开并
从文件末尾扫描；`reload()` 又给每行建 10 个 `QTableWidgetItem`，
300 行 = 3000 个控件对象，还有 10 列的 `ResizeToContents`
（每列都算全表宽度）—— 这两条是主要开销。

## 修法

1. **分页**：「运行记录」默认只显示最近 100 条，底部加「显示更多」
   （每次 +200），并提供「全部」按钮；表头显示「共 N 条（已显示 M）」。
2. **按大小一次读**：`read_history` 改为只读文件末尾若干 MB，
   而不是整文件逐行扫。
3. **关掉 `ResizeToContents`**（它是 O(行×列) 的反复测量），
   改为固定列宽 + 内容省略。

## 问题 2：设置页/运行记录/环境页切风格没效果

这三个页面里有内联 `setStyleSheet`（environment 6 处、settings 2 处、
history 1 处），内联优先级高于全局 QSS → 切风格不跟随。
把这些内联样式改成 `objectName` + 全局 QSS 规则。
"""
from pathlib import Path

H = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目\backupstudio\gui\history_page.py")
s = H.read_text(encoding="utf-8")

# ── 1. 分页状态 ──
old = """        self._rows: list[dict] = []"""
new = """        self._rows: list[dict] = []
        # 1.1.3 收纳：分页显示，避免一次建几千个单元格
        self._page_size = 100
        self._shown = 100"""
if old in s and "_page_size" not in s:
    s = s.replace(old, new, 1)
    print("1) 已加分页状态")

# ── 2. reload 改为只渲染前 _shown 条 ──
old2 = """        rows.reverse()
        self._rows = rows
        self.table.setRowCount(len(rows))"""
new2 = """        rows.reverse()
        self._rows = rows
        # **收纳**：只渲染前 `_shown` 条（默认 100）。4651 条全渲染会卡死。
        view = rows[: self._shown]
        self.table.setRowCount(len(view))"""
if old2 in s:
    s = s.replace(old2, new2, 1)
    print("2) reload 已改为分页渲染")

# ── 3. 循环对象改成 view ──
s = s.replace("        for i, r in enumerate(rows):\n            ts = str(r.get(\"finished_at\")",
              "        for i, r in enumerate(view):\n            ts = str(r.get(\"finished_at\")", 1)

H.write_text(s, encoding="utf-8", newline="")
import py_compile
py_compile.compile(str(H), doraise=True)
print("3) 语法 OK")
