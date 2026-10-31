"""修设置页外观区布局（1.1.5）。

## 截图暴露的问题

用户截图显示设置页「常规」卡片里，五组控件被挤在一行并被右侧截断：

    外观主题 [下拉] 外观风格 [下拉] Material You 源色 [框][选色…] ...

原因：`appearance = QHBoxLayout()` —— 一个**水平**布局里塞了 5 组
（label + 控件）。水平布局不换行，总宽度超出卡片就被裁掉。

## 修法

改成「每行一个设置项」的表单布局：
* 外层 `QVBoxLayout`
* 每行用 `QHBoxLayout`：左侧固定宽度 label、右侧控件拉伸
* 长文本 label 允许换行
"""
from pathlib import Path

P = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目\backupstudio\gui\settings_page.py")
s = P.read_text(encoding="utf-8")

old_start = '        appearance = QHBoxLayout()'
i = s.index(old_start)
j = s.index('        general.add_layout(appearance)', i)
new_block = '''        # **每行一个设置项**（1.1.5）。原来把 5 组控件塞进一个 QHBoxLayout，
        # 水平布局不换行 → 总宽度超卡片就被裁掉（用户截图可见"截断"）。
        appearance = QVBoxLayout()
        appearance.setSpacing(8)

        def _row(label_text: str, widget) -> None:  # noqa: ANN001
            row = QHBoxLayout()
            row.setSpacing(10)
            lab = QLabel(label_text)
            lab.setWordWrap(True)
            lab.setMinimumWidth(150)
            row.addWidget(lab, 0)
            row.addWidget(widget, 1)
            appearance.addLayout(row)

        _row("外观主题", self.theme_combo)
        _row("外观风格", self.style_combo)
        _row("Material You 源色", seed_row)
        _row("Material You 配色风格", self.tonal_combo)

'''
s = s[:i] + new_block + s[j:]

# 源色行内部也要收紧（原来 maxWidth=120 + stretch 会留大空白）
s = s.replace(
    '        self.seed_edit.setMaximumWidth(120)',
    '        self.seed_edit.setFixedWidth(110)',
    1,
)
# seed_row 的布局改成紧凑
s = s.replace(
    '''        seed_row = QWidget()
        sl = QHBoxLayout(seed_row)
        sl.setContentsMargins(0, 0, 0, 0)''',
    '''        seed_row = QWidget()
        sl = QHBoxLayout(seed_row)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(6)''',
    1,
)

P.write_text(s, encoding="utf-8", newline="")
import py_compile
py_compile.compile(str(P), doraise=True)
print("✓ 设置页外观区已改为逐行表单布局")
