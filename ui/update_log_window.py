"""更新内容窗口：展示软件各版本的更新日志。

按版本从高到低依次拼接 Markdown 文档，统一渲染展示。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

_LOG = logging.getLogger("ui.update_log_window")


class UpdateLogWindow(QDialog):
    """更新内容展示窗口（模态）。

    通过 ``set_logs()`` 传入 ``[(version, content), ...]`` 列表（按版本降序），
    各版本以二级标题分隔，版本间以水平线分隔，统一以 Markdown 渲染展示。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("更新内容")
        self.setMinimumSize(600, 500)

        layout = QVBoxLayout(self)

        # 标题
        self._title_label = QLabel("软件更新内容", self)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = self._title_label.font()
        title_font.setPointSize(14)
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        layout.addWidget(self._title_label)

        # 内容区
        self._content_browser = QTextBrowser(self)
        self._content_browser.setReadOnly(True)
        self._content_browser.setOpenExternalLinks(True)
        self._content_browser.setMinimumHeight(350)
        layout.addWidget(self._content_browser)

        # 关闭按钮
        self._close_btn = QPushButton("关闭", self)
        self._close_btn.clicked.connect(self.accept)
        layout.addWidget(self._close_btn)

    def set_logs(self, logs: list[tuple[str, str]]) -> None:
        """设置更新日志列表。

        ``logs`` 为 ``[(version, content), ...]`` 元组列表，按版本降序排列。
        各版本拼成 ``## v{version}\\n\\n{content}\\n\\n---\\n\\n``，统一以
        Markdown 渲染；setMarkdown 不可用时回退到 ``markdown`` 库转 HTML，
        再不可用则回退纯文本展示。
        """
        combined_parts: list[str] = []
        for version, content in logs:
            combined_parts.append(f"## v{version}\n\n{content}\n\n---\n\n")
        combined_text = "".join(combined_parts)

        # 优先使用 Qt 内建 Markdown 渲染
        try:
            self._content_browser.setMarkdown(combined_text)
            return
        except Exception as e:  # noqa: BLE001
            _LOG.debug("setMarkdown 渲染失败，尝试 markdown 库回退: %s", e)

        # 回退 1：使用 markdown 库转 HTML
        try:
            import markdown as md  # type: ignore[import-not-found]

            html_text = md.markdown(combined_text, extensions=["extra", "sane"])
            self._content_browser.setHtml(html_text)
            return
        except ImportError:
            _LOG.debug("markdown 库不可用，回退纯文本展示")
        except Exception as e:  # noqa: BLE001
            _LOG.debug("markdown 库转 HTML 失败，回退纯文本展示: %s", e)

        # 回退 2：纯文本
        self._content_browser.setPlainText(combined_text)
