"""主窗口 —— 专业网络爬虫 GUI 的顶层窗口（Task 9 + 15）。

本模块组装菜单栏、工具栏、状态栏与中央区分屏布局，并把
:class:`crawler.engine.CrawlEngine` 的信号连接到各子视图的槽函数，
实现「开始 / 暂停 / 继续 / 停止」的完整控制流。

公共 API：
    MainWindow(QMainWindow) —— 应用主窗口
"""

from __future__ import annotations

import json
import logging
import os

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from crawler.engine import CrawlEngine
from crawler.models import CrawlConfig, PickerTask, ResultItem
from ui.config_panel import ConfigPanel
from ui.log_view import LogView
from ui.result_table import ResultTable
from ui.robots_view import RobotsView
from ui.scskill_editor import ScskillEditor
from ui.stats_view import StatsView


# 状态字符串 -> 状态栏中文显示
_STATE_LABELS: dict[str, str] = {
    "IDLE": "空闲",
    "RUNNING": "运行中",
    "PAUSED": "暂停",
    "STOPPING": "停止中",
    "STOPPED": "已停止",
}


class MainWindow(QMainWindow):
    """专业网络爬虫主窗口。

    布局：左侧配置面板（约 40%），右侧选项卡（结果 / 统计 / 日志，约 60%）；
    顶部菜单栏与工具栏提供「开始 / 暂停 / 停止 / 保存 / 加载 / 导出」；
    底部状态栏显示当前状态、当前 URL 与进度条。
    """

    # Task 4.2 + 9.7：警告触发信号，通知系统托盘弹出消息
    warning_triggered = Signal(str, str)
    # Task 4.4 + 9.8：托盘状态变化信号（active / idle / warning）
    tray_state_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowTitle("专业网络爬虫")
        self.resize(1280, 800)

        # 引擎实例（启动爬取时创建；停止后保留以供查询最终统计）
        self._engine: CrawlEngine | None = None
        # 标记日志 handler 是否已安装（仅在 __init__ 中安装一次）
        self._log_handler_installed: bool = False
        # 运行期警告记忆：key = 警告类型（如 "status:429" / "auth" / "tos"），
        # value = 用户选择的动作（"continue" / "skip" / "stop" 等）。仅本次运行有效，不持久化。
        self._warning_memory: dict[str, str] = {}
        # 爬取锁定：启动流程中（如用户协议展示期间）锁定，禁止开始爬取
        self._crawl_locked: bool = False

        # ---- 创建子视图 ----
        self.config_panel = ConfigPanel(self)
        self.result_table = ResultTable(self)
        self.stats_view = StatsView(self)
        self.log_view = LogView(self)
        self.robots_view = RobotsView(self)
        # Task 9.1：.scskill 编辑器子控件（在 _build_central 之前创建）
        self._scskill_editor = ScskillEditor(self)

        # ---- 构建 UI ----
        self._build_central()
        self._build_menu_bar()
        self._build_tool_bar()
        self._build_status_bar()

        # ---- 安装 Python logging handler（仅一次） ----
        self._install_log_handler()

        # ---- 初始按钮状态 ----
        self._update_action_states("IDLE")

        # 连接结果表格的状态提示信号到状态栏
        self.result_table.status_message.connect(self._on_status_message)

        # 连接配置面板的「通过网页爬寻」按钮信号：打开元素拾取器窗口
        self.config_panel.open_picker_requested.connect(self._on_open_picker)
        # Task 9.2：连接「按 .scskill 运行」按钮信号
        self.config_panel.run_scskill_requested.connect(self._on_run_scskill)
        # Task 9.5：连接编辑器「运行」按钮信号
        self._scskill_editor.run_requested.connect(self._on_run_scskill_from_editor)

    # ------------------------------------------------------------------ #
    # UI 构建
    # ------------------------------------------------------------------ #

    def _build_central(self) -> None:
        """构建中央区：水平 QSplitter，左配置 / 右选项卡。"""
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # 左侧：配置面板（自身已带 QScrollArea）
        splitter.addWidget(self.config_panel)

        # 右侧：选项卡
        tabs = QTabWidget(self)
        tabs.addTab(self.result_table, "结果")
        tabs.addTab(self.stats_view, "统计")
        tabs.addTab(self.log_view, "日志")
        tabs.addTab(self.robots_view, "robots.txt")
        # Task 9.1：新增 .scskill 编辑器标签页
        tabs.addTab(self._scskill_editor, ".scskill 编辑器")
        splitter.addWidget(tabs)

        # 初始比例约 40% : 60%
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)
        # 设置最小尺寸避免完全折叠
        self.config_panel.setMinimumWidth(320)
        tabs.setMinimumWidth(420)

        self.setCentralWidget(splitter)

    def _build_menu_bar(self) -> None:
        """构建菜单栏：文件 / 抓取 / 帮助。"""
        menubar = self.menuBar()

        # ---- 文件菜单 ----
        file_menu = menubar.addMenu("文件")

        self._act_new_project = QAction("新建项目", self)
        self._act_new_project.setShortcut(QKeySequence.New)
        self._act_new_project.triggered.connect(self._on_new_project)
        file_menu.addAction(self._act_new_project)

        self._act_open_project = QAction("打开项目…", self)
        self._act_open_project.setShortcut(QKeySequence.Open)
        self._act_open_project.triggered.connect(self._on_load_project)
        file_menu.addAction(self._act_open_project)

        self._act_save_project = QAction("保存项目…", self)
        self._act_save_project.setShortcut(QKeySequence.Save)
        self._act_save_project.triggered.connect(self._on_save_project)
        file_menu.addAction(self._act_save_project)

        file_menu.addSeparator()

        self._act_exit = QAction("退出", self)
        self._act_exit.setShortcut(QKeySequence.Quit)
        self._act_exit.triggered.connect(self.close)
        file_menu.addAction(self._act_exit)

        # ---- 抓取菜单 ----
        crawl_menu = menubar.addMenu("抓取")

        self._act_start = QAction("开始", self)
        self._act_start.triggered.connect(self._on_start)
        crawl_menu.addAction(self._act_start)

        self._act_pause = QAction("暂停", self)
        self._act_pause.triggered.connect(self._on_pause_toggle)
        crawl_menu.addAction(self._act_pause)

        self._act_stop = QAction("停止", self)
        self._act_stop.triggered.connect(self._on_stop)
        crawl_menu.addAction(self._act_stop)

        # ---- 帮助菜单 ----
        help_menu = menubar.addMenu("帮助")

        self._act_help_usage = QAction("如何使用", self)
        self._act_help_usage.triggered.connect(self._on_help_usage)
        help_menu.addAction(self._act_help_usage)

        # ---- 关于软件菜单 ----
        about_menu = menubar.addMenu("关于软件")

        self._act_about_software = QAction("关于软件", self)
        self._act_about_software.triggered.connect(self._on_about_software)
        about_menu.addAction(self._act_about_software)

    def _build_tool_bar(self) -> None:
        """构建工具栏：开始 / 暂停 / 停止 / 保存 / 加载 / 导出。"""
        toolbar = QToolBar("主工具栏", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        # 复用菜单中已创建的 QAction，使菜单与工具栏联动
        toolbar.addAction(self._act_start)
        toolbar.addAction(self._act_pause)
        toolbar.addAction(self._act_stop)
        toolbar.addSeparator()
        toolbar.addAction(self._act_save_project)
        toolbar.addAction(self._act_open_project)

        # 导出结果（独立 QAction）
        self._act_export = QAction("导出结果", self)
        self._act_export.triggered.connect(self._on_export_results)
        toolbar.addAction(self._act_export)

    def _build_status_bar(self) -> None:
        """构建状态栏：状态标签 / 当前 URL 标签 / 进度条。"""
        bar = self.statusBar()

        # 状态标签（左侧，永久部件）
        self._state_label = QLabel("空闲")
        self._state_label.setMinimumWidth(80)
        bar.addPermanentWidget(self._state_label)

        # 当前 URL 标签（中间，永久部件，会随抓取更新）
        self._url_label = QLabel("")
        self._url_label.setMinimumWidth(200)
        self._url_label.setStyleSheet("color: #555;")
        bar.addPermanentWidget(self._url_label)

        # 进度条（右侧，永久部件）
        self._progress = QProgressBar()
        self._progress.setFixedWidth(220)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        bar.addPermanentWidget(self._progress)

    # ------------------------------------------------------------------ #
    # 日志 handler
    # ------------------------------------------------------------------ #

    def _install_log_handler(self) -> None:
        """把 LogView 的 logging handler 安装到根 logger（仅一次）。"""
        if self._log_handler_installed:
            return
        try:
            handler = self.log_view.make_handler()
            # 设置一个合理的最低级别，避免 DEBUG 刷屏
            handler.setLevel(logging.DEBUG)
            root_logger = logging.getLogger()
            root_logger.addHandler(handler)
            # 保证根 logger 级别不会过滤掉 INFO
            if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
                root_logger.setLevel(logging.INFO)
            self._log_handler_installed = True
        except Exception as exc:  # noqa: BLE001 - handler 安装失败不应阻断 UI
            logging.getLogger("ui.main_window").warning(
                "安装日志 handler 失败: %s", exc
            )

    # ------------------------------------------------------------------ #
    # 槽：动作处理
    # ------------------------------------------------------------------ #

    def _on_start(self) -> None:
        """「开始」按钮：读取配置、校验、创建引擎并启动。"""
        if self._crawl_locked:
            return
        try:
            config = self.config_panel.get_config()

            # 校验：至少一个起始 URL
            if not config.start_urls:
                QMessageBox.warning(
                    self,
                    "无法开始",
                    "请至少配置一个起始 URL。",
                )
                return

            # 若已有引擎在运行，先停止
            if self._engine is not None and self._engine.is_running:
                self._engine.stop()
                # 注意：这里不等待 finished，直接新建引擎；
                # 旧引擎的轮询定时器会在完成后自行停止并 emit finished。

            # 清空结果与统计
            self.result_table.clear()
            self.result_table.clear_columns()
            self.stats_view.reset()
            self._progress.setValue(0)
            self._url_label.setText("")

            # 创建新引擎
            self._engine = CrawlEngine(config)

            # 连接信号
            self._engine.log_message.connect(self.log_view.append_log)
            self._engine.result_found.connect(self.result_table.add_result)
            self._engine.stats_updated.connect(self.stats_view.update_stats)
            self._engine.stats_updated.connect(self._on_stats_updated)
            self._engine.page_crawled.connect(self._on_page_crawled)
            self._engine.state_changed.connect(self._on_state_changed)
            self._engine.finished.connect(self._on_finished)
            self._engine.error.connect(self._on_error)
            # Task 7 / 9 / 11 / 15：合规与探测信号
            self._engine.status_warning.connect(self._on_status_warning)
            self._engine.api_detected.connect(self._on_api_detected)
            self._engine.auth_required.connect(self._on_auth_required)
            self._engine.compliance_report.connect(self._on_compliance_report)
            self._engine.tos_warning.connect(self._on_tos_warning)

            # 刷新 robots.txt 视图（在启动前，便于用户预览）
            self.robots_view.refresh(config.start_urls)

            # 启动
            self._engine.start()

            # 立即刷新按钮状态（start 内部会 emit state_changed，但这里兜底）
            self._update_action_states(self._engine.state)
        except Exception as exc:  # noqa: BLE001 - 启动异常不应崩溃
            logging.getLogger("ui.main_window").exception("启动爬取异常")
            QMessageBox.critical(
                self,
                "启动失败",
                f"启动爬取时发生错误：\n{exc}",
            )

    def _on_pause_toggle(self) -> None:
        """「暂停 / 继续」切换按钮。

        根据当前引擎状态决定调用 pause() 还是 resume()，并同步按钮文本。
        """
        try:
            engine = self._engine
            if engine is None:
                return
            if engine.state == "PAUSED":
                engine.resume()
                self._act_pause.setText("暂停")
            else:
                engine.pause()
                self._act_pause.setText("继续")
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("ui.main_window").exception("暂停/继续异常")
            QMessageBox.critical(
                self,
                "操作失败",
                f"暂停/继续时发生错误：\n{exc}",
            )

    def _on_stop(self) -> None:
        """「停止」按钮：请求引擎优雅停止。"""
        try:
            engine = self._engine
            if engine is None or not engine.is_running:
                return
            engine.stop()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("ui.main_window").exception("停止异常")
            QMessageBox.critical(
                self,
                "操作失败",
                f"停止时发生错误：\n{exc}",
            )

    def _on_save_project(self) -> None:
        """「保存项目」：弹出文件对话框并保存配置。"""
        try:
            path, _ = QFileDialog.getSaveFileName(
                self,
                "保存爬虫项目",
                "crawler.crawlproj",
                "爬虫项目 (*.crawlproj);;所有文件 (*.*)",
            )
            if not path:
                return
            self.config_panel.save_to_file(path)
            self.statusBar().showMessage(f"已保存项目：{path}", 3000)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("ui.main_window").exception("保存项目异常")
            QMessageBox.critical(
                self,
                "保存失败",
                f"保存项目时发生错误：\n{exc}",
            )

    def _on_load_project(self) -> None:
        """「加载项目」：弹出文件对话框并加载配置。"""
        try:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "加载爬虫项目",
                "",
                "爬虫项目 (*.crawlproj);;JSON 文件 (*.json);;所有文件 (*.*)",
            )
            if not path:
                return
            self.config_panel.load_from_file(path)
            self.statusBar().showMessage(f"已加载项目：{path}", 3000)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("ui.main_window").exception("加载项目异常")
            QMessageBox.critical(
                self,
                "加载失败",
                f"加载项目时发生错误：\n{exc}",
            )

    def _on_export_results(self) -> None:
        """「导出结果」：弹出文件对话框并导出 CSV。"""
        try:
            path, _ = QFileDialog.getSaveFileName(
                self,
                "导出结果为 CSV",
                "results.csv",
                "CSV 文件 (*.csv);;所有文件 (*.*)",
            )
            if not path:
                return
            self.result_table.export_csv(path)
            self.statusBar().showMessage(f"已导出结果：{path}", 3000)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("ui.main_window").exception("导出结果异常")
            QMessageBox.critical(
                self,
                "导出失败",
                f"导出结果时发生错误：\n{exc}",
            )

    def _on_new_project(self) -> None:
        """「新建项目」：用默认配置重置配置面板。"""
        try:
            self.config_panel.set_config(CrawlConfig())
            self.statusBar().showMessage("已新建项目（恢复默认配置）", 3000)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("ui.main_window").exception("新建项目异常")
            QMessageBox.critical(
                self,
                "操作失败",
                f"新建项目时发生错误：\n{exc}",
            )

    def _on_help_usage(self) -> None:
        """「如何使用」：打开使用说明窗口（非模态）。"""
        from ui.help_window import HelpWindow

        # 保留引用避免被 GC
        self._help_window = HelpWindow(self)
        self._help_window.show()

    def _on_about_software(self) -> None:
        """打开关于软件窗口。"""
        from ui.about_window import AboutWindow

        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dialog = AboutWindow(app_dir, self)
        dialog.exec()

    def _on_open_picker(self, url: str, browser_type: str = "secretcrawler") -> None:
        """「通过网页爬寻」按钮槽：打开元素拾取器窗口并加载起始 URL。

        局部导入 ``QtWebEngineWidgets`` 相关模块，避免在主窗口模块加载
        阶段就初始化 QtWebEngine 进程。窗口引用保留在 ``self._picker_window``
        上以防被 Python GC 回收。
        """
        from ui.element_picker_window import ElementPickerWindow

        self._picker_window = ElementPickerWindow(url, self, browser_type=browser_type)
        self._picker_window.data_collected.connect(self._on_picker_data_collected)
        self._picker_window.show()

    def _on_run_scskill(self, scskill_name: str, browser_type: str) -> None:
        """「按 .scskill 运行」：根据后缀分流执行。

        scskill_name 是去掉 .scskill 后缀的显示名（如 'test' / 'test.py' / 'test.js'），
        实际文件名 = scskill_name + '.scskill'。
        """
        # scskill_name 是去掉 .scskill 后缀的显示名（如 'test' / 'test.py' / 'test.js'）
        # 实际文件名 = scskill_name + '.scskill'
        file_name = scskill_name + '.scskill'
        skill_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill")
        if not os.path.isdir(skill_dir):
            skill_dir = "skill"
        file_path = os.path.join(skill_dir, file_name)

        if not os.path.isfile(file_path):
            QMessageBox.warning(self, "文件不存在", f"找不到 scskill 文件: {file_path}")
            return

        # 根据后缀分流
        if file_name.endswith('.py.scskill'):
            self._run_python_scskill(file_path)
        elif file_name.endswith('.js.scskill'):
            self._run_javascript_scskill(file_path, browser_type)
        else:
            # .scskill — PickerTask JSON
            self._run_pickertask_scskill(file_path, browser_type)

    def _run_python_scskill(self, file_path: str) -> None:
        """执行 .py.scskill Python 脚本。"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            import io, contextlib
            import requests, bs4
            from crawler import models as crawler_models
            restricted_globals = {
                '__builtins__': __builtins__,
                'requests': requests,
                'bs4': bs4,
                'json': json,
                'os': os,
                'crawler': crawler_models,
            }
            output_buf = io.StringIO()
            with contextlib.redirect_stdout(output_buf), contextlib.redirect_stderr(output_buf):
                exec(content, restricted_globals)
            output = output_buf.getvalue() or "(无输出)"
            logging.getLogger("ui.main_window").info("Python scskill 执行完成:\n%s", output)
            self.statusBar().showMessage(f"Python 脚本执行完成", 5000)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("ui.main_window").exception("Python scskill 执行异常")
            QMessageBox.critical(self, "执行失败", f"Python 脚本执行异常:\n{exc}")

    def _run_javascript_scskill(self, file_path: str, browser_type: str) -> None:
        """执行 .js.scskill JavaScript 脚本：通过拾取器 view 的 runJavaScript。"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # 复用 _on_open_picker 的逻辑创建拾取器窗口
            from ui.element_picker_window import ElementPickerWindow
            # 从 config_panel 取第一个起始 URL
            config = self.config_panel.get_config()
            url = config.start_urls[0] if config.start_urls else "about:blank"
            self._picker_window = ElementPickerWindow(url, self, browser_type=browser_type)
            self._picker_window.show()
            # _view 延迟初始化（showEvent 触发 _init_web_view），需等待
            def _inject_js() -> None:
                if self._picker_window is not None and self._picker_window._view is not None:
                    try:
                        self._picker_window._view.page().runJavaScript(content)
                        self.statusBar().showMessage("JS 脚本已注入执行", 5000)
                    except Exception as exc:  # noqa: BLE001
                        logging.getLogger("ui.main_window").warning("JS 注入失败: %s", exc)
                else:
                    QTimer.singleShot(500, _inject_js)
            QTimer.singleShot(1000, _inject_js)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("ui.main_window").exception("JS scskill 执行异常")
            QMessageBox.critical(self, "执行失败", f"JS 脚本执行异常:\n{exc}")

    def _run_pickertask_scskill(self, file_path: str, browser_type: str) -> None:
        """执行 .scskill (PickerTask JSON) 脚本。"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            task = PickerTask.from_dict(data)
            # 读取起始 URL
            config = self.config_panel.get_config()
            url = config.start_urls[0] if config.start_urls else ""
            # 创建拾取器并执行
            from ui.element_picker_window import ElementPickerWindow
            self._picker_window = ElementPickerWindow(url, self, browser_type=browser_type)
            self._picker_window.data_collected.connect(self._on_picker_data_collected)
            self._picker_window.show()
            self._picker_window.run_task(task)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("ui.main_window").exception("PickerTask scskill 执行异常")
            QMessageBox.critical(self, "执行失败", f"PickerTask 脚本执行异常:\n{exc}")

    def _on_run_scskill_from_editor(self, path: str, script_type: str) -> None:
        """从编辑器运行 scskill：根据 script_type 分流。"""
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "无法运行", "请先保存文件再运行")
            return

        if script_type == 'python':
            self._run_python_scskill(path)
        elif script_type == 'javascript':
            # 从 config_panel 取浏览器类型
            browser_type = 'secretcrawler'  # 默认
            try:
                browser_type = self.config_panel._browser_combo.currentData() or 'secretcrawler'
            except Exception:  # noqa: BLE001
                pass
            self._run_javascript_scskill(path, browser_type)
        else:  # 'pickertask'
            try:
                browser_type = self.config_panel._browser_combo.currentData() or 'secretcrawler'
            except Exception:  # noqa: BLE001
                browser_type = 'secretcrawler'
            self._run_pickertask_scskill(path, browser_type)

    def _on_picker_data_collected(self, data: dict) -> None:
        """拾取器采集到数据时回调：写入结果表格。"""
        try:
            self.result_table.add_result(
                ResultItem(
                    url="",
                    fields={data.get("field", ""): data.get("value", "")},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("ui.main_window").warning(
                "写入拾取器采集数据失败: %s", exc
            )

    # ------------------------------------------------------------------ #
    # 槽：引擎信号
    # ------------------------------------------------------------------ #

    def _on_page_crawled(self, url: str, status: int) -> None:
        """单页抓取完成：更新当前 URL 标签。"""
        # 截断过长 URL 以免状态栏被撑爆
        display = url if len(url) <= 80 else url[:77] + "…"
        self._url_label.setText(f"{display} [{status}]")

    def _on_stats_updated(self, stats) -> None:
        """统计更新：刷新进度条。

        progress = pages_crawled / (pages_crawled + queue_size)
        """
        try:
            pages_crawled = int(getattr(stats, "pages_crawled", 0) or 0)
            queue_size = int(getattr(stats, "queue_size", 0) or 0)
            denom = pages_crawled + queue_size
            if denom > 0:
                progress = pages_crawled / denom
            else:
                progress = 0.0
            self._progress.setValue(int(progress * 100))
        except Exception:  # noqa: BLE001 - 统计刷新失败不应影响 UI
            pass

    def _on_state_changed(self, state: str) -> None:
        """引擎状态变化：更新状态标签与按钮启用状态。"""
        self._update_action_states(state)
        label = _STATE_LABELS.get(state, state)
        self._state_label.setText(label)
        # 进入暂停时把按钮文本改为「继续」；恢复运行时改回「暂停」
        if state == "PAUSED":
            self._act_pause.setText("继续")
        elif state == "RUNNING":
            self._act_pause.setText("暂停")
        # 停止完成时清空当前 URL
        if state == "STOPPED":
            self._url_label.setText("")
        # Task 4.5：同步托盘图标状态
        if state == "RUNNING":
            self.set_tray_active(True)
        elif state == "PAUSED":
            self.set_tray_warning(True)
        else:  # IDLE / STOPPED / STOPPING
            self.set_tray_active(False)

    def _on_finished(self) -> None:
        """爬取完成：刷新按钮状态并在状态栏提示。"""
        self._update_action_states("STOPPED")
        self._state_label.setText("完成")
        self.statusBar().showMessage("爬取完成", 5000)
        # 进度条置满
        self._progress.setValue(100)

    def _on_error(self, message: str) -> None:
        """致命错误：弹窗 + 记录日志。"""
        logging.getLogger("ui.main_window").error("引擎致命错误: %s", message)
        QMessageBox.critical(
            self,
            "爬虫错误",
            f"爬虫发生致命错误：\n{message}",
        )

    # ------------------------------------------------------------------ #
    # 槽：Task 7 / 9 / 11 / 15 合规与探测信号
    # ------------------------------------------------------------------ #

    def _on_status_warning(self, payload: dict) -> None:
        """Task 7：状态码警告——询问用户继续 / 跳过该 URL / 终止爬取。"""
        # Task 4.1：警告时先暂停任务，避免继续触发更多警告
        try:
            if self._engine is not None and self._engine.is_running:
                self._engine.pause()
                self._act_pause.setText("继续")
        except Exception:  # noqa: BLE001
            pass
        # Task 4.2 + 9.7：通知系统托盘弹出消息
        status_code = payload.get("status_code", payload.get("status", "未知"))
        description = payload.get("description", payload.get("message", ""))
        self.warning_triggered.emit("爬虫警告", f"状态码 {status_code}：{description}")
        try:
            status = payload.get("status")
            warning_key = f"status:{status}"
            action = self._show_warning_dialog(
                "状态码警告",
                f"状态码 {status}\n"
                f"URL: {payload.get('url', '')}\n"
                f"{payload.get('message', '')}",
                warning_key,
                [("继续", "continue"), ("跳过该 URL", "skip"), ("终止爬取", "stop")],
            )
            if action == "stop":
                if self._engine:
                    self._engine.stop()
            else:
                # continue / skip / None(关闭)：恢复调度器，避免引擎长期挂起
                if self._engine:
                    self._engine.resume()
        except Exception:  # noqa: BLE001 - 弹窗失败不应崩溃
            pass

    def _on_api_detected(self, payload: dict) -> None:
        """Task 9：探测到官方 API——询问用户继续网页爬取 / 改用 API / 取消。"""
        try:
            evidence = "\n".join(payload.get("evidence", [])) or "(无)"
            endpoints = "\n".join(payload.get("suggested_endpoints", [])) or "(无)"
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("检测到官方 API")
            box.setText(
                f"基准地址: {payload.get('base_url', '')}\n"
                f"指标:\n{evidence}\n"
                f"建议端点:\n{endpoints}"
            )
            box.addButton("继续网页爬取", QMessageBox.ButtonRole.AcceptRole)
            btn_api = box.addButton("改用官方 API", QMessageBox.ButtonRole.RejectRole)
            btn_cancel = box.addButton("取消", QMessageBox.ButtonRole.DestructiveRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is btn_api:
                if self._engine:
                    self._engine.pause()
                QMessageBox.information(
                    self,
                    "改用 API",
                    "请在配置面板填写 API 端点后重新开始。",
                )
            elif clicked is btn_cancel:
                if self._engine:
                    self._engine.stop()
            # 继续网页爬取：不做任何操作
        except Exception:  # noqa: BLE001
            pass

    def _on_auth_required(self, payload: dict) -> None:
        """Task 11：站点可能需要书面认证——询问用户继续 / 获取认证文件 / 终止。"""
        try:
            action = self._show_warning_dialog(
                "需要认证",
                f"URL: {payload.get('url', '')}\n{payload.get('reason', '')}",
                "auth",
                [("继续", "continue"), ("获取书面认证文件", "get_auth"), ("终止", "stop")],
            )
            if action == "continue":
                if self._engine:
                    self._engine.resume()
            elif action == "get_auth":
                if self._engine:
                    self._engine.pause()
                QMessageBox.information(
                    self,
                    "获取认证",
                    "请在配置面板填写认证信息(Cookie/Authorization)后继续",
                )
            elif action == "stop":
                if self._engine:
                    self._engine.stop()
            else:
                # 关闭对话框未选择：默认恢复，避免引擎长期挂起
                if self._engine:
                    self._engine.resume()
        except Exception:  # noqa: BLE001
            pass

    def _on_compliance_report(self, payload: dict) -> None:
        """Task 15：合规评估结果——更新状态栏，敏感命中时记录日志。"""
        try:
            sensitive_hits = payload.get("sensitive_hits", []) or []
            tos_prohibits = payload.get("tos_prohibits", False)
            self.statusBar().showMessage(
                f"合规评估: 敏感命中 {len(sensitive_hits)} 项, "
                f"ToS禁止={tos_prohibits}",
                5000,
            )
            if sensitive_hits:
                logging.getLogger("ui.main_window").warning(
                    "合规评估敏感命中 %d 项: %s",
                    len(sensitive_hits),
                    ", ".join(sensitive_hits),
                )
        except Exception:  # noqa: BLE001
            pass

    def _on_tos_warning(self, payload: dict) -> None:
        """Task 15：用户协议可能禁止爬取——询问用户是否继续。"""
        try:
            evidence = payload.get("evidence", []) or []
            action = self._show_warning_dialog(
                "用户协议警告",
                f"该站点用户协议可能禁止爬取：\n{', '.join(evidence)}",
                "tos",
                [("继续", "continue"), ("终止", "stop")],
            )
            if action == "stop":
                if self._engine:
                    self._engine.stop()
            # continue / None(关闭)：继续爬取，不做任何操作
        except Exception:  # noqa: BLE001
            pass

    def _on_status_message(self, message: str) -> None:
        """结果表格的状态提示：转发到状态栏临时显示。"""
        self.statusBar().showMessage(message, 3000)

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _show_warning_dialog(
        self,
        title: str,
        text: str,
        warning_key: str,
        buttons: list[tuple[str, str]],
    ) -> str | None:
        """显示带「本次运行不再弹出相同警告」复选框的模态警告对话框。

        若 ``warning_key`` 已在 ``_warning_memory`` 中，则直接返回记忆的动作，
        不再弹窗。否则构造自定义 QDialog，根据用户点击的按钮返回对应动作；
        若勾选了复选框，则把动作记入运行期记忆。

        Args:
            title: 对话框窗口标题。
            text: 对话框正文。
            warning_key: 警告类型键（如 ``"status:429"`` / ``"auth"`` / ``"tos"``）。
            buttons: ``(按钮文本, 动作值)`` 元组列表，按顺序排列。

        Returns:
            用户选择的动作值；若用户关闭对话框未做选择，返回 ``None``。
        """
        # 已记忆：直接返回，不再弹窗
        memorized = self._warning_memory.get(warning_key)
        if memorized is not None:
            return memorized

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setModal(True)

        label = QLabel(text, dialog)
        label.setWordWrap(True)

        checkbox = QCheckBox("本次运行不再弹出相同警告", dialog)

        # 按钮行
        btn_layout = QHBoxLayout()
        button_widgets: list[tuple[QPushButton, str]] = []
        for btn_text, action_value in buttons:
            btn = QPushButton(btn_text, dialog)
            btn_layout.addWidget(btn)
            button_widgets.append((btn, action_value))

        main_layout = QVBoxLayout(dialog)
        main_layout.addWidget(label)
        main_layout.addWidget(checkbox)
        main_layout.addLayout(btn_layout)

        # 用闭包捕获用户点击的动作
        clicked: list[str | None] = [None]

        def _make_handler(action_value: str):
            def _handler() -> None:
                clicked[0] = action_value
                dialog.accept()

            return _handler

        for btn, action_value in button_widgets:
            btn.clicked.connect(_make_handler(action_value))

        dialog.exec()

        action = clicked[0]
        # 若用户做出了选择且勾选了复选框，则记入运行期记忆
        if action is not None and checkbox.isChecked():
            self._warning_memory[warning_key] = action
        return action

    def set_crawl_locked(self, locked: bool) -> None:
        """锁定或解锁「开始」动作。

        锁定期间禁用菜单与工具栏中的「开始」按钮，避免在启动流程
        （如用户协议展示）完成前触发爬取。工具栏复用同一个 QAction，
        因此禁用动作即同时禁用菜单项与工具栏按钮。同时同步锁定
        ConfigPanel 中「网页爬寻」相关控件。
        """
        self._crawl_locked = locked
        self._act_start.setEnabled(not locked)
        self.config_panel.set_picker_locked(locked)

    def _update_action_states(self, state: str) -> None:
        """根据引擎状态刷新各 QAction 的启用状态。

        - 开始：仅非运行态可用
        - 暂停/继续：仅运行态或暂停态可用
        - 停止：仅运行态或暂停态可用
        """
        running = state in ("RUNNING", "PAUSED", "STOPPING")
        self._act_start.setEnabled(not running)
        self._act_pause.setEnabled(state in ("RUNNING", "PAUSED"))
        self._act_stop.setEnabled(running)

    # ------------------------------------------------------------------ #
    # 重写：关闭事件
    # ------------------------------------------------------------------ #

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """关闭窗口时隐藏到系统托盘，而非退出应用。

        任务继续在后台运行；用户可通过托盘菜单「退出」真正退出应用。
        """
        # 隐藏到托盘而非退出
        self.hide()
        event.ignore()

    # ------------------------------------------------------------------ #
    # 托盘状态同步（Task 4.4 + 9.8）
    # ------------------------------------------------------------------ #

    def set_tray_active(self, active: bool) -> None:
        """设置托盘「活跃」状态（任务运行中）。"""
        if active:
            self.tray_state_changed.emit("active")
        else:
            self.tray_state_changed.emit("idle")

    def set_tray_warning(self, warning: bool) -> None:
        """设置托盘「警告」状态。"""
        if warning:
            self.tray_state_changed.emit("warning")
        else:
            self.tray_state_changed.emit("idle")
