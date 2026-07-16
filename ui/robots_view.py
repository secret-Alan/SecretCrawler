"""robots.txt 查看器面板。

本模块为 spec 中「robots.txt 查看器」需求提供 :class:`RobotsView` 控件，
用于在 UI 中展示目标站点的 robots.txt 原文及 UA 视角下的摘要信息
（是否允许抓取 / Crawl-delay / Sitemap）。

控件自包含，不依赖运行中的引擎 / fetcher：通过
:func:`crawler.robots.fetch_raw_robots` 的 urllib 回退路径直接下载，
通过 :func:`crawler.robots.parse_robots_summary` 解析摘要。

类定义:
    RobotsView - robots.txt 查看器面板控件（QWidget）
"""

from __future__ import annotations

from urllib.parse import urlsplit

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from crawler.robots import fetch_raw_robots, parse_robots_summary


class RobotsView(QWidget):
    """robots.txt 查看器面板控件。

    顶部提供「刷新」按钮与当前站点根 URL 标签；中部为摘要标签
    （是否允许抓取 / Crawl-delay / Sitemap）；下部为只读的 robots.txt 原文。

    通过 :meth:`refresh` 接收起始 URL 列表并加载首个站点的 robots.txt。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # 最近一次传入的起始 URL 列表，供「刷新」按钮重新抓取使用
        self._last_start_urls: list[str] = []
        # 当前站点根（scheme://host），未加载时为空字符串
        self._base_url: str = ""

        root = QVBoxLayout(self)

        # ---- 顶部工具栏：刷新按钮 + 当前站点 URL ----
        top_bar = QHBoxLayout()
        self._refresh_btn = QPushButton("刷新", self)
        self._url_label = QLabel("当前站点: -", self)
        top_bar.addWidget(self._refresh_btn)
        top_bar.addWidget(self._url_label, 1)
        root.addLayout(top_bar)

        # ---- 摘要标签：稍大、加粗 ----
        self._summary_label = QLabel("未配置起始 URL", self)
        self._summary_label.setWordWrap(True)
        summary_font = self._summary_label.font()
        summary_font.setBold(True)
        summary_font.setPointSize(summary_font.pointSize() + 2)
        self._summary_label.setFont(summary_font)
        root.addWidget(self._summary_label)

        # ---- robots.txt 原文：只读、等宽字体 ----
        self._raw_edit = QPlainTextEdit(self)
        self._raw_edit.setReadOnly(True)
        mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self._raw_edit.setFont(mono_font)
        root.addWidget(self._raw_edit, 1)

        # ---- 信号绑定 ----
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)

    # ---------- 公共 API ----------

    def refresh(self, start_urls: list[str]) -> None:
        """根据起始 URL 列表加载首个站点的 robots.txt 并刷新视图。

        - ``start_urls`` 为空时提示未配置并清空原文。
        - 取首个 URL 推导 ``scheme://host`` 作为站点根，下载并解析
          robots.txt；下载失败 / 404 / 原文为空均按「该站点未提供 robots.txt」处理。
        - 任何异常都被吞掉，UI 仅显示「robots.txt 获取失败」。
        """
        self._last_start_urls = list(start_urls) if start_urls else []
        try:
            if not start_urls:
                self._summary_label.setText("未配置起始 URL")
                self._raw_edit.setPlainText("")
                return

            first = start_urls[0]
            parts = urlsplit(first)
            scheme = parts.scheme.lower()
            host = parts.hostname or ""
            if not scheme or not host:
                self._summary_label.setText("未配置起始 URL")
                self._raw_edit.setPlainText("")
                return
            self._base_url = f"{scheme}://{host}"
            self._url_label.setText(f"当前站点: {self._base_url}")

            raw_text, status = fetch_raw_robots(self._base_url)
            if status == 200 and raw_text:
                self._raw_edit.setPlainText(raw_text)
                summary = parse_robots_summary(raw_text)
                self._summary_label.setText(self._format_summary(summary))
            else:
                self._raw_edit.setPlainText("")
                self._summary_label.setText("该站点未提供 robots.txt")
        except Exception:
            self._summary_label.setText("robots.txt 获取失败")
            self._raw_edit.setPlainText("")

    # ---------- 内部辅助 ----------

    def _on_refresh_clicked(self) -> None:
        """「刷新」按钮槽：使用最近一次的起始 URL 列表重新抓取。"""
        self.refresh(self._last_start_urls)

    @staticmethod
    def _format_summary(summary: dict) -> str:
        """将 :func:`parse_robots_summary` 的字典格式化为摘要展示文本。"""
        allowed_text = "是" if summary.get("allowed") else "否"
        delay = summary.get("crawl_delay")
        delay_text = f"{delay}s" if delay is not None else "无"
        sitemaps = summary.get("sitemaps") or []
        if sitemaps:
            sitemap_text = ", ".join(sitemaps)
        else:
            sitemap_text = "无"
        return (
            f"允许抓取: {allowed_text} | "
            f"Crawl-delay: {delay_text} | "
            f"Sitemap: {sitemap_text}"
        )
