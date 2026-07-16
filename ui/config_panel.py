"""爬虫配置面板。

将 :class:`crawler.models.CrawlConfig` 的全部字段以分组表单的形式暴露给
用户编辑。面板整体包裹在 ``QScrollArea`` 中以便内容超长时滚动查看。

公共 API：
    ConfigPanel.get_config()      -> CrawlConfig
    ConfigPanel.set_config(cfg)   -> None
    ConfigPanel.load_from_file(p) -> None
    ConfigPanel.save_to_file(p)   -> None
"""

from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QLineEdit,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QComboBox,
    QPushButton,
    QScrollArea,
    QMessageBox,
)

from crawler.models import CrawlConfig, ExtractionRule, SelectorType
from ui.widgets import FilePathEdit, KeyValueTable, RulesTable


# 存储格式下拉可选项（与 CrawlConfig 注释中的合法值保持一致）
_STORAGE_FORMATS = ["CSV", "JSON", "EXCEL", "SQLITE", "NONE"]

# 请求方法下拉可选项
_HTTP_METHODS = ["GET", "POST"]


class ConfigPanel(QWidget):
    """爬虫配置面板控件。

    持有所有可编辑控件的引用，支持将控件状态与 :class:`CrawlConfig`
    互转，以及 JSON 项目文件（.crawlproj）的加载与保存。
    """

    # 「通过网页爬寻」按钮：打开嵌入式浏览器拾取器
    # 参数携带第一个起始 URL（无则空串）与浏览器类型
    open_picker_requested = Signal(str, str)

    # 「按 .scskill 运行」按钮：触发 scskill 运行
    # 参数为 scskill 名称与浏览器类型
    run_scskill_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # ---- 顶层布局：QScrollArea 包裹一个内容容器 ----
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        outer.addWidget(self._scroll)

        container = QWidget(self._scroll)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(8, 8, 8, 8)
        container_layout.setSpacing(10)
        self._scroll.setWidget(container)

        # ---- 依次构建各分组 ----
        self._build_section_start(container_layout)        # 起始配置
        self._build_section_crawl(container_layout)        # 抓取控制
        self._build_section_headers(container_layout)      # 请求头与 Cookie
        self._build_section_proxy_ua(container_layout)     # 代理与 UA
        self._build_section_rules_filter(container_layout) # 规则与过滤
        self._build_section_extraction(container_layout)   # 提取规则
        self._build_section_storage(container_layout)      # 存储与输出
        self._build_section_download(container_layout)     # 文件下载

        # 「爬寻步骤」分区：.scskill 导入 + 浏览器选择 + 通过网页爬寻按钮
        self._build_section_picker_steps(container_layout)

        container_layout.addStretch(1)

    # ------------------------------------------------------------------
    # 分组构建
    # ------------------------------------------------------------------

    def _build_section_start(self, parent_layout: QVBoxLayout) -> None:
        """Section 1: 起始配置。"""
        group = QGroupBox("起始配置", self)
        form = QFormLayout(group)

        # 起始 URL（多行，每行一个）
        self.start_urls_edit = QPlainTextEdit(group)
        self.start_urls_edit.setPlaceholderText("每行一个 URL，例如 https://example.com/")
        self.start_urls_edit.setFixedHeight(80)
        form.addRow("起始 URL:", self.start_urls_edit)

        # 请求方法 GET / POST
        self.method_combo = QComboBox(group)
        self.method_combo.addItems(_HTTP_METHODS)
        form.addRow("请求方法:", self.method_combo)

        # POST 数据：键值对表格（始终显示）
        self.post_data_table = KeyValueTable(group)
        form.addRow("POST 数据:", self.post_data_table)

        parent_layout.addWidget(group)

    def _build_section_crawl(self, parent_layout: QVBoxLayout) -> None:
        """Section 2: 抓取控制。"""
        group = QGroupBox("抓取控制", self)
        form = QFormLayout(group)

        # 最大深度，默认 3（取消上限）
        self.max_depth_spin = QSpinBox(group)
        self.max_depth_spin.setRange(0, 2147483647)
        self.max_depth_spin.setValue(3)
        form.addRow("最大深度:", self.max_depth_spin)

        # 最大页面数，默认 100（取消上限）
        self.max_pages_spin = QSpinBox(group)
        self.max_pages_spin.setRange(1, 2147483647)
        self.max_pages_spin.setValue(100)
        form.addRow("最大页面数:", self.max_pages_spin)

        # 并发线程数，默认 5（取消上限）
        self.concurrency_spin = QSpinBox(group)
        self.concurrency_spin.setRange(1, 2147483647)
        self.concurrency_spin.setValue(5)
        form.addRow("并发线程数:", self.concurrency_spin)

        # 请求间隔，步长 0.1，默认 0.5（取消上限）
        self.request_interval_spin = QDoubleSpinBox(group)
        self.request_interval_spin.setRange(0.0, 2147483647.0)
        self.request_interval_spin.setSingleStep(0.1)
        self.request_interval_spin.setValue(0.5)
        form.addRow("请求间隔(秒):", self.request_interval_spin)

        # 请求超时 1.0-300，默认 30
        self.timeout_spin = QDoubleSpinBox(group)
        self.timeout_spin.setRange(1.0, 2147483647.0)
        self.timeout_spin.setSingleStep(0.1)
        self.timeout_spin.setValue(30.0)
        form.addRow("请求超时(秒):", self.timeout_spin)

        # 最大重试次数 0-10，默认 3
        self.max_retries_spin = QSpinBox(group)
        self.max_retries_spin.setRange(0, 2147483647)
        self.max_retries_spin.setValue(3)
        form.addRow("最大重试次数:", self.max_retries_spin)

        # 最大重定向 0-50，默认 10
        self.max_redirects_spin = QSpinBox(group)
        self.max_redirects_spin.setRange(0, 2147483647)
        self.max_redirects_spin.setValue(10)
        form.addRow("最大重定向:", self.max_redirects_spin)

        parent_layout.addWidget(group)

    def _build_section_headers(self, parent_layout: QVBoxLayout) -> None:
        """Section 3: 请求头与 Cookie。"""
        group = QGroupBox("请求头与 Cookie", self)
        form = QFormLayout(group)

        self.headers_table = KeyValueTable(group)
        form.addRow("请求头:", self.headers_table)

        self.cookies_table = KeyValueTable(group)
        form.addRow("Cookie:", self.cookies_table)

        parent_layout.addWidget(group)

    def _build_section_proxy_ua(self, parent_layout: QVBoxLayout) -> None:
        """Section 4: 代理与 UA。"""
        group = QGroupBox("代理与 UA", self)
        form = QFormLayout(group)

        # 代理列表（多行）
        self.proxies_edit = QPlainTextEdit(group)
        self.proxies_edit.setPlaceholderText(
            "每行一个代理，例如 http://host:port 或 socks5://host:port"
        )
        self.proxies_edit.setFixedHeight(80)
        form.addRow("代理列表:", self.proxies_edit)

        # User-Agent 列表（多行）
        self.user_agents_edit = QPlainTextEdit(group)
        self.user_agents_edit.setPlaceholderText("每行一个 User-Agent 字符串")
        self.user_agents_edit.setFixedHeight(80)
        form.addRow("User-Agent 列表:", self.user_agents_edit)

        # UA 轮换开关，默认勾选
        self.rotate_user_agents_check = QCheckBox("启用 UA 轮换", group)
        self.rotate_user_agents_check.setChecked(True)
        form.addRow("", self.rotate_user_agents_check)

        parent_layout.addWidget(group)

    def _build_section_rules_filter(self, parent_layout: QVBoxLayout) -> None:
        """Section 5: 规则与过滤。"""
        group = QGroupBox("规则与过滤", self)
        form = QFormLayout(group)

        # 遵守 robots.txt，默认勾选
        self.respect_robots_check = QCheckBox("遵守 robots.txt", group)
        self.respect_robots_check.setChecked(True)
        form.addRow("", self.respect_robots_check)

        # 爬取前合规评估，默认勾选
        self._compliance_check = QCheckBox(
            "爬取前合规评估（核查敏感数据与用户协议）", group
        )
        self._compliance_check.setChecked(True)
        self._compliance_check.stateChanged.connect(
            self._on_compliance_check_changed
        )
        form.addRow("", self._compliance_check)

        # 仅起始域名，默认勾选 → restrict_to_start_domains
        self.restrict_to_start_domains_check = QCheckBox(
            "仅起始域名", group
        )
        self.restrict_to_start_domains_check.setChecked(True)
        form.addRow("", self.restrict_to_start_domains_check)

        # 允许域名（可选，每行一个）
        self.allowed_domains_edit = QPlainTextEdit(group)
        self.allowed_domains_edit.setPlaceholderText("每行一个允许的域名（可选）")
        self.allowed_domains_edit.setFixedHeight(60)
        form.addRow("允许域名:", self.allowed_domains_edit)

        # URL 白名单正则（每行一个）
        self.url_whitelist_edit = QPlainTextEdit(group)
        self.url_whitelist_edit.setPlaceholderText("每行一个正则表达式")
        self.url_whitelist_edit.setFixedHeight(60)
        form.addRow("URL 白名单正则:", self.url_whitelist_edit)

        # URL 黑名单正则（每行一个）
        self.url_blacklist_edit = QPlainTextEdit(group)
        self.url_blacklist_edit.setPlaceholderText("每行一个正则表达式")
        self.url_blacklist_edit.setFixedHeight(60)
        form.addRow("URL 黑名单正则:", self.url_blacklist_edit)

        # 自动跟随同域链接，默认勾选 → auto_follow_links
        self.auto_follow_links_check = QCheckBox("自动跟随同域链接", group)
        self.auto_follow_links_check.setChecked(True)
        form.addRow("", self.auto_follow_links_check)

        # 自动发现分页，默认勾选 → auto_detect_pagination
        self.auto_detect_pagination_check = QCheckBox("自动发现分页", group)
        self.auto_detect_pagination_check.setChecked(True)
        form.addRow("", self.auto_detect_pagination_check)

        parent_layout.addWidget(group)

    def _build_section_extraction(self, parent_layout: QVBoxLayout) -> None:
        """Section 6: 提取规则。"""
        group = QGroupBox("提取规则", self)
        form = QFormLayout(group)

        self.rules_table = RulesTable(group)
        form.addRow("提取规则:", self.rules_table)

        parent_layout.addWidget(group)

    def _build_section_storage(self, parent_layout: QVBoxLayout) -> None:
        """Section 7: 存储与输出。"""
        group = QGroupBox("存储与输出", self)
        form = QFormLayout(group)

        # 存储格式
        self.storage_format_combo = QComboBox(group)
        self.storage_format_combo.addItems(_STORAGE_FORMATS)
        form.addRow("存储格式:", self.storage_format_combo)

        # 输出路径（目录）
        self.output_path_edit = FilePathEdit(
            path="output", is_dir=True, parent=group
        )
        form.addRow("输出路径:", self.output_path_edit)

        # 持久化 URL 去重(SQLite)，默认不勾选
        self.persist_state_sqlite_check = QCheckBox(
            "持久化 URL 去重(SQLite)", group
        )
        self.persist_state_sqlite_check.setChecked(False)
        form.addRow("", self.persist_state_sqlite_check)

        parent_layout.addWidget(group)

    def _build_section_download(self, parent_layout: QVBoxLayout) -> None:
        """Section 8: 文件下载。"""
        group = QGroupBox("文件下载", self)
        form = QFormLayout(group)

        # 启用文件下载，默认不勾选
        self.download_files_check = QCheckBox("启用文件下载", group)
        self.download_files_check.setChecked(False)
        form.addRow("", self.download_files_check)

        # 下载 URL 正则（兼容可选）
        self.download_url_regex_edit = QLineEdit(group)
        self.download_url_regex_edit.setPlaceholderText(
            r"例如 https?://.*\.(png|jpg|pdf)"
        )
        form.addRow("下载 URL 正则(兼容可选):", self.download_url_regex_edit)

        # 后缀名白名单
        self.download_ext_whitelist_edit = QPlainTextEdit(group)
        self.download_ext_whitelist_edit.setPlaceholderText(
            "每行一个后缀，无需带点，如 png jpg pdf（仅下载这些后缀）"
        )
        self.download_ext_whitelist_edit.setFixedHeight(60)
        form.addRow("后缀名白名单:", self.download_ext_whitelist_edit)

        # 后缀名黑名单
        self.download_ext_blacklist_edit = QPlainTextEdit(group)
        self.download_ext_blacklist_edit.setPlaceholderText(
            "每行一个后缀，无需带点，如 exe dll（不下载这些后缀）"
        )
        self.download_ext_blacklist_edit.setFixedHeight(60)
        form.addRow("后缀名黑名单:", self.download_ext_blacklist_edit)

        # 字符串白名单
        self.download_str_whitelist_edit = QPlainTextEdit(group)
        self.download_str_whitelist_edit.setPlaceholderText(
            "每行一个字符串，URL/文件名须包含其一才下载"
        )
        self.download_str_whitelist_edit.setFixedHeight(60)
        form.addRow("字符串白名单:", self.download_str_whitelist_edit)

        # 字符串黑名单
        self.download_str_blacklist_edit = QPlainTextEdit(group)
        self.download_str_blacklist_edit.setPlaceholderText(
            "每行一个字符串，URL/文件名含其一则不下载"
        )
        self.download_str_blacklist_edit.setFixedHeight(60)
        form.addRow("字符串黑名单:", self.download_str_blacklist_edit)

        # 下载目录
        self.download_dir_edit = FilePathEdit(
            path="downloads", is_dir=True, parent=group
        )
        form.addRow("下载目录:", self.download_dir_edit)

        parent_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def get_config(self) -> CrawlConfig:
        """读取所有控件状态，构建并返回一个新的 :class:`CrawlConfig`。"""
        return CrawlConfig(
            # 起始配置
            start_urls=self._lines_from_edit(self.start_urls_edit),
            method=self.method_combo.currentText(),
            post_data=self.post_data_table.get_items(),
            # 抓取控制
            max_depth=self.max_depth_spin.value(),
            max_pages=self.max_pages_spin.value(),
            concurrency=self.concurrency_spin.value(),
            request_interval=self.request_interval_spin.value(),
            timeout=self.timeout_spin.value(),
            max_retries=self.max_retries_spin.value(),
            max_redirects=self.max_redirects_spin.value(),
            # 请求头与 Cookie
            headers=self.headers_table.get_items(),
            cookies=self.cookies_table.get_items(),
            # 代理与 UA
            proxies=self._lines_from_edit(self.proxies_edit),
            user_agents=self._lines_from_edit(self.user_agents_edit),
            rotate_user_agents=self.rotate_user_agents_check.isChecked(),
            # 规则与过滤
            respect_robots=self.respect_robots_check.isChecked(),
            compliance_check_enabled=self._compliance_check.isChecked(),
            allowed_domains=self._lines_from_edit(self.allowed_domains_edit),
            restrict_to_start_domains=self.restrict_to_start_domains_check.isChecked(),
            url_whitelist_regex=self._lines_from_edit(self.url_whitelist_edit),
            url_blacklist_regex=self._lines_from_edit(self.url_blacklist_edit),
            auto_follow_links=self.auto_follow_links_check.isChecked(),
            auto_detect_pagination=self.auto_detect_pagination_check.isChecked(),
            # 提取规则
            extraction_rules=self.rules_table.get_rules(),
            # 存储与输出
            storage_format=self.storage_format_combo.currentText(),
            output_path=self.output_path_edit.get_path(),
            persist_state_sqlite=self.persist_state_sqlite_check.isChecked(),
            # 文件下载
            download_files=self.download_files_check.isChecked(),
            download_url_regex=self.download_url_regex_edit.text(),
            download_dir=self.download_dir_edit.get_path(),
            download_ext_whitelist=self._lines_from_edit(self.download_ext_whitelist_edit),
            download_ext_blacklist=self._lines_from_edit(self.download_ext_blacklist_edit),
            download_str_whitelist=self._lines_from_edit(self.download_str_whitelist_edit),
            download_str_blacklist=self._lines_from_edit(self.download_str_blacklist_edit),
        )

    def set_config(self, config: CrawlConfig) -> None:
        """用给定的 :class:`CrawlConfig` 填充所有控件。"""
        # 起始配置
        self.start_urls_edit.setPlainText(
            "\n".join(config.start_urls or [])
        )
        self._set_combo_text(self.method_combo, config.method or "GET")
        self.post_data_table.set_items(config.post_data or {})

        # 抓取控制
        self.max_depth_spin.setValue(self._safe_int(config.max_depth, 3))
        self.max_pages_spin.setValue(self._safe_int(config.max_pages, 100))
        self.concurrency_spin.setValue(self._safe_int(config.concurrency, 5))
        self.request_interval_spin.setValue(
            self._safe_float(config.request_interval, 0.5)
        )
        self.timeout_spin.setValue(self._safe_float(config.timeout, 30.0))
        self.max_retries_spin.setValue(self._safe_int(config.max_retries, 3))
        self.max_redirects_spin.setValue(self._safe_int(config.max_redirects, 10))

        # 请求头与 Cookie
        self.headers_table.set_items(config.headers or {})
        self.cookies_table.set_items(config.cookies or {})

        # 代理与 UA
        self.proxies_edit.setPlainText("\n".join(config.proxies or []))
        self.user_agents_edit.setPlainText("\n".join(config.user_agents or []))
        self.rotate_user_agents_check.setChecked(bool(config.rotate_user_agents))

        # 规则与过滤
        self.respect_robots_check.setChecked(bool(config.respect_robots))
        self._compliance_check.setChecked(
            bool(getattr(config, "compliance_check_enabled", True))
        )
        self.allowed_domains_edit.setPlainText(
            "\n".join(config.allowed_domains or [])
        )
        self.restrict_to_start_domains_check.setChecked(
            bool(config.restrict_to_start_domains)
        )
        self.url_whitelist_edit.setPlainText(
            "\n".join(config.url_whitelist_regex or [])
        )
        self.url_blacklist_edit.setPlainText(
            "\n".join(config.url_blacklist_regex or [])
        )
        self.auto_follow_links_check.setChecked(bool(config.auto_follow_links))
        self.auto_detect_pagination_check.setChecked(
            bool(config.auto_detect_pagination)
        )

        # 提取规则
        self.rules_table.set_rules(config.extraction_rules or [])

        # 存储与输出
        self._set_combo_text(
            self.storage_format_combo, config.storage_format or "CSV"
        )
        self.output_path_edit.set_path(config.output_path or "output")
        self.persist_state_sqlite_check.setChecked(
            bool(config.persist_state_sqlite)
        )

        # 文件下载
        self.download_files_check.setChecked(bool(config.download_files))
        self.download_url_regex_edit.setText(config.download_url_regex or "")
        self.download_dir_edit.set_path(config.download_dir or "downloads")
        self.download_ext_whitelist_edit.setPlainText(
            "\n".join(config.download_ext_whitelist or [])
        )
        self.download_ext_blacklist_edit.setPlainText(
            "\n".join(config.download_ext_blacklist or [])
        )
        self.download_str_whitelist_edit.setPlainText(
            "\n".join(config.download_str_whitelist or [])
        )
        self.download_str_blacklist_edit.setPlainText(
            "\n".join(config.download_str_blacklist or [])
        )

    def load_from_file(self, path: str) -> None:
        """从 JSON 项目文件（.crawlproj）加载配置。"""
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        config = CrawlConfig.from_dict(data)
        self.set_config(config)

    def save_to_file(self, path: str) -> None:
        """将当前配置序列化为 JSON 写入指定路径。"""
        config = self.get_config()
        data = config.to_dict()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)

    def set_picker_locked(self, locked: bool) -> None:
        """爬取锁定时禁用「网页爬寻」相关控件，解锁后恢复。"""
        self._picker_btn.setEnabled(not locked)
        self._run_scskill_btn.setEnabled(not locked)
        self._browser_combo.setEnabled(not locked)
        self._scskill_combo.setEnabled(not locked)
        if hasattr(self, '_refresh_scskill_btn') and self._refresh_scskill_btn is not None:
            self._refresh_scskill_btn.setEnabled(not locked)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _on_open_picker(self) -> None:
        """「通过网页爬寻」按钮：读取首 URL 与浏览器类型并 emit 信号。"""
        urls = self._lines_from_edit(self.start_urls_edit)
        url = urls[0] if urls else ""
        browser_type = self._browser_combo.currentData() or "secretcrawler"
        self.open_picker_requested.emit(url, browser_type)

    def _build_section_picker_steps(self, container_layout) -> None:
        """构建「爬寻步骤」分区：.scskill 导入 + 浏览器选择 + 通过网页爬寻按钮。"""
        group = QGroupBox("爬寻步骤", self)
        group_layout = QVBoxLayout(group)

        # ---- 顶部：导入 .scskill ----
        scskill_row = QHBoxLayout()
        scskill_row.addWidget(QLabel("导入 .scskill:", self))
        self._scskill_combo = QComboBox(self)
        self._scskill_combo.setMinimumWidth(120)
        scskill_row.addWidget(self._scskill_combo, 1)
        self._refresh_scskill_btn = QPushButton("刷新", self)
        scskill_row.addWidget(self._refresh_scskill_btn)
        group_layout.addLayout(scskill_row)

        # ---- 按 .scskill 运行按钮 ----
        self._run_scskill_btn = QPushButton("按 .scskill 运行", self)
        group_layout.addWidget(self._run_scskill_btn)

        # ---- 占位提示 ----
        self._picker_placeholder = QLabel("请通过网页爬寻设计或导入 .scskill 脚本", self)
        self._picker_placeholder.setStyleSheet("color: #888; font-style: italic; padding: 4px;")
        self._picker_placeholder.setWordWrap(True)
        group_layout.addWidget(self._picker_placeholder)

        # ---- 浏览器选择 ----
        browser_row = QHBoxLayout()
        browser_row.addWidget(QLabel("浏览器:", self))
        self._browser_combo = QComboBox(self)
        self._browser_combo.addItem("SecretCrawler 浏览器", "secretcrawler")
        self._browser_combo.addItem("Google Chrome", "chrome")
        self._browser_combo.addItem("Microsoft Edge", "edge")
        # 默认选 SecretCrawler（index 0）
        self._browser_combo.setCurrentIndex(0)
        browser_row.addWidget(self._browser_combo, 1)
        group_layout.addLayout(browser_row)

        # ---- 通过网页爬寻按钮 ----
        self._picker_btn = QPushButton("通过网页爬寻", self)
        self._picker_btn.clicked.connect(self._on_open_picker)
        group_layout.addWidget(self._picker_btn)

        container_layout.addWidget(group)

        # ---- 信号连接 ----
        self._refresh_scskill_btn.clicked.connect(self._scan_scskill_files)
        self._run_scskill_btn.clicked.connect(self._on_run_scskill)

        # 初始扫描 skill/ 目录
        self._scan_scskill_files()

    def _scan_scskill_files(self) -> None:
        """扫描 skill/ 目录下所有 .scskill 后缀文件并填充下拉。

        支持三种后缀形式：
        - ``xxx.scskill``      （单后缀，PickerTask JSON）
        - ``xxx.py.scskill``   （Python 脚本）
        - ``xxx.js.scskill``   （JavaScript 脚本）

        下拉显示完整文件名但去掉末尾的 ``.scskill`` 后缀，
        例如 ``test`` / ``test.py`` / ``test.js``。
        """
        self._scskill_combo.clear()
        # skill/ 目录位于 main.py 同级（应用目录）
        try:
            from __main__ import __file__ as main_file  # type: ignore
            base = os.path.dirname(os.path.abspath(main_file))
        except Exception:  # noqa: BLE001
            base = os.getcwd()
        skill_dir = os.path.join(base, "skill")
        if not os.path.isdir(skill_dir):
            self._scskill_combo.addItem("（无脚本）", "")
            return
        # endswith(".scskill") 同时匹配 .scskill / .py.scskill / .js.scskill
        files = sorted([f for f in os.listdir(skill_dir) if f.endswith(".scskill")])
        if not files:
            self._scskill_combo.addItem("（无脚本）", "")
            return
        suffix = ".scskill"
        for f in files:
            # 显式去掉末尾的 .scskill 后缀，保留可能存在的 .py / .js 中间后缀
            name = f[:-len(suffix)]
            self._scskill_combo.addItem(name, name)

    def _on_run_scskill(self) -> None:
        """「按 .scskill 运行」按钮：emit run_scskill_requested 信号。"""
        name = self._scskill_combo.currentData()
        if not name:
            return
        browser_type = self._browser_combo.currentData() or "secretcrawler"
        self.run_scskill_requested.emit(name, browser_type)

    def _on_compliance_check_changed(self, state) -> None:
        """合规评估复选框状态变化处理。

        取消勾选时弹出确认对话框；用户放弃取消则恢复勾选。勾选时静默允许。
        """

        # state == 0 表示取消勾选（Qt.CheckState.Unchecked）
        if state == Qt.CheckState.Unchecked.value:
            ret = QMessageBox.warning(
                self,
                "确认取消合规评估",
                "取消合规评估可能导致违规爬取敏感数据，确认要取消吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                # 用户放弃取消：恢复勾选，屏蔽信号避免递归
                self._compliance_check.blockSignals(True)
                self._compliance_check.setChecked(True)
                self._compliance_check.blockSignals(False)
        # state == 2（Qt.CheckState.Checked）时静默允许

    @staticmethod
    def _lines_from_edit(edit: QPlainTextEdit) -> list[str]:
        """从多行文本控件中按行解析非空字符串列表。"""
        raw = edit.toPlainText() or ""
        result: list[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped:
                result.append(stripped)
        return result

    @staticmethod
    def _safe_int(value, default: int) -> int:
        """容错地将任意值转为 int；失败则返回默认值。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value, default: float) -> float:
        """容错地将任意值转为 float；失败则返回默认值。"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _set_combo_text(combo: QComboBox, text: str) -> None:
        """将 QComboBox 切换到与给定文本匹配的项；找不到则保持不变。"""
        if not text:
            return
        idx = combo.findText(text)
        if idx >= 0:
            combo.setCurrentIndex(idx)
