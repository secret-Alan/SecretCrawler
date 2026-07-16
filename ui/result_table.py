"""结果表格视图。

本模块提供专业爬虫项目的「结果表格视图」控件，满足 spec 中
「结果表格视图」需求：列由提取规则的字段名动态生成，支持实时分批
追加行、双击单元格复制、右键菜单（复制行 / 查看原始响应 / 在浏览器
打开来源 URL / 删除该行）、导出当前表格为 CSV。

爬虫引擎的 `engine.result_found` 信号可以直接连接到本控件的
`add_result` 槽；当结果来自工作线程时，Qt 的信号槽机制（QueuedConnection）
会自动将调用调度回 GUI 线程，无需额外加锁。

类定义:
    ResultTable - 结果表格视图控件（QWidget）
"""

from __future__ import annotations

import csv
import webbrowser
from collections import OrderedDict

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# 第一列固定为来源 URL，列名在此定义
_URL_COLUMN_HEADER = "来源URL"

# 内存中保留的原始响应条目数上限（按 URL 去重存储；超过时丢弃最旧）
_MAX_RAW_RESPONSES = 200

# 表格中保留的行数上限，避免长时间运行导致内存膨胀；超过时丢弃最旧行
_MAX_ROWS = 100_000

# 批量刷新间隔（毫秒）：到达后把累积的待写入结果一次性写入表格
_FLUSH_INTERVAL_MS = 250


class ResultTable(QWidget):
    """结果表格视图控件。

    列由 ResultItem.fields 的键动态生成，第一列固定为「来源URL」
    （对应 item.url）。新结果到达时若出现未知字段，会以追加列的方式
    扩展表格（不重排已有列）。结果先累积到内存待写列表，由定时器
    批量刷新到表格，避免每条结果都触发一次重绘导致的卡顿。
    """

    # 状态提示信号：主窗口可连接以在状态栏/工具提示中显示
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # 当前列顺序：首列恒为 来源URL，其余为各字段名（按出现顺序）
        self._columns: list[str] = [_URL_COLUMN_HEADER]
        # 待批量写入表格的结果列表
        self._pending: list = []
        # 按 URL 存储的原始响应文本（OrderedDict，超出上限淘汰最旧）
        self._raw_responses: "OrderedDict[str, str]" = OrderedDict()

        # ---- 顶部小工具栏 ----
        self._export_btn = QPushButton("导出 CSV", self)
        self._clear_btn = QPushButton("清空", self)
        toolbar = QHBoxLayout()
        toolbar.addStretch(1)
        toolbar.addWidget(self._export_btn)
        toolbar.addWidget(self._clear_btn)

        # ---- 结果表格 ----
        self.table = QTableWidget(0, 1, self)
        self.table.setHorizontalHeaderLabels([_URL_COLUMN_HEADER])
        header = self.table.horizontalHeader()
        # 第一列按内容宽度，其余列拉伸填充
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        # 隐藏垂直表头（行号）以保持外观整洁
        self.table.verticalHeader().setVisible(False)
        # 选中整行
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        # 允许点击表头排序（可选，便于用户浏览）
        self.table.setSortingEnabled(True)
        # 右键自定义菜单
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        # ---- 整体布局：工具栏在上，表格在下 ----
        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(self.table)

        # ---- 批量刷新定时器 ----
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_INTERVAL_MS)
        self._flush_timer.setSingleShot(False)
        self._flush_timer.timeout.connect(self._flush_now)
        # 启动后定时器常驻；空闲时（_pending 为空）槽函数会主动 stop()
        self._flush_timer.start()

        # ---- 信号绑定 ----
        self._export_btn.clicked.connect(self._on_export_clicked)
        self._clear_btn.clicked.connect(self.clear)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

    # ---------- 公共 API ----------

    @Slot(object)
    def add_result(self, item) -> None:
        """追加一条结果。

        实际并不立即写入表格，而是先放入待写列表，由定时器批量刷新。
        同时缓存 item.raw_response 以供「查看原始响应」使用（按 URL
        去重，超出 _MAX_RAW_RESPONSES 时淘汰最旧）。

        参数:
            item: crawler.models.ResultItem，包含 url / fields / raw_response
        """
        # 暂存待写入
        self._pending.append(item)

        # 缓存原始响应（若有内容）
        if item.raw_response:
            url = item.url or ""
            # 已存在则先弹出再插入，确保位于最新位置（LRU 语义）
            if url in self._raw_responses:
                self._raw_responses.pop(url)
            self._raw_responses[url] = item.raw_response
            # 超出上限时淘汰最旧
            while len(self._raw_responses) > _MAX_RAW_RESPONSES:
                self._raw_responses.popitem(last=False)

        # 确保定时器处于运行态（可能在空闲时被 stop）
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def set_columns(self, columns: list[str]) -> None:
        """显式设置列集合，覆盖现有列定义。

        首列强制为「来源URL」；传入列表中若已含同名项则去重。
        会清空表格内容并按新列重建表头。

        参数:
            columns: 期望的字段列名列表（不含来源URL）
        """
        merged: list[str] = [_URL_COLUMN_HEADER]
        for col in columns:
            if not isinstance(col, str):
                col = str(col)
            if col not in merged:
                merged.append(col)
        self._columns = merged
        self._apply_columns_to_table()

    def clear_columns(self) -> None:
        """重置列定义为仅含「来源URL」一列，并清空表格内容。"""
        self._columns = [_URL_COLUMN_HEADER]
        self._apply_columns_to_table()

    def clear(self) -> None:
        """清空表格行、待写列表与原始响应缓存（保留列定义）。"""
        self._pending.clear()
        self._raw_responses.clear()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.table.setSortingEnabled(True)

    def export_csv(self, path: str) -> None:
        """将当前表格全部可见单元格导出为 UTF-8 with BOM 的 CSV。

        参数:
            path: 目标 .csv 文件路径
        """
        # 关闭排序，确保导出顺序与视觉行顺序一致
        self.table.setSortingEnabled(False)
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fp:
                writer = csv.writer(fp)
                writer.writerow(self._columns)
                for row in range(self.table.rowCount()):
                    row_values: list[str] = []
                    for col in range(self.table.columnCount()):
                        cell = self.table.item(row, col)
                        row_values.append(cell.text() if cell is not None else "")
                    writer.writerow(row_values)
        finally:
            self.table.setSortingEnabled(True)

    # ---------- 内部：列管理 ----------

    def _ensure_columns_for(self, item) -> None:
        """根据 ResultItem 的字段键扩展列（仅追加新列，不重排已有列）。

        参数:
            item: crawler.models.ResultItem
        """
        new_keys: list[str] = []
        existing = set(self._columns)
        for key in item.fields.keys():
            if key not in existing:
                new_keys.append(key)
                existing.add(key)
        if not new_keys:
            return
        self._columns.extend(new_keys)
        # 追加新列到表格末尾
        self.table.setColumnCount(len(self._columns))
        self.table.setHorizontalHeaderLabels(self._columns)

    def _apply_columns_to_table(self) -> None:
        """根据 self._columns 重建表格列与表头（清空数据）。"""
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.table.setColumnCount(len(self._columns))
        self.table.setHorizontalHeaderLabels(self._columns)
        # 第一列按内容宽度，其余拉伸
        header = self.table.horizontalHeader()
        if len(self._columns) >= 1:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSortingEnabled(True)

    # ---------- 内部：批量刷新 ----------

    def _flush_now(self) -> None:
        """将待写列表中的结果批量写入表格。

        策略：
        - 关闭更新与信号以提升性能；
        - 关闭排序，确保按到达顺序追加；
        - 写入完成后恢复；
        - 超过 _MAX_ROWS 时删除最旧的若干行。
        若 _pending 为空，则停止定时器以避免无谓唤醒。
        """
        if not self._pending:
            self._flush_timer.stop()
            return

        # 取出待写并清空列表
        batch = self._pending
        self._pending = []

        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        was_sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        try:
            for item in batch:
                # 按需扩展列
                self._ensure_columns_for(item)
                # 插入行
                row = self.table.rowCount()
                self.table.insertRow(row)
                # 来源URL 始终在第 0 列
                url_item = QTableWidgetItem(item.url or "")
                self.table.setItem(row, 0, url_item)
                # 其余字段按 self._columns 顺序填入
                for col_idx in range(1, len(self._columns)):
                    col_name = self._columns[col_idx]
                    value = item.fields.get(col_name, "")
                    self.table.setItem(row, col_idx, QTableWidgetItem(str(value)))

            # 控制最大行数：删除最旧的若干行
            overflow = self.table.rowCount() - _MAX_ROWS
            if overflow > 0:
                # removeRow(0) 反复调用代价较高，但仅在超限时触发
                for _ in range(overflow):
                    self.table.removeRow(0)
        finally:
            self.table.setSortingEnabled(was_sorting)
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)

        # 写入完成后若 _pending 仍为空，停止定时器以避免空转
        if not self._pending:
            self._flush_timer.stop()

    # ---------- 内部：交互 ----------

    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        """双击单元格：复制其文本到剪贴板并发出状态提示。"""
        item = self.table.item(row, column)
        text = item.text() if item is not None else ""
        QApplication.clipboard().setText(text)
        self.status_message.emit("已复制单元格内容到剪贴板")

    def _on_context_menu(self, pos) -> None:
        """右键自定义菜单：复制行 / 查看原始响应 / 浏览器打开 / 删除该行。"""
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()

        menu = QMenu(self)

        act_copy_row = menu.addAction("复制行")
        act_view_raw = menu.addAction("查看原始响应")
        act_open_browser = menu.addAction("在浏览器打开来源 URL")
        menu.addSeparator()
        act_delete_row = menu.addAction("删除该行")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        if chosen is act_copy_row:
            self._copy_row(row)
        elif chosen is act_view_raw:
            self._show_raw_response(row)
        elif chosen is act_open_browser:
            self._open_in_browser(row)
        elif chosen is act_delete_row:
            self.table.removeRow(row)
            self.status_message.emit("已删除该行")

    def _row_url(self, row: int) -> str:
        """返回指定行的来源URL（第 0 列文本）。"""
        item = self.table.item(row, 0)
        return item.text() if item is not None else ""

    def _copy_row(self, row: int) -> None:
        """复制指定行所有单元格文本（制表符分隔）到剪贴板。"""
        parts: list[str] = []
        for col in range(self.table.columnCount()):
            cell = self.table.item(row, col)
            parts.append(cell.text() if cell is not None else "")
        text = "\t".join(parts)
        QApplication.clipboard().setText(text)
        self.status_message.emit("已复制整行到剪贴板")

    def _show_raw_response(self, row: int) -> None:
        """以模态对话框展示该行来源 URL 对应的原始响应文本。

        对话框顶部带搜索条：支持关键字搜索、区分大小写、上一个/下一个
        跳转、命中计数；当前命中以黄色背景高亮并自动滚动定位。
        """
        url = self._row_url(row)
        raw = self._raw_responses.get(url, "")

        dialog = QDialog(self)
        dialog.setWindowTitle(f"原始响应 - {url}" if url else "原始响应")
        dialog.resize(720, 520)

        edit = QPlainTextEdit(dialog)
        edit.setReadOnly(True)
        if raw:
            edit.setPlainText(raw)
        else:
            edit.setPlainText("（无原始响应缓存）")

        # ---- 搜索条 ----
        search_input = QLineEdit(dialog)
        search_input.setPlaceholderText("输入关键字搜索…")
        case_check = QCheckBox("区分大小写", dialog)
        prev_btn = QPushButton("上一个", dialog)
        next_btn = QPushButton("下一个", dialog)
        count_label = QLabel("无匹配", dialog)
        count_label.setMinimumWidth(96)

        search_bar = QHBoxLayout()
        search_bar.addWidget(search_input)
        search_bar.addWidget(case_check)
        search_bar.addWidget(prev_btn)
        search_bar.addWidget(next_btn)
        search_bar.addWidget(count_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)

        layout = QVBoxLayout(dialog)
        layout.addLayout(search_bar)
        layout.addWidget(edit)
        layout.addWidget(buttons)

        # ---- 搜索逻辑（闭包，不污染类 API）----
        state = {"matches": [], "current": -1}

        def _find_all(text_edit: QPlainTextEdit, keyword: str, case_sensitive: bool):
            """返回所有命中位置（QTextCursor 列表），按文档顺序排列。"""
            if not keyword:
                return []
            document = text_edit.document()
            flags = QTextDocument.FindFlag(0)
            if case_sensitive:
                flags |= QTextDocument.FindFlag.FindCaseSensitively
            results = []
            cursor = QTextCursor(document)
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            while True:
                found = document.find(keyword, cursor, flags)
                if not found or not found.isValid():
                    break
                # 防御：避免零长度匹配导致死循环
                if found.selectionStart() == found.selectionEnd():
                    break
                results.append(QTextCursor(found))
                cursor.setPosition(found.selectionEnd(), QTextCursor.MoveMode.MoveAnchor)
            return results

        def _refresh_highlight() -> None:
            """高亮当前命中并滚动定位；更新计数标签。"""
            matches = state["matches"]
            total = len(matches)
            # 清空旧高亮
            edit.setExtraSelections([])
            if total == 0:
                count_label.setText("无匹配")
                state["current"] = -1
                return
            idx = state["current"]
            if idx < 0 or idx >= total:
                idx = 0
                state["current"] = 0
            selection = QPlainTextEdit.ExtraSelection()
            selection.format.setBackground(QColor("yellow"))
            selection.cursor = QTextCursor(matches[idx])
            edit.setExtraSelections([selection])
            edit.setTextCursor(QTextCursor(matches[idx]))
            count_label.setText(f"第 {idx + 1}/{total} 处")

        def _do_search() -> None:
            keyword = search_input.text()
            case_sensitive = case_check.isChecked()
            state["matches"] = _find_all(edit, keyword, case_sensitive)
            state["current"] = 0 if state["matches"] else -1
            _refresh_highlight()

        def _goto_next() -> None:
            matches = state["matches"]
            if not matches:
                return
            state["current"] = (state["current"] + 1) % len(matches)
            _refresh_highlight()

        def _goto_prev() -> None:
            matches = state["matches"]
            if not matches:
                return
            state["current"] = (state["current"] - 1) % len(matches)
            _refresh_highlight()

        search_input.textChanged.connect(_do_search)
        case_check.toggled.connect(_do_search)
        next_btn.clicked.connect(_goto_next)
        prev_btn.clicked.connect(_goto_prev)

        dialog.exec()

    def _open_in_browser(self, row: int) -> None:
        """使用系统默认浏览器打开该行的来源 URL。"""
        url = self._row_url(row)
        if not url:
            self.status_message.emit("该行无来源 URL")
            return
        try:
            webbrowser.open(url)
            self.status_message.emit(f"已在浏览器打开：{url}")
        except Exception as exc:  # noqa: BLE001 - 浏览器调用失败不应崩溃
            self.status_message.emit(f"打开浏览器失败：{exc}")

    # ---------- 内部：导出 ----------

    def _on_export_clicked(self) -> None:
        """点击导出按钮：弹出文件保存对话框并写入 CSV。"""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出结果为 CSV",
            "results.csv",
            "CSV 文件 (*.csv);;所有文件 (*.*)",
        )
        if path:
            try:
                self.export_csv(path)
                self.status_message.emit(f"已导出：{path}")
            except Exception as exc:  # noqa: BLE001 - 导出失败不应崩溃
                self.status_message.emit(f"导出失败：{exc}")
