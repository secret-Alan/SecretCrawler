"""可复用的复合控件。

本模块为 UI 配置面板提供三个自包含组件：
    KeyValueTable  - 可编辑的键/值对表格（请求头、Cookie 等）
    RulesTable     - 提取规则编辑器（字段名 / 类型 / 表达式 / 属性）
    FilePathEdit   - 路径选择器（按钮 + 单行输入框）

仅依赖 PySide6 与 crawler.models 中的纯数据模型，不引入引擎或调度器。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QFileDialog,
    QLineEdit,
    QComboBox,
    QHeaderView,
)

from crawler.models import ExtractionRule, SelectorType


class KeyValueTable(QWidget):
    """可编辑的键/值对表格，适用于请求头、Cookie 等场景。

    表格固定两列「键 / 值」，底部提供「添加 / 删除 / 清空」按钮。
    始终保留一行空行便于直接键入新条目。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # ---- 表格 ----
        self._table = QTableWidget(0, 2, self)
        # 设置最小高度，便于多行编辑（POST 数据/请求头/Cookie）
        self._table.setMinimumHeight(160)
        self._table.setHorizontalHeaderLabels(["键", "值"])
        # 水平表头拉伸：两列均分；最后一列填满剩余空间
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # 隐藏垂直表头（行号）以保持外观整洁
        self._table.verticalHeader().setVisible(False)
        # 选中整行
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)

        # ---- 按钮行 ----
        self._add_btn = QPushButton("添加", self)
        self._del_btn = QPushButton("删除", self)
        self._clear_btn = QPushButton("清空", self)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._add_btn)
        btn_row.addWidget(self._del_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._clear_btn)

        # ---- 整体布局：表格在上，按钮在下 ----
        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addLayout(btn_row)

        # ---- 信号绑定 ----
        self._add_btn.clicked.connect(self._on_add)
        self._del_btn.clicked.connect(self._on_delete)
        self._clear_btn.clicked.connect(self._on_clear)

        # 初始保证有一行空行
        self._ensure_trailing_empty_row()

    # ---------- 公共 API ----------

    def set_items(self, items: dict[str, str]) -> None:
        """用给定字典替换全部行。"""
        self._table.setRowCount(0)
        if items:
            for key, value in items.items():
                self._append_row(str(key), str(value))
        self._ensure_trailing_empty_row()

    def get_items(self) -> dict[str, str]:
        """收集所有键非空的行。

        - 键为空的行跳过。
        - 同名键后出现的覆盖先出现的。
        - 末尾的空行自然会被跳过。
        """
        result: dict[str, str] = {}
        for row in range(self._table.rowCount()):
            key_item = self._table.item(row, 0)
            value_item = self._table.item(row, 1)
            key = key_item.text() if key_item is not None else ""
            value = value_item.text() if value_item is not None else ""
            key = key.strip()
            if not key:
                continue
            result[key] = value
        return result

    # ---------- 内部辅助 ----------

    def _append_row(self, key: str = "", value: str = "") -> None:
        """在末尾追加一行并填入键值。"""
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(key))
        self._table.setItem(row, 1, QTableWidgetItem(value))

    def _ensure_trailing_empty_row(self) -> None:
        """确保末尾始终存在一行空白行便于输入。"""
        row_count = self._table.rowCount()
        if row_count == 0:
            self._append_row("", "")
            return
        last_key_item = self._table.item(row_count - 1, 0)
        last_value_item = self._table.item(row_count - 1, 1)
        last_key = last_key_item.text() if last_key_item is not None else ""
        last_value = last_value_item.text() if last_value_item is not None else ""
        if last_key or last_value:
            self._append_row("", "")

    def _on_add(self) -> None:
        """添加按钮：选中行下方插入空行；未选中则追加到末尾。"""
        current = self._table.currentRow()
        if current < 0:
            self._append_row("", "")
        else:
            self._table.insertRow(current + 1)
            self._table.setItem(current + 1, 0, QTableWidgetItem(""))
            self._table.setItem(current + 1, 1, QTableWidgetItem(""))
            self._table.setCurrentCell(current + 1, 0)
        self._ensure_trailing_empty_row()

    def _on_delete(self) -> None:
        """删除按钮：移除当前选中行。"""
        current = self._table.currentRow()
        if current >= 0:
            self._table.removeRow(current)
        self._ensure_trailing_empty_row()

    def _on_clear(self) -> None:
        """清空按钮：清空所有行，仅保留一行空行。"""
        self._table.setRowCount(0)
        self._ensure_trailing_empty_row()


class RulesTable(QWidget):
    """提取规则编辑器表格。

    四列：「字段名 / 类型 / 表达式 / 属性」。
    「类型」列使用 QComboBox 单元格控件，提供 SelectorType 的可选项。
    始终保留一行空行便于直接键入新规则。
    """

    # 类型下拉的可选项（显示文本与 SelectorType 值一一对应）
    _SELECTOR_OPTIONS = [
        SelectorType.CSS,
        SelectorType.XPATH,
        SelectorType.REGEX,
        SelectorType.JSON,
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # ---- 表格 ----
        self._table = QTableWidget(0, 4, self)
        self._table.setMinimumHeight(160)
        self._table.setHorizontalHeaderLabels(["字段名", "类型", "表达式", "属性"])
        header = self._table.horizontalHeader()
        # 字段名、表达式、属性列均拉伸；类型列按内容宽度
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)

        # ---- 按钮行 ----
        self._add_btn = QPushButton("添加规则", self)
        self._del_btn = QPushButton("删除规则", self)
        self._clear_btn = QPushButton("清空", self)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._add_btn)
        btn_row.addWidget(self._del_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._clear_btn)

        # ---- 布局 ----
        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addLayout(btn_row)

        # ---- 信号 ----
        self._add_btn.clicked.connect(self._on_add)
        self._del_btn.clicked.connect(self._on_delete)
        self._clear_btn.clicked.connect(self._on_clear)

        # 初始空行
        self._ensure_trailing_empty_row()

    # ---------- 公共 API ----------

    def set_rules(self, rules: list[ExtractionRule]) -> None:
        """用给定规则列表替换全部行。"""
        self._table.setRowCount(0)
        for rule in rules:
            self._append_rule_row(rule)
        self._ensure_trailing_empty_row()

    def get_rules(self) -> list[ExtractionRule]:
        """收集所有「字段名与表达式」均非空的规则行。

        空行（字段名或表达式为空）会被跳过；末尾空行自然忽略。
        """
        result: list[ExtractionRule] = []
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, 0)
            expr_item = self._table.item(row, 2)
            attr_item = self._table.item(row, 3)
            name = name_item.text().strip() if name_item is not None else ""
            expression = expr_item.text() if expr_item is not None else ""
            attribute = attr_item.text() if attr_item is not None else ""
            if not name or not expression.strip():
                continue
            selector_type = self._selector_type_at(row)
            result.append(
                ExtractionRule(
                    name=name,
                    selector_type=selector_type,
                    expression=expression,
                    attribute=attribute,
                )
            )
        return result

    # ---------- 内部辅助 ----------

    def _append_rule_row(self, rule: ExtractionRule | None = None) -> None:
        """在末尾追加一行规则。

        rule 为 None 时表示新增空行（类型默认 CSS）。
        """
        row = self._table.rowCount()
        self._table.insertRow(row)

        if rule is None:
            self._table.setItem(row, 0, QTableWidgetItem(""))
            self._table.setItem(row, 2, QTableWidgetItem(""))
            self._table.setItem(row, 3, QTableWidgetItem(""))
        else:
            self._table.setItem(row, 0, QTableWidgetItem(rule.name))
            self._table.setItem(row, 2, QTableWidgetItem(rule.expression))
            self._table.setItem(row, 3, QTableWidgetItem(rule.attribute))

        # 为「类型」列设置下拉控件
        combo = QComboBox(self._table)
        for option in self._SELECTOR_OPTIONS:
            combo.addItem(option.value, option)
        # 选中给定类型（默认 CSS）
        desired = rule.selector_type if rule is not None else SelectorType.CSS
        idx = combo.findData(desired)
        if idx < 0:
            idx = 0
        combo.setCurrentIndex(idx)
        self._table.setCellWidget(row, 1, combo)

    def _ensure_trailing_empty_row(self) -> None:
        """确保末尾存在一行空行。"""
        row_count = self._table.rowCount()
        if row_count == 0:
            self._append_rule_row(None)
            return
        name_item = self._table.item(row_count - 1, 0)
        expr_item = self._table.item(row_count - 1, 2)
        last_name = name_item.text() if name_item is not None else ""
        last_expr = expr_item.text() if expr_item is not None else ""
        if last_name.strip() or last_expr.strip():
            self._append_rule_row(None)

    def _selector_type_at(self, row: int) -> SelectorType:
        """读取指定行的 SelectorType。若控件缺失则回退为 CSS。"""
        widget = self._table.cellWidget(row, 1)
        if isinstance(widget, QComboBox):
            data = widget.currentData()
            if isinstance(data, SelectorType):
                return data
            # 兜底：用文本反查
            try:
                return SelectorType(widget.currentText())
            except ValueError:
                return SelectorType.CSS
        return SelectorType.CSS

    def _on_add(self) -> None:
        """添加规则：选中行下方插入空行；未选中则追加到末尾。"""
        current = self._table.currentRow()
        if current < 0:
            self._append_rule_row(None)
        else:
            # 在 current+1 处插入：先插入空壳行再补齐控件与空 item
            self._table.insertRow(current + 1)
            self._table.setItem(current + 1, 0, QTableWidgetItem(""))
            self._table.setItem(current + 1, 2, QTableWidgetItem(""))
            self._table.setItem(current + 1, 3, QTableWidgetItem(""))
            combo = QComboBox(self._table)
            for option in self._SELECTOR_OPTIONS:
                combo.addItem(option.value, option)
            combo.setCurrentIndex(0)
            self._table.setCellWidget(current + 1, 1, combo)
            self._table.setCurrentCell(current + 1, 0)
        self._ensure_trailing_empty_row()

    def _on_delete(self) -> None:
        """删除当前选中规则行。"""
        current = self._table.currentRow()
        if current >= 0:
            # 移除前先清理 cellWidget，避免悬空引用
            self._table.removeCellWidget(current, 1)
            self._table.removeRow(current)
        self._ensure_trailing_empty_row()

    def _on_clear(self) -> None:
        """清空所有规则，仅保留一行空行。"""
        # 清理所有 cellWidget
        for row in range(self._table.rowCount()):
            self._table.removeCellWidget(row, 1)
        self._table.setRowCount(0)
        self._ensure_trailing_empty_row()


class FilePathEdit(QWidget):
    """路径选择控件：单行输入框 + 「浏览…」按钮。

    is_dir=True 选择目录，否则选择文件（可指定 file_filter）。
    """

    def __init__(
        self,
        path: str = "",
        is_dir: bool = False,
        file_filter: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._is_dir = is_dir
        self._file_filter = file_filter

        self._line_edit = QLineEdit(self)
        self._line_edit.setText(path)

        self._browse_btn = QPushButton("浏览…", self)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._line_edit)
        layout.addWidget(self._browse_btn)

        self._browse_btn.clicked.connect(self._on_browse)

    # ---------- 公共 API ----------

    def get_path(self) -> str:
        """返回当前输入框中的路径文本。"""
        return self._line_edit.text()

    def set_path(self, path: str) -> None:
        """设置输入框中的路径文本。"""
        self._line_edit.setText(path)

    # ---------- 内部辅助 ----------

    def _on_browse(self) -> None:
        """点击浏览按钮：弹出对应的文件/目录选择对话框。"""
        current = self._line_edit.text().strip()
        if self._is_dir:
            selected = QFileDialog.getExistingDirectory(
                self, "选择目录", current
            )
        else:
            selected, _ = QFileDialog.getOpenFileName(
                self, "选择文件", current, self._file_filter
            )
        if selected:
            self._line_edit.setText(selected)
