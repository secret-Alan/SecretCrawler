"""更新进度窗口 —— 显示更新下载进度并供用户重启 / 重试。

非模态 QWidget，允许后台执行更新而用户可实时看到进度。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


class UpdateProgressWindow(QWidget):
    """更新进度窗口（非模态 QWidget）。

    显示下载进度条、当前文件、状态文本与更新内容说明；
    更新完成 / 失败后切换按钮为「立即重启 / 稍后」或「重试 / 取消」。
    """

    # 更新完成时发出：True=成功，False=失败
    update_finished = Signal(bool)
    # 用户点击「立即重启」时发出
    restart_requested = Signal()
    # 用户点击「重试」时发出，调用方连接以重新触发更新
    retry_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("更新进度")
        self.setMinimumSize(450, 400)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 标题
        self._title_label = QLabel("正在更新", self)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self._title_label.font()
        font.setPointSize(14)
        font.setBold(True)
        self._title_label.setFont(font)
        layout.addWidget(self._title_label)

        # 进度条
        self._progress_bar = QProgressBar(self)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%p%")
        layout.addWidget(self._progress_bar)

        # 当前文件
        self._file_label = QLabel("准备中...", self)
        layout.addWidget(self._file_label)

        # 状态
        self._status_label = QLabel("", self)
        layout.addWidget(self._status_label)

        # 更新内容说明
        self._content_browser = QTextBrowser(self)
        self._content_browser.setReadOnly(True)
        self._content_browser.setMinimumHeight(150)
        layout.addWidget(self._content_browser)

        # 按钮行
        btn_layout = QHBoxLayout()
        self._restart_btn = QPushButton("立即重启", self)
        self._later_btn = QPushButton("稍后", self)
        self._retry_btn = QPushButton("重试", self)
        self._cancel_btn = QPushButton("取消", self)
        btn_layout.addWidget(self._restart_btn)
        btn_layout.addWidget(self._later_btn)
        btn_layout.addWidget(self._retry_btn)
        btn_layout.addWidget(self._cancel_btn)
        layout.addLayout(btn_layout)

        # 初始按钮可见性：仅取消可见
        self._restart_btn.hide()
        self._later_btn.hide()
        self._retry_btn.hide()
        self._cancel_btn.show()

        # 信号连接
        self._restart_btn.clicked.connect(self._on_restart_clicked)
        self._later_btn.clicked.connect(self.close)
        self._retry_btn.clicked.connect(self.retry_requested.emit)
        self._cancel_btn.clicked.connect(self.close)

    # ---- 内部槽 ----

    def _on_restart_clicked(self) -> None:
        """立即重启：发出信号并关闭窗口。"""
        self.restart_requested.emit()
        self.close()

    # ---- 公共 API ----

    def set_progress(self, pct: int, current_file: str) -> None:
        """设置下载进度与当前文件名。"""
        self._progress_bar.setValue(int(pct))
        self._file_label.setText(f"正在下载: {current_file}")

    def set_update_content(self, content: str) -> None:
        """设置更新内容说明（Markdown 优先，失败回退纯文本）。"""
        try:
            self._content_browser.setMarkdown(content)
        except Exception:  # noqa: BLE001
            try:
                self._content_browser.setHtml(content)
            except Exception:  # noqa: BLE001
                self._content_browser.setPlainText(content)

    def show_success(self) -> None:
        """显示更新成功状态。"""
        self._progress_bar.setValue(100)
        self._status_label.setText("更新完成！重启应用以生效。")
        self._restart_btn.show()
        self._later_btn.show()
        self._retry_btn.hide()
        self._cancel_btn.hide()
        self.update_finished.emit(True)

    def show_error(self, error_msg: str) -> None:
        """显示更新失败状态。"""
        self._status_label.setText(error_msg)
        self._retry_btn.show()
        self._cancel_btn.show()
        self._restart_btn.hide()
        self._later_btn.hide()
        self.update_finished.emit(False)

    def show_downloading(self) -> None:
        """恢复为下载中状态：仅显示取消按钮。"""
        self._restart_btn.hide()
        self._later_btn.hide()
        self._retry_btn.hide()
        self._cancel_btn.show()
