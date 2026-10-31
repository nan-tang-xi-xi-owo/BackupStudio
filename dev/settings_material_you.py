"""设置页：加 Material You 源色/风格选择（1.1.3）。"""
from pathlib import Path

P = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目\backupstudio\gui\settings_page.py")
s = P.read_text(encoding="utf-8")

# 1) 在风格下拉之后插入 Material You 控制区
old = """        self.style_combo.setToolTip(
            "风格决定圆角、立体感、渐变与透明度；配色决定明暗。两者可自由组合。"
        )
        appearance.addWidget(self.style_combo)"""
new = """        self.style_combo.setToolTip(
            "风格决定圆角、立体感、渐变与透明度；配色决定明暗。两者可自由组合。"
        )
        appearance.addWidget(self.style_combo)

        # ── Material You 动态配色（1.1.3）──
        # 依据 AOSP 文档：单一源色 → 5 套色调板 × 13 档 = 65 色，
        # 再映射到颜色角色。换源色整套配色都变。
        appearance.addWidget(QLabel("Material You 源色"), 1)
        seed_row = QWidget()
        sl = QHBoxLayout(seed_row)
        sl.setContentsMargins(0, 0, 0, 0)
        self.seed_edit = QLineEdit()
        self.seed_edit.setPlaceholderText("#6750A4")
        self.seed_edit.setMaximumWidth(120)
        self.seed_edit.editingFinished.connect(self._on_seed_changed)
        sl.addWidget(self.seed_edit)
        self.btn_pick_seed = QPushButton("选色…")
        self.btn_pick_seed.clicked.connect(self._pick_seed)
        sl.addWidget(self.btn_pick_seed)
        sl.addStretch(1)
        appearance.addWidget(seed_row)

        appearance.addWidget(QLabel("Material You 配色风格"), 1)
        self.tonal_combo = Combo()
        for key, name in (
            ("TONAL_SPOT", "TONAL_SPOT（默认）"),
            ("VIBRANT", "VIBRANT（更鲜艳）"),
            ("EXPRESSIVE", "EXPRESSIVE（极鲜艳）"),
            ("SPRITZ", "SPRITZ（近灰阶）"),
        ):
            self.tonal_combo.addItem(name, key)
        self.tonal_combo.currentIndexChanged.connect(self._on_tonal_changed)
        appearance.addWidget(self.tonal_combo)"""
if old in s:
    s = s.replace(old, new, 1)
    print("1) 已插入 Material You 控制区")
else:
    print("1) 未匹配 appearance 区")

# 2) 处理方法
anchor = "    def _on_style(self) -> None:"
methods = '''    # ------------------------------------------------------------------
    # Material You（1.1.3）
    # ------------------------------------------------------------------
    def _on_seed_changed(self) -> None:
        from .mcu import normalize_seed

        seed = normalize_seed(self.seed_edit.text())
        self.seed_edit.setText(seed)
        s = self.state.store.settings
        s.material_seed = seed
        s.save()
        self.state.set_style(self.style_combo.currentData() or "win")

    def _pick_seed(self) -> None:
        from PySide6.QtWidgets import QColorDialog

        from .mcu import normalize_seed

        cur = normalize_seed(self.seed_edit.text())
        c = QColorDialog.getColor(QColor(cur), self, "选择 Material You 源色")
        if c.isValid():
            self.seed_edit.setText(c.name().upper())
            self._on_seed_changed()

    def _on_tonal_changed(self) -> None:
        if self._loading:
            return
        s = self.state.store.settings
        s.material_style = self.tonal_combo.currentData() or "TONAL_SPOT"
        s.save()
        self.state.set_style(self.style_combo.currentData() or "win")

'''
if "def _on_seed_changed" not in s:
    s = s.replace(anchor, methods + anchor, 1)
    print("2) 已加 Material You 处理方法")

P.write_text(s, encoding="utf-8", newline="")
import py_compile
py_compile.compile(str(P), doraise=True)
print("3) 语法 OK")
