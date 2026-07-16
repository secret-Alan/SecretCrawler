"""用户协议窗口：展示协议内容并要求用户滚动至底部后勾选同意。"""

from __future__ import annotations

try:
    import markdown as md
except ImportError:
    md = None

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
)


class AgreementWindow(QDialog):
    """用户协议窗口（模态）。

    展示 Markdown 格式的协议内容，要求用户滚动至底部后方可勾选「同意」；
    勾选后「我同意」按钮才会启用。点击「我不同意，退出软件」则 ``reject()``，
    点击「我同意」则 ``accept()``。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("用户协议")
        self.setModal(True)
        self.setMinimumSize(600, 500)
        self.resize(600, 500)

        # 滚动状态跟踪
        self._scrolled_to_bottom = False

        layout = QVBoxLayout(self)

        # 标题
        self._title_label = QLabel("用户协议", self)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = self._title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        layout.addWidget(self._title_label)

        # 协议内容区（Markdown 渲染，只读，可滚动）
        self._content_browser = QTextBrowser(self)
        self._content_browser.setReadOnly(True)
        self._content_browser.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._content_browser.setMinimumHeight(350)
        self._content_browser.verticalScrollBar().valueChanged.connect(
            self._on_scroll
        )
        layout.addWidget(self._content_browser)

        # 提示文本（灰色斜体，随滚动状态动态更新）
        self._hint_label = QLabel("请滚动至底部后勾选同意", self)
        self._hint_label.setStyleSheet("color: #888; font-style: italic;")
        layout.addWidget(self._hint_label)

        # 同意复选框（初始禁用，仅滚动到底部后启用）
        self._agree_check = QCheckBox(
            "我愿意自觉遵守网络爬虫使用规则，并对自己的行为负责", self
        )
        self._agree_check.setEnabled(False)
        self._agree_check.toggled.connect(self._on_agree_toggled)
        layout.addWidget(self._agree_check)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)

        self._disagree_btn = QPushButton("我不同意，退出软件", self)
        self._disagree_btn.setDefault(True)
        self._disagree_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._disagree_btn)

        self._agree_btn = QPushButton("我同意", self)
        self._agree_btn.setEnabled(False)
        self._agree_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self._agree_btn)

        layout.addLayout(btn_layout)

        # 默认焦点放在「我不同意」按钮上
        self._disagree_btn.setFocus()

    # ---- 滚动检测 ----

    def _on_scroll(self) -> None:
        """滚动条变化时检查是否已滚到底部，据此启用/禁用复选框与按钮。"""
        sb = self._content_browser.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 2
        self._scrolled_to_bottom = bool(at_bottom)
        if at_bottom:
            self._agree_check.setEnabled(True)
            self._hint_label.setText("您可以勾选同意")
        else:
            self._agree_check.setEnabled(False)
            self._agree_check.setChecked(False)
            self._agree_btn.setEnabled(False)
            self._hint_label.setText("请滚动至底部后勾选同意")

    def _check_scroll_state(self) -> None:
        """布局稳定后重新检查滚动状态。

        ``set_content`` 后滚动条最大值可能未立即更新（Qt 布局延迟），
        需延迟重检以获得准确的底部判断。
        """
        self._on_scroll()

    # ---- 复选框 ----

    def _on_agree_toggled(self, checked: bool) -> None:
        """复选框状态变化时启用/禁用「我同意」按钮。"""
        self._agree_btn.setEnabled(checked)

    # ---- 公共 API ----

    def set_content(self, markdown_text: str) -> None:
        """设置协议内容（Markdown）。

        使用 ``markdown`` 库将 Markdown 转为 HTML（支持表格、围栏代码、目录），
        再通过 ``QTextBrowser.setHtml`` 渲染，避免 ``setMarkdown`` 在部分内容
        （如表格）上截断的问题。设置后重置滚动条至顶部，并延迟重检滚动状态。
        """
        if md is not None:
            html = md.markdown(markdown_text, extensions=['tables', 'fenced_code', 'toc'])
            self._content_browser.setHtml(html)
        else:
            # 回退到 Qt 内置 Markdown 渲染（部分表格语法可能不完整）
            import logging
            logging.getLogger("ui.agreement").warning(
                "markdown 模块未安装，使用 Qt 内置 Markdown 渲染"
            )
            self._content_browser.setMarkdown(markdown_text)

        # 重置到顶部并恢复初始禁用状态
        sb = self._content_browser.verticalScrollBar()
        sb.setValue(0)
        self._scrolled_to_bottom = False
        self._agree_check.setEnabled(False)
        self._agree_check.setChecked(False)
        self._agree_btn.setEnabled(False)
        self._hint_label.setText("请滚动至底部后勾选同意")

        # 布局稳定后重新检查（短内容可能无需滚动即视为已到底）
        QTimer.singleShot(50, self._check_scroll_state)
