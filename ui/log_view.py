"""实时日志视图。

本模块提供专业爬虫项目的「实时日志视图」控件，满足 spec 中
「实时日志视图」需求：带时间戳与级别的彩色日志、级别过滤、关键字
搜索、清空与导出。同时提供一个 QtLogHandler，将 Python 标准库
logging 模块的日志安全地转发到本视图（跨线程时通过 QueuedConnection
调度回 GUI 线程）。

类定义:
    LogView        - 实时日志视图控件（QWidget）
    QtLogHandler   - 桥接 logging 模块到 LogView 的处理器
"""

from __future__ import annotations

import html
import logging
from datetime import datetime

from PySide6.QtCore import Qt, QMetaObject, Q_ARG, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# 显示区最多保留的块数（用于限制 UI 内存占用；旧条目会被自动丢弃）
_MAX_DISPLAY_BLOCKS = 5000

# 内存中保留的最多日志条数（用于过滤/导出，超过时丢弃最早的）
_MAX_MEM_RECORDS = 50000

# 各日志级别对应的颜色（HTML 颜色字符串）
_LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG: "#888888",
    logging.INFO: "#0a7f3f",
    logging.WARNING: "#c47f00",
    logging.ERROR: "#c0392b",
    logging.CRITICAL: "#7f1c1c",
}

# 级别下拉的可选项：(显示文本, 级别值) —— 0 表示 ALL
_LEVEL_OPTIONS: list[tuple[str, int]] = [
    ("ALL", 0),
    ("DEBUG", logging.DEBUG),
    ("INFO", logging.INFO),
    ("WARNING", logging.WARNING),
    ("ERROR", logging.ERROR),
    ("CRITICAL", logging.CRITICAL),
]


def _level_name(level: int) -> str:
    """返回日志级别的可读名称（如 'INFO'、'WARNING'）。"""
    return logging.getLevelName(level)


class LogView(QWidget):
    """实时日志视图控件。

    顶部工具栏提供「日志级别」下拉、「搜索关键字」输入框、「清空」与
    「导出」按钮；下方为只读的 QPlainTextEdit 显示区。所有日志条目同时
    存入内存列表，便于在过滤条件变化时重绘，或导出为文本文件。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # 内存中的全部日志记录：每项为 (timestamp_str, level, message)
        self._records: list[tuple[str, int, str]] = []
        # 当前生效的最低显示级别（0 表示显示全部）
        self._min_level: int = logging.INFO
        # 当前搜索关键字（已转小写；为空表示无关键字过滤）
        self._keyword: str = ""

        # ---- 顶部工具栏 ----
        toolbar = QHBoxLayout()
        level_label = QLabel("日志级别:", self)
        toolbar.addWidget(level_label)

        self._level_combo = QComboBox(self)
        for text, value in _LEVEL_OPTIONS:
            self._level_combo.addItem(text, value)
        # 默认 INFO
        idx = self._level_combo.findData(logging.INFO)
        if idx >= 0:
            self._level_combo.setCurrentIndex(idx)
        toolbar.addWidget(self._level_combo)

        self._search_edit = QLineEdit(self)
        self._search_edit.setPlaceholderText("搜索关键字…")
        toolbar.addWidget(self._search_edit, 1)

        self._clear_btn = QPushButton("清空", self)
        self._export_btn = QPushButton("导出", self)
        toolbar.addWidget(self._clear_btn)
        toolbar.addWidget(self._export_btn)

        # ---- 日志显示区 ----
        self._text_edit = QPlainTextEdit(self)
        self._text_edit.setReadOnly(True)
        # 限制 UI 中保留的块数以控制内存；旧块自动丢弃
        self._text_edit.setMaximumBlockCount(_MAX_DISPLAY_BLOCKS)

        # ---- 整体布局：工具栏在上，显示区在下 ----
        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(self._text_edit)

        # ---- 信号绑定 ----
        self._level_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._search_edit.textChanged.connect(self._on_filter_changed)
        self._clear_btn.clicked.connect(self.clear)
        self._export_btn.clicked.connect(self._on_export_clicked)

    # ---------- 公共 API ----------

    @Slot(int, str)
    def append_log(self, level: int, message: str) -> None:
        """追加一条日志。

        线程安全说明：本槽函数既可以直接被同线程调用（例如引擎的信号槽
        连接），也可以经 QtLogHandler 通过 QMetaObject.invokeMethod +
        QueuedConnection 调度到 GUI 线程执行。Qt 的信号槽机制保证不会
        与 GUI 线程并发进入。

        消息本身已包含时间戳/级别/模块前缀（统一日志格式），此处不再
        重复生成时间戳，仅按级别着色显示。

        参数:
            level:   logging 模块级别常量（logging.INFO 等）
            message: 日志正文（纯文本，已含统一格式前缀）
        """
        self._records.append(("", level, message))
        # 超过内存上限时丢弃最早的记录
        if len(self._records) > _MAX_MEM_RECORDS:
            del self._records[: len(self._records) - _MAX_MEM_RECORDS]
        # 仅当当前条目通过过滤条件时才追加显示
        if self._passes_filter(level, message):
            self._append_html_line(level, message)

    def set_min_level(self, level: int) -> None:
        """设置最低显示级别，并重绘显示区。

        参数 level 为 0 表示显示全部；其它值应为 logging 模块的级别常量。
        """
        self._min_level = level
        # 同步下拉选择，使 UI 与程序化调用保持一致
        idx = self._level_combo.findData(level)
        if idx >= 0 and self._level_combo.currentIndex() != idx:
            # 切换下拉会触发 _on_filter_changed 完成重绘
            self._level_combo.setCurrentIndex(idx)
        else:
            # index 未变化或未找到，手动重绘
            self._rerender()

    def clear(self) -> None:
        """清空内存中的全部记录与显示区。"""
        self._records.clear()
        self._text_edit.clear()

    def export_to_file(self, path: str) -> None:
        """将内存中的全部日志记录以纯文本形式写入指定文件。

        每行即一条日志（消息本身已包含时间戳/级别/模块等前缀）。
        """
        with open(path, "w", encoding="utf-8") as fp:
            for _ts, _level, message in self._records:
                fp.write(f"{message}\n")

    def make_handler(self) -> "QtLogHandler":
        """返回一个与本视图绑定的 logging.Handler。

        调用方可以将其添加到任意 logger，例如：
            logging.getLogger().addHandler(view.make_handler())
        """
        return QtLogHandler(self)

    # ---------- 内部辅助 ----------

    def _passes_filter(self, level: int, message: str) -> bool:
        """判断给定记录是否通过当前的级别与关键字过滤。"""
        if self._min_level > 0 and level < self._min_level:
            return False
        if self._keyword and self._keyword not in message.lower():
            return False
        return True

    def _append_html_line(self, level: int, message: str) -> None:
        """以 HTML 形式追加一行带颜色高亮的日志。

        消息本身已包含时间戳/级别/模块等前缀（统一日志格式），
        此处仅按级别着色，不再重复添加时间戳与级别前缀。
        """
        color = _LEVEL_COLORS.get(level, "#000000")
        msg_html = html.escape(message)
        # 消息按级别着色；CRITICAL 整行加粗
        if level >= logging.CRITICAL:
            line = f'<b><span style="color:{color}">{msg_html}</span></b>'
        else:
            line = f'<span style="color:{color}">{msg_html}</span>'
        self._text_edit.appendHtml(line)

    def _rerender(self) -> None:
        """根据当前过滤条件从内存列表重建显示区。

        仅取最近 _MAX_DISPLAY_BLOCKS 条原始记录参与渲染，避免内存列表
        过长时重绘卡顿（搜索框每次按键都会触发本方法）。
        """
        records = self._records
        if len(records) > _MAX_DISPLAY_BLOCKS:
            records = records[-_MAX_DISPLAY_BLOCKS:]
        # setPlainText 不会触发额外信号；maximumBlockCount 仍生效
        self._text_edit.setPlainText("")
        for _ts, level, message in records:
            if self._passes_filter(level, message):
                self._append_html_line(level, message)

    def _on_filter_changed(self, *args: object) -> None:
        """级别下拉或搜索框变化时更新过滤条件并重绘。

        参数列表使用 *args 以兼容 QComboBox.currentIndexChanged(int) 与
        QLineEdit.textChanged(QString) 两种信号签名。
        """
        data = self._level_combo.currentData()
        self._min_level = int(data) if isinstance(data, int) else 0
        self._keyword = self._search_edit.text().strip().lower()
        self._rerender()

    def _on_export_clicked(self) -> None:
        """点击导出按钮：弹出文件保存对话框并写入文件。"""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出日志",
            "crawler.log",
            "日志文件 (*.log *.txt);;所有文件 (*.*)",
        )
        if path:
            self.export_to_file(path)


class QtLogHandler(logging.Handler):
    """将 Python logging 模块的日志转发到 LogView。

    因为 logging 可能从工作线程被调用（例如调度器在子线程中输出日志），
    本处理器的 emit 通过 QMetaObject.invokeMethod 配合
    Qt.QueuedConnection 将调用调度到 GUI 线程执行，确保不会从非 GUI
    线程触碰 QWidget。
    """

    def __init__(self, log_view: LogView) -> None:
        super().__init__()
        self._log_view = log_view

    def emit(self, record: logging.LogRecord) -> None:
        """将日志记录转发到 LogView.append_log（在 GUI 线程执行）。"""
        try:
            message = record.getMessage()
        except Exception:
            # 格式化失败时退回原始消息文本，避免日志系统自身崩溃
            message = record.msg if isinstance(record.msg, str) else str(record.msg)
        try:
            QMetaObject.invokeMethod(
                self._log_view,
                "append_log",
                Qt.QueuedConnection,
                Q_ARG(int, record.levelno),
                Q_ARG(str, message),
            )
        except Exception:
            # 调度失败（例如应用退出阶段对象已销毁），忽略以保护日志系统
            pass
