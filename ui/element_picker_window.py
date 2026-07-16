"""可视化元素拾取器窗口。

本模块提供 EasySpider 风格的内嵌浏览器 ``ElementPickerWindow``，支持：
    - 顶部地址栏（QLineEdit + 「前往」按钮）加载任意 URL
    - 左侧工具栏：启用 / 关闭拾取模式、切换元素库面板、验证动作、关闭窗口
    - 中央 QWebEngineView 渲染目标网页
    - 右侧 QDockWidget 元素库面板（QListWidget + 删除 / 清空）

拾取机制：
    - 启用「拾取模式」后向页面注入 JS，监听 ``mouseover`` / ``mouseout`` / ``click``。
    - 仅当用户按住 ``Ctrl`` 时高亮元素 / 拦截点击，避免影响普通浏览。
    - JS 将拾取结果（description / xpath / css_selector）写入 ``window.__ic_last_picked``。
    - Python 侧以 200ms ``QTimer`` 轮询读取并追加到元素库。

类定义:
    ElementPickerWindow - 元素拾取器主窗口（QMainWindow）
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any

from PySide6.QtCore import Qt, QUrl, QTimer, Signal
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from crawler.models import ElementInfo, PickerAction, PickerTask


# 轮询拾取结果的间隔（毫秒）
_PICK_POLL_INTERVAL_MS = 200


# 注入页面的拾取 JS：仅当 Ctrl 按下时高亮 / 拦截点击；结果写入 window.__ic_last_picked。
_PICK_JS = r"""
(function() {
  if (window.__ic_picking_active) return;
  window.__ic_picking_active = true;
  window.__ic_highlighted = null;

  function generateXPath(element) {
    if (element.id) return '//*[@id="' + element.id + '"]';
    var parts = [];
    while (element && element.nodeType === 1) {
      var tag = element.tagName.toLowerCase();
      var parent = element.parentNode;
      if (parent) {
        var siblings = Array.prototype.filter.call(parent.children, function(c) { return c.tagName === element.tagName; });
        if (siblings.length > 1) {
          var index = siblings.indexOf(element) + 1;
          parts.unshift(tag + '[' + index + ']');
        } else {
          parts.unshift(tag);
        }
      } else {
        parts.unshift(tag);
      }
      element = parent;
    }
    return '/' + parts.join('/');
  }

  function generateCssSelector(element) {
    if (element.id) return '#' + element.id;
    var tag = element.tagName.toLowerCase();
    var cls = element.className;
    if (cls) {
      var classes = cls.split(/\s+/).filter(Boolean).join('.');
      var selector = tag + '.' + classes;
      var parent = element.parentNode;
      if (parent) {
        var siblings = Array.prototype.filter.call(parent.children, function(c) { return c.tagName === element.tagName; });
        if (siblings.length > 1) {
          var index = siblings.indexOf(element) + 1;
          selector += ':nth-of-type(' + index + ')';
        }
      }
      return selector;
    }
    return tag;
  }

  function generateDescription(element) {
    var tag = element.tagName.toLowerCase();
    var desc = tag;
    if (element.id) desc += '#' + element.id;
    if (element.className) desc += '.' + element.className.split(/\s+/).filter(Boolean).join('.');
    return desc;
  }

  window.__ic_mouseover = function(e) {
    if (window.__ic_picked_set && window.__ic_picked_set.has(e.target)) return;
    if (!e.ctrlKey) return;
    if (window.__ic_highlighted && window.__ic_highlighted !== e.target) {
      window.__ic_highlighted.style.outline = '';
    }
    e.target.style.outline = '2px solid orange';
    e.target.style.outlineOffset = '-2px';
    window.__ic_highlighted = e.target;
  };

  window.__ic_mouseout = function(e) {
    if (window.__ic_picked_set && window.__ic_picked_set.has(e.target)) return;
    if (!e.ctrlKey) return;
    if (window.__ic_highlighted === e.target) {
      e.target.style.outline = '';
      window.__ic_highlighted = null;
    }
  };

  window.__ic_click = function(e) {
    if (!e.ctrlKey) return;
    e.preventDefault();
    e.stopPropagation();
    if (window.__ic_last_picked) return;  // 上次结果尚未被 Python 读取，跳过避免覆盖
    var xpath = generateXPath(e.target);
    var css = generateCssSelector(e.target);
    var desc = generateDescription(e.target);
    e.target.style.outline = '2px dashed green';
    e.target.style.outlineOffset = '-2px';
    if (!window.__ic_picked_set) window.__ic_picked_set = new Set();
    window.__ic_picked_set.add(e.target);
    window.__ic_last_picked = JSON.stringify({description: desc, xpath: xpath, css_selector: css});
  };

  window.__ic_blur = function(e) {
    e.stopPropagation();
  };
  document.addEventListener('blur', window.__ic_blur, true);
  window.addEventListener('blur', window.__ic_blur, true);

  document.addEventListener('mouseover', window.__ic_mouseover, true);
  document.addEventListener('mouseout', window.__ic_mouseout, true);
  document.addEventListener('click', window.__ic_click, true);
})();
"""

# 卸载拾取 JS：移除监听（保留已拾取元素的绿色持久标记）。
_UNPICK_JS = r"""
(function() {
  if (window.__ic_mouseover) document.removeEventListener('mouseover', window.__ic_mouseover, true);
  if (window.__ic_mouseout) document.removeEventListener('mouseout', window.__ic_mouseout, true);
  if (window.__ic_click) document.removeEventListener('click', window.__ic_click, true);
  if (window.__ic_blur) {
    document.removeEventListener('blur', window.__ic_blur, true);
    window.removeEventListener('blur', window.__ic_blur, true);
  }
  window.__ic_picking_active = false;
})();
"""

# 右键上下文菜单 JS：捕获 contextmenu 事件并写入 window.__ic_last_right_clicked。
# 始终在 view 创建后注入（不依赖拾取模式），供 Python 侧轮询读取。
# 复用 _PICK_JS 的 description/xpath/css 生成算法。
_CONTEXT_JS = r"""
(function() {
  if (window.__ic_context_active) return;
  window.__ic_context_active = true;

  function generateXPath(element) {
    if (element.id) return '//*[@id="' + element.id + '"]';
    var parts = [];
    while (element && element.nodeType === 1) {
      var tag = element.tagName.toLowerCase();
      var parent = element.parentNode;
      if (parent) {
        var siblings = Array.prototype.filter.call(parent.children, function(c) { return c.tagName === element.tagName; });
        if (siblings.length > 1) {
          var index = siblings.indexOf(element) + 1;
          parts.unshift(tag + '[' + index + ']');
        } else {
          parts.unshift(tag);
        }
      } else {
        parts.unshift(tag);
      }
      element = parent;
    }
    return '/' + parts.join('/');
  }

  function generateCssSelector(element) {
    if (element.id) return '#' + element.id;
    var tag = element.tagName.toLowerCase();
    var cls = element.className;
    if (cls) {
      var classes = cls.split(/\s+/).filter(Boolean).join('.');
      var selector = tag + '.' + classes;
      var parent = element.parentNode;
      if (parent) {
        var siblings = Array.prototype.filter.call(parent.children, function(c) { return c.tagName === element.tagName; });
        if (siblings.length > 1) {
          var index = siblings.indexOf(element) + 1;
          selector += ':nth-of-type(' + index + ')';
        }
      }
      return selector;
    }
    return tag;
  }

  function generateDescription(element) {
    var tag = element.tagName.toLowerCase();
    var desc = tag;
    if (element.id) desc += '#' + element.id;
    if (element.className) desc += '.' + element.className.split(/\s+/).filter(Boolean).join('.');
    return desc;
  }

  window.__ic_contextmenu = function(e) {
    e.preventDefault();
    e.stopPropagation();
    var xpath = generateXPath(e.target);
    var css = generateCssSelector(e.target);
    var desc = generateDescription(e.target);
    window.__ic_last_right_clicked = JSON.stringify({description: desc, xpath: xpath, css_selector: css});
  };

  document.addEventListener('contextmenu', window.__ic_contextmenu, true);
})();
"""


class ElementPickerWindow(QMainWindow):
    """EasySpider 风格的内嵌浏览器元素拾取器主窗口。

    通过左侧「拾取模式」按钮启用 Ctrl+click 拾取；拾取到的元素追加到右侧
    元素库面板，可继续验证输入 / 点击 / 高亮等动作。
    """

    # 元素库发生变化时发出（供外部订阅，如导出到提取规则面板）
    elementsChanged = Signal()
    # 任务执行过程中采集到数据时发出（主窗口连接后写入结果表格）
    data_collected = Signal(dict)

    def __init__(
        self,
        start_url: str = "",
        parent: QWidget | None = None,
        browser_type: str = "secretcrawler",
    ) -> None:
        super().__init__(parent)

        self.setWindowTitle("通过网页爬寻 - 元素拾取器")
        self.resize(1200, 800)

        # 是否处于拾取模式（已注入 JS 并启动轮询）
        self._picking_mode: bool = False
        # 元素库存储
        self._elements: list[ElementInfo] = []
        # 流程步骤列表（PickerAction 序列）
        self._steps: list[PickerAction] = []
        # 任务执行游标与状态
        self._exec_index: int = 0
        self._waiting_load_for_step: bool = False
        self._paginate_timeout_timer: QTimer | None = None
        # 循环动作执行状态
        self._loop_total: int = 0
        self._loop_index: int = 0
        self._loop_child_index: int = 0
        # JS 是否已注入当前页面
        self._picking_js_installed: bool = False
        # 延迟初始化的 QWebEngineView（在 showEvent 中创建以避免启动卡死）
        self._view: QWebEngineView | None = None
        self._view_initialized: bool = False
        # 右键上下文菜单轮询定时器
        self._right_click_timer: QTimer | None = None
        # 初始 URL（_init_web_view 创建 view 后加载）
        self._start_url: str = start_url
        # 浏览器类型：secretcrawler（内嵌）/ chrome / edge
        self._browser_type: str = browser_type

        # ---- 构建 UI ----
        self._build_address_toolbar()
        self._build_left_toolbar()
        self._build_placeholder()
        self._build_element_dock()

        # 拾取结果轮询定时器
        self._pick_timer = QTimer(self)
        self._pick_timer.setInterval(_PICK_POLL_INTERVAL_MS)
        self._pick_timer.timeout.connect(self._check_picked_element)

        # Esc 关闭窗口
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc_shortcut.activated.connect(self.close)

        if start_url:
            self._url_edit.setText(start_url)

    # ---------- UI 构建 ----------

    def _build_address_toolbar(self) -> None:
        """顶部地址栏工具栏：QLineEdit + 「前往」按钮。"""
        bar = QToolBar("地址栏", self)
        bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)

        self._url_edit = QLineEdit(self)
        self._url_edit.setPlaceholderText("输入网址，例如 https://example.com")
        self._url_edit.setMinimumWidth(480)

        self._go_btn = QPushButton("前往", self)
        self._go_btn.clicked.connect(self._on_navigate)
        self._url_edit.returnPressed.connect(self._on_navigate)

        bar.addWidget(self._url_edit)
        bar.addWidget(self._go_btn)

    def _build_left_toolbar(self) -> None:
        """左侧竖排工具栏：拾取模式 / 元素库 / 流程步骤 / 验证动作 / 保存任务 / 加载任务 / 执行任务 / 关闭。"""
        toolbar = QToolBar("拾取工具", self)
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, toolbar)

        self._pick_mode_btn = QPushButton("拾取模式", self)
        self._pick_mode_btn.setCheckable(True)
        self._pick_mode_btn.setToolTip("启用后按住 Ctrl 并点击页面元素即可拾取")
        self._pick_mode_btn.toggled.connect(self._on_pick_mode_toggled)

        self._library_btn = QPushButton("元素库", self)
        self._library_btn.setCheckable(True)
        self._library_btn.toggled.connect(self._on_library_toggled)

        self._steps_btn = QPushButton("流程步骤", self)
        self._steps_btn.setCheckable(True)
        self._steps_btn.toggled.connect(self._on_steps_toggled)

        self._verify_btn = QPushButton("验证动作", self)
        self._verify_btn.clicked.connect(self._on_verify_action)

        self._save_task_btn = QPushButton("保存任务", self)
        self._save_task_btn.clicked.connect(self._on_save_task)

        self._load_task_btn = QPushButton("加载任务", self)
        self._load_task_btn.clicked.connect(self._on_load_task)

        self._export_scskill_btn = QPushButton("导出为 .scskill", self)
        self._export_scskill_btn.clicked.connect(self._on_export_scskill)

        self._exec_task_btn = QPushButton("执行任务", self)
        self._exec_task_btn.clicked.connect(self._on_execute_task)

        self._close_btn = QPushButton("关闭", self)
        self._close_btn.clicked.connect(self.close)

        toolbar.addWidget(self._pick_mode_btn)
        toolbar.addSeparator()
        toolbar.addWidget(self._library_btn)
        toolbar.addWidget(self._steps_btn)
        toolbar.addSeparator()
        toolbar.addWidget(self._verify_btn)
        toolbar.addSeparator()
        toolbar.addWidget(self._save_task_btn)
        toolbar.addWidget(self._load_task_btn)
        toolbar.addWidget(self._export_scskill_btn)
        toolbar.addSeparator()
        toolbar.addWidget(self._exec_task_btn)
        toolbar.addSeparator()
        toolbar.addWidget(self._close_btn)

    def _build_placeholder(self) -> None:
        """加载 QWebEngineView 前的占位标签，避免同步初始化阻塞主线程。"""
        self._placeholder = QLabel("正在加载浏览器...", self)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCentralWidget(self._placeholder)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """窗口首次显示时延迟创建 QWebEngineView，避免启动卡死。"""
        super().showEvent(event)
        if not self._view_initialized:
            self._init_web_view()

    def _init_web_view(self) -> None:
        """延迟创建 QWebEngineView 并替换占位标签。"""
        self._view = QWebEngineView(self)
        self.setCentralWidget(self._view)
        if self._placeholder is not None:
            self._placeholder.deleteLater()
            self._placeholder = None
        self._view_initialized = True
        self._view.loadFinished.connect(self._on_load_finished)
        # 注入右键上下文菜单 JS（始终启用，不依赖拾取模式）
        self._view.page().runJavaScript(_CONTEXT_JS)
        # 启动右键轮询定时器（200ms）
        self._right_click_timer = QTimer(self)
        self._right_click_timer.setInterval(_PICK_POLL_INTERVAL_MS)
        self._right_click_timer.timeout.connect(self._check_right_clicked)
        self._right_click_timer.start()
        if self._start_url:
            self._view.load(QUrl(self._start_url))

    def _build_element_dock(self) -> None:
        """右侧 DockWidget，内含 QTabWidget：元素库 + 流程步骤。"""
        self._dock = QDockWidget("拾取面板", self)
        self._dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._dock)

        self._dock_tab = QTabWidget(self)
        self._dock_tab.setMinimumWidth(340)

        # ---- Tab 1: 元素库 ----
        elements_tab = QWidget(self)
        el_layout = QVBoxLayout(elements_tab)
        el_layout.setContentsMargins(6, 6, 6, 6)
        el_layout.setSpacing(6)

        el_hint = QLabel("拾取到的元素将列在此处：")
        el_hint.setStyleSheet("font-weight: bold;")
        el_layout.addWidget(el_hint)

        self._element_list = QListWidget(self)
        self._element_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._element_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._element_list.customContextMenuRequested.connect(self._on_element_context_menu)
        el_layout.addWidget(self._element_list, 1)

        del_btn = QPushButton("删除选中", self)
        del_btn.clicked.connect(self._on_delete_element)
        clear_btn = QPushButton("清空", self)
        clear_btn.clicked.connect(self._on_clear_elements)

        el_row = QHBoxLayout()
        el_row.addWidget(del_btn)
        el_row.addWidget(clear_btn)
        el_layout.addLayout(el_row)

        self._dock_tab.addTab(elements_tab, "元素库")

        # ---- Tab 2: 流程步骤 ----
        steps_tab = QWidget(self)
        st_layout = QVBoxLayout(steps_tab)
        st_layout.setContentsMargins(6, 6, 6, 6)
        st_layout.setSpacing(6)

        st_hint = QLabel("已设计的任务步骤：")
        st_hint.setStyleSheet("font-weight: bold;")
        st_layout.addWidget(st_hint)

        self._steps_list = QListWidget(self)
        self._steps_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        st_layout.addWidget(self._steps_list, 1)

        del_step_btn = QPushButton("删除选中", self)
        del_step_btn.clicked.connect(self._on_delete_step)
        up_btn = QPushButton("上移", self)
        up_btn.clicked.connect(self._on_move_step_up)
        down_btn = QPushButton("下移", self)
        down_btn.clicked.connect(self._on_move_step_down)
        clear_steps_btn = QPushButton("清空", self)
        clear_steps_btn.clicked.connect(self._on_clear_steps)

        st_row = QHBoxLayout()
        st_row.addWidget(del_step_btn)
        st_row.addWidget(up_btn)
        st_row.addWidget(down_btn)
        st_row.addWidget(clear_steps_btn)
        st_layout.addLayout(st_row)

        self._dock_tab.addTab(steps_tab, "流程步骤")

        self._dock.setWidget(self._dock_tab)
        self._dock.setVisible(False)

    # ---------- 地址栏 ----------

    def _on_navigate(self) -> None:
        """从地址栏读取 URL，补全 scheme 后加载。"""
        url = self._url_edit.text().strip()
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
            self._url_edit.setText(url)
        self._picking_js_installed = False
        if self._view is None:
            # 视图尚未初始化，记住 URL，待 _init_web_view 加载
            self._start_url = url
            return
        self._view.setUrl(QUrl(url))

    # ---------- 拾取模式 ----------

    def _on_pick_mode_toggled(self, checked: bool) -> None:
        """启用 / 关闭拾取模式。"""
        if self._view is None:
            if checked:
                QMessageBox.warning(self, "提示", "浏览器尚未加载完成，请稍候再试")
                # 防止按钮停留在选中状态
                self._pick_mode_btn.blockSignals(True)
                self._pick_mode_btn.setChecked(False)
                self._pick_mode_btn.blockSignals(False)
            return
        self._picking_mode = checked
        if checked:
            self._pick_mode_btn.setText("拾取模式: 开")
            self._inject_picking_js()
            if not self._pick_timer.isActive():
                self._pick_timer.start()
        else:
            self._pick_mode_btn.setText("拾取模式: 关")
            self._remove_picking_js()
            if self._pick_timer.isActive():
                self._pick_timer.stop()

    def _on_library_toggled(self, checked: bool) -> None:
        """切换元素库 DockWidget 可见性并切换到「元素库」tab。"""
        if checked:
            self._dock_tab.setCurrentIndex(0)
            self._dock.setVisible(True)
            # 互斥：取消勾选「流程步骤」
            if self._steps_btn.isChecked():
                self._steps_btn.blockSignals(True)
                self._steps_btn.setChecked(False)
                self._steps_btn.blockSignals(False)
        else:
            self._dock.setVisible(False)

    def _on_steps_toggled(self, checked: bool) -> None:
        """切换 DockWidget 可见性并切换到「流程步骤」tab。"""
        if checked:
            self._dock_tab.setCurrentIndex(1)
            self._dock.setVisible(True)
            # 互斥：取消勾选「元素库」
            if self._library_btn.isChecked():
                self._library_btn.blockSignals(True)
                self._library_btn.setChecked(False)
                self._library_btn.blockSignals(False)
        else:
            self._dock.setVisible(False)

    def _inject_picking_js(self) -> None:
        """向当前页面注入拾取 JS 并启动轮询。"""
        self._view.page().runJavaScript(_PICK_JS, self._on_js_injected)
        self._picking_js_installed = True
        if not self._pick_timer.isActive():
            self._pick_timer.start()

    def _on_js_injected(self, _result: Any) -> None:
        """JS 注入完成回调（仅用于标记，无需处理结果）。"""
        # runJavaScript 的回调在 JS 执行后触发；IIFE 无返回值，忽略即可。

    def _remove_picking_js(self) -> None:
        """移除拾取 JS 并停止轮询。"""
        self._view.page().runJavaScript(_UNPICK_JS)
        self._picking_js_installed = False
        if self._pick_timer.isActive():
            self._pick_timer.stop()

    def _check_picked_element(self) -> None:
        """轮询 window.__ic_last_picked，若有新结果则读取并处理。"""
        if not self._picking_mode:
            return
        if self._view is None:
            return
        js = "window.__ic_last_picked || ''"
        self._view.page().runJavaScript(js, self._on_pick_result)

    def _on_pick_result(self, result: Any) -> None:
        """轮询结果回调：解析 JSON 并追加到元素库。"""
        if not result:
            return
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            return
        # 解析成功后再清空标记，避免覆盖丢失（Task 4: 修复 Ctrl 连续拾取）
        if self._view is not None:
            self._view.page().runJavaScript("window.__ic_last_picked = null;")
        info = ElementInfo(
            description=data.get("description", ""),
            xpath=data.get("xpath", ""),
            css_selector=data.get("css_selector", ""),
        )
        self._elements.append(info)
        self._refresh_element_list()
        self.elementsChanged.emit()

    # ---------- 页面加载 ----------

    def _on_load_finished(self, ok: bool) -> None:
        """页面加载完成后：刷新地址栏 URL，必要时重新注入拾取 JS 与右键菜单 JS。"""
        qurl = self._view.url()
        if qurl.isValid():
            self._url_edit.setText(qurl.toString())
        # 页面切换后 JS 状态会丢失，需要重置标记并在拾取模式下重注入
        self._picking_js_installed = False
        if self._picking_mode:
            self._inject_picking_js()
        # 右键上下文菜单 JS 在每次页面加载后重新注入
        self._view.page().runJavaScript(_CONTEXT_JS)
        # 翻页动作等待 loadFinished 触发
        if self._waiting_load_for_step:
            self._waiting_load_for_step = False
            if self._paginate_timeout_timer is not None:
                self._paginate_timeout_timer.stop()
                self._paginate_timeout_timer = None
            self._exec_index += 1
            QTimer.singleShot(500, self._execute_next_step)

    # ---------- 元素库 ----------

    def _refresh_element_list(self) -> None:
        """根据 self._elements 重建 QListWidget。"""
        self._element_list.clear()
        for i, info in enumerate(self._elements):
            text = (
                f"{i + 1}. {info.description} | "
                f"XPath: {info.xpath} | CSS: {info.css_selector}"
            )
            QListWidgetItem(text, self._element_list)

    def _on_delete_element(self) -> None:
        """删除元素库中当前选中的条目。"""
        row = self._element_list.currentRow()
        if row < 0 or row >= len(self._elements):
            return
        del self._elements[row]
        self._refresh_element_list()
        self.elementsChanged.emit()

    def _on_clear_elements(self) -> None:
        """清空元素库。"""
        if not self._elements:
            return
        self._elements.clear()
        self._refresh_element_list()
        self.elementsChanged.emit()

    def _on_element_context_menu(self, pos) -> None:
        """元素库右键菜单：将选中元素加入流程步骤。"""
        list_widget = self._element_list
        item = list_widget.itemAt(pos)
        if item is None:
            return
        row = list_widget.row(item)
        if row < 0 or row >= len(self._elements):
            return
        element = self._elements[row]

        menu = QMenu(self)
        add_action = menu.addAction("加入流程步骤")
        chosen = menu.exec(list_widget.mapToGlobal(pos))
        if chosen is add_action:
            self._on_add_element_to_steps(element)

    def _on_add_element_to_steps(self, element: ElementInfo) -> None:
        """弹出对话框选择动作类型，将元素转为 PickerAction 追加到流程步骤。"""
        # 动作类型选择
        dialog = QDialog(self)
        dialog.setWindowTitle("加入流程步骤")
        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel("选择动作类型:"))
        type_combo = QComboBox(dialog)
        type_combo.addItems(["输入文字", "点击元素", "采集数据"])
        layout.addWidget(type_combo)

        value_edit = QLineEdit(dialog)
        value_edit.setPlaceholderText("输入文字的值 / 采集字段名（按动作类型填写）")
        layout.addWidget(value_edit)

        attr_combo = QComboBox(dialog)
        attr_combo.addItems(["textContent", "innerHTML", "href", "src", "value"])
        attr_combo.setEnabled(False)
        layout.addWidget(QLabel("采集属性（仅采集数据）:"))
        layout.addWidget(attr_combo)

        def on_type_changed(idx: int) -> None:
            # 采集数据时启用属性选择
            attr_combo.setEnabled(idx == 2)
            if idx == 0:
                value_edit.setPlaceholderText("输入文字的值")
            elif idx == 2:
                value_edit.setPlaceholderText("采集字段名")
            else:
                value_edit.setPlaceholderText("（点击元素无需输入）")
        type_combo.currentIndexChanged.connect(on_type_changed)

        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("确定", dialog)
        cancel_btn = QPushButton("取消", dialog)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        action_map = {"输入文字": "input", "点击元素": "click", "采集数据": "collect"}
        action_type = action_map.get(type_combo.currentText(), "click")
        value = value_edit.text().strip()

        action = PickerAction(
            action_type=action_type,
            css_selector=element.css_selector,
            xpath=element.xpath,
            description=element.description,
            value=value if action_type in ("input", "collect") else "",
            field_name=value if action_type == "collect" else "",
            extract_attr=attr_combo.currentText() if action_type == "collect" else "",
            children=None,
        )
        self._steps.append(action)
        self._refresh_steps_list()

    # ---------- 流程步骤 ----------

    def _refresh_steps_list(self) -> None:
        """根据 self._steps 重建流程步骤 QListWidget。"""
        self._steps_list.clear()
        for i, step in enumerate(self._steps):
            t = step.action_type
            if t == "input":
                text = f"{i + 1}. 输入文字 -> {step.css_selector}: {step.value}"
            elif t == "click":
                text = f"{i + 1}. 点击元素 -> {step.css_selector}"
            elif t == "collect":
                text = (
                    f"{i + 1}. 采集数据 -> {step.css_selector} "
                    f"({step.field_name}, {step.extract_attr})"
                )
            elif t == "select_all":
                text = f"{i + 1}. 选中全部同类 -> {step.css_selector}"
            elif t == "paginate":
                text = f"{i + 1}. 翻页按钮 -> {step.css_selector}"
            elif t == "loop":
                text = f"{i + 1}. 循环容器 -> {step.css_selector}"
            else:
                text = f"{i + 1}. {step.action_type} -> {step.css_selector}"
            QListWidgetItem(text, self._steps_list)

    def _on_delete_step(self) -> None:
        """删除流程步骤中当前选中的条目。"""
        row = self._steps_list.currentRow()
        if row < 0 or row >= len(self._steps):
            return
        del self._steps[row]
        self._refresh_steps_list()

    def _on_move_step_up(self) -> None:
        """将当前选中的步骤上移一位。"""
        row = self._steps_list.currentRow()
        if row <= 0 or row >= len(self._steps):
            return
        self._steps[row - 1], self._steps[row] = (
            self._steps[row],
            self._steps[row - 1],
        )
        self._refresh_steps_list()
        self._steps_list.setCurrentRow(row - 1)

    def _on_move_step_down(self) -> None:
        """将当前选中的步骤下移一位。"""
        row = self._steps_list.currentRow()
        if row < 0 or row >= len(self._steps) - 1:
            return
        self._steps[row + 1], self._steps[row] = (
            self._steps[row],
            self._steps[row + 1],
        )
        self._refresh_steps_list()
        self._steps_list.setCurrentRow(row + 1)

    def _on_clear_steps(self) -> None:
        """清空流程步骤列表。"""
        if not self._steps:
            return
        self._steps.clear()
        self._refresh_steps_list()

    def _add_action(self, action: PickerAction) -> None:
        """将 PickerAction 追加到步骤列表并刷新 UI / 状态栏。"""
        self._steps.append(action)
        self._refresh_steps_list()
        self.statusBar().showMessage(
            f"已添加步骤: {action.action_type} -> {action.css_selector}", 3000
        )

    # ---------- 右键上下文菜单 ----------

    def _check_right_clicked(self) -> None:
        """轮询 window.__ic_last_right_clicked，若有新结果则交给 _on_right_clicked。"""
        if self._view is None:
            return
        self._view.page().runJavaScript(
            "window.__ic_last_right_clicked || ''", self._on_right_clicked
        )

    def _on_right_clicked(self, result: Any) -> None:
        """右键轮询结果回调：解析后弹出上下文菜单。"""
        if not result:
            return
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            return
        # 立即清空标记，避免重复弹出菜单
        if self._view is not None:
            self._view.page().runJavaScript("window.__ic_last_right_clicked = null;")
        desc = data.get("description", "")
        xpath = data.get("xpath", "")
        css = data.get("css_selector", "")
        self._show_context_menu(desc, xpath, css)

    def _show_context_menu(self, desc: str, xpath: str, css: str) -> None:
        """在鼠标位置弹出右键菜单，选择动作后追加到流程步骤。"""
        menu = QMenu(self)
        act_input = menu.addAction("输入文字")
        act_click = menu.addAction("点击元素")
        act_collect = menu.addAction("采集该元素数据")
        act_select_all = menu.addAction("选中全部同类元素")
        act_paginate = menu.addAction("设为翻页按钮")
        act_loop = menu.addAction("设为循环项")

        chosen = menu.exec(QCursor.pos())
        if chosen is act_input:
            value = self._prompt_input_value()
            if value is None:
                return
            self._add_action(
                PickerAction(
                    action_type="input",
                    css_selector=css,
                    xpath=xpath,
                    description=desc,
                    value=value,
                )
            )
        elif chosen is act_click:
            self._add_action(
                PickerAction(
                    action_type="click",
                    css_selector=css,
                    xpath=xpath,
                    description=desc,
                )
            )
        elif chosen is act_collect:
            result = self._prompt_collect_config()
            if result is None:
                return
            field_name, extract_attr = result
            self._add_action(
                PickerAction(
                    action_type="collect",
                    css_selector=css,
                    xpath=xpath,
                    description=desc,
                    field_name=field_name,
                    extract_attr=extract_attr,
                )
            )
        elif chosen is act_select_all:
            self._add_action(
                PickerAction(
                    action_type="select_all",
                    css_selector=css,
                    xpath=xpath,
                    description=desc,
                )
            )
        elif chosen is act_paginate:
            self._add_action(
                PickerAction(
                    action_type="paginate",
                    css_selector=css,
                    xpath=xpath,
                    description=desc,
                )
            )
        elif chosen is act_loop:
            # 弹出循环次数对话框（MVP 不实际配置子动作）
            if self._prompt_loop_count() is None:
                return
            self._add_action(
                PickerAction(
                    action_type="loop",
                    css_selector=css,
                    xpath=xpath,
                    description=desc,
                )
            )

    def _prompt_input_value(self) -> str | None:
        """弹出对话框让用户输入待填入的文本。返回 None 表示取消。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("输入文字")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("请输入要填入的文本："))
        edit = QLineEdit(dialog)
        layout.addWidget(edit)
        btn_row = QHBoxLayout()
        ok_btn = QPushButton("确定", dialog)
        cancel_btn = QPushButton("取消", dialog)
        btn_row.addStretch(1)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        edit.returnPressed.connect(dialog.accept)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return edit.text()

    def _prompt_collect_config(self) -> tuple[str, str] | None:
        """弹出对话框让用户配置采集字段名与提取属性。返回 None 表示取消。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("采集该元素数据")
        layout = QVBoxLayout(dialog)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("字段名:"))
        name_edit = QLineEdit(dialog)
        name_edit.setPlaceholderText("如 title / price ...")
        name_row.addWidget(name_edit, 1)
        layout.addLayout(name_row)

        attr_row = QHBoxLayout()
        attr_row.addWidget(QLabel("提取属性:"))
        attr_combo = QComboBox(dialog)
        attr_combo.addItems(["text", "attr", "html"])
        attr_row.addWidget(attr_combo, 1)
        layout.addLayout(attr_row)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("确定", dialog)
        cancel_btn = QPushButton("取消", dialog)
        btn_row.addStretch(1)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return name_edit.text(), attr_combo.currentText()

    def _prompt_loop_count(self) -> str | None:
        """弹出对话框让用户输入循环次数（可选）。返回 None 表示取消。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("设为循环项")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("循环次数（留空表示遍历全部子元素）："))
        edit = QLineEdit(dialog)
        edit.setPlaceholderText("可选，默认为空")
        layout.addWidget(edit)
        btn_row = QHBoxLayout()
        ok_btn = QPushButton("确定", dialog)
        cancel_btn = QPushButton("取消", dialog)
        btn_row.addStretch(1)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return edit.text()

    # ---------- 任务保存与加载 ----------

    def _on_save_task(self) -> None:
        """将当前流程步骤保存为 .pickertask / .json 文件。"""
        if not self._steps:
            QMessageBox.warning(self, "提示", "无步骤可保存")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存任务",
            "task.pickertask",
            "Picker Task (*.pickertask);;JSON (*.json);;All (*.*)",
        )
        if not path:
            return
        task = PickerTask(
            name=os.path.basename(path),
            actions=self._steps,
            created_at=datetime.datetime.now().isoformat(),
        )
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(task.to_dict(), f, indent=2, ensure_ascii=False)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", f"写入文件出错: {exc}")
            return
        self.statusBar().showMessage(f"已保存任务: {path}", 3000)

    def _on_load_task(self) -> None:
        """从 .pickertask / .json 文件加载流程步骤。"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "加载任务",
            "",
            "Picker Task (*.pickertask);;JSON (*.json);;All (*.*)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "加载失败", f"读取文件出错: {exc}")
            return
        task = PickerTask.from_dict(data)
        self._steps = task.actions
        self._refresh_steps_list()
        self.statusBar().showMessage(f"已加载任务: {path}", 3000)

    def _on_export_scskill(self) -> None:
        """将当前流程步骤导出为 .scskill 文件。"""
        if not self._steps:
            QMessageBox.warning(self, "无法导出", "当前无步骤可导出")
            return
        name, ok = QInputDialog.getText(
            self, "导出为 .scskill", "脚本名称:", text="my_script"
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        # 确定 skill/ 目录
        try:
            from __main__ import __file__ as main_file  # type: ignore
            base = os.path.dirname(os.path.abspath(main_file))
        except Exception:  # noqa: BLE001
            base = os.getcwd()
        skill_dir = os.path.join(base, "skill")
        os.makedirs(skill_dir, exist_ok=True)
        path = os.path.join(skill_dir, f"{name}.scskill")
        # 构造 PickerTask 并序列化
        task = PickerTask(
            name=name,
            actions=self._steps,
            created_at=datetime.datetime.now().isoformat(),
            metadata={"author": "用户", "description": ""},
        )
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(task.to_dict(), f, indent=2, ensure_ascii=False)
            self.statusBar().showMessage(f"已导出: {path}", 5000)
            QMessageBox.information(self, "导出成功", f"脚本已保存到：\n{path}")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"写入文件失败：\n{exc}")

    # ---------- 执行任务 ----------

    def _on_execute_task(self) -> None:
        """开始按顺序执行流程步骤。"""
        if not self._steps:
            QMessageBox.warning(self, "提示", "无步骤可执行")
            return
        if self._view is None:
            QMessageBox.warning(self, "提示", "浏览器尚未加载完成")
            return
        # 若选择外部浏览器（Chrome/Edge），启动之
        if self._browser_type != "secretcrawler":
            try:
                from crawler.browser_launcher import launch_browser

                proc = launch_browser(self._browser_type, self._start_url)
                if proc is None:
                    QMessageBox.warning(
                        self,
                        "浏览器启动失败",
                        f"未找到 {self._browser_type} 浏览器，已回退到内嵌浏览器执行。",
                    )
                    self._browser_type = "secretcrawler"
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(
                    self,
                    "浏览器启动失败",
                    f"启动 {self._browser_type} 时发生错误：\n{exc}",
                )
                self._browser_type = "secretcrawler"
        self._exec_index = 0
        self._execute_next_step()

    def run_task(self, task: PickerTask) -> None:
        """按给定的 PickerTask 执行任务（外部调用入口）。"""
        self._steps = list(task.actions)
        self._refresh_steps_list()
        # 延迟一下执行，等 view 加载完成
        QTimer.singleShot(1000, self._on_execute_task)

    def _execute_next_step(self) -> None:
        """执行 self._steps[self._exec_index] 对应的 JS 动作。"""
        if self._exec_index >= len(self._steps):
            self.statusBar().showMessage("任务执行完成", 5000)
            return
        step = self._steps[self._exec_index]
        self.statusBar().showMessage(
            f"正在执行步骤 {self._exec_index + 1}/{len(self._steps)}: "
            f"{step.action_type} -> {step.css_selector}"
        )

        if step.action_type == "loop":
            # 先通过 JS 拉取循环容器的子节点数量
            css_safe = step.css_selector.replace("\\", "\\\\").replace('"', '\\"')
            js = (
                'try { var el = document.querySelector("' + css_safe + '");'
                ' if(!el) { "NOT_FOUND"; } else { String(el.children.length); } }'
                ' catch(e) { "ERROR:" + e.message; }'
            )

            def _on_loop_count(result: Any, step: PickerAction = step) -> None:
                if (
                    not result
                    or (isinstance(result, str) and result.startswith("NOT_FOUND"))
                    or (isinstance(result, str) and result.startswith("ERROR:"))
                ):
                    logging.getLogger("ui.element_picker").warning(
                        "循环容器未找到: %s", step.css_selector
                    )
                    self._exec_index += 1
                    QTimer.singleShot(500, self._execute_next_step)
                    return
                try:
                    self._loop_total = int(result)
                except (TypeError, ValueError):
                    self._loop_total = 0
                self._loop_index = 0
                self._loop_child_index = 0
                self._execute_loop_iteration(step)

            self._view.page().runJavaScript(js, _on_loop_count)
            return  # 不要落入默认推进逻辑

        js, callback_type = self._build_action_js(step)
        if callback_type == "skip":
            # 未知动作类型，跳过
            self._exec_index += 1
            QTimer.singleShot(500, self._execute_next_step)
            return

        callback = {
            "step": self._on_step_done,
            "collect": self._on_collect_done,
            "select_all": self._on_select_all_done,
            "paginate": self._on_paginate_done,
        }.get(callback_type, self._on_step_done)
        self._view.page().runJavaScript(js, lambda r: callback(r, step))

    def _build_action_js(self, action: PickerAction) -> tuple[str, str]:
        """根据单个 PickerAction 构建在页面中执行的 JS 与回调类型。

        返回 ``(js_string, callback_type)``，其中 ``callback_type`` 取值为
        "step" / "collect" / "select_all" / "paginate" / "skip"。"loop" 与未知
        类型返回 ``("", "skip")``，由调用方单独处理。
        """
        css_safe = action.css_selector.replace("\\", "\\\\").replace('"', '\\"')

        if action.action_type == "input":
            value_safe = action.value.replace("\\", "\\\\").replace('"', '\\"')
            js = (
                'try { var el = document.querySelector("' + css_safe + '");'
                ' if(!el) { "NOT_FOUND"; } else { el.value = "' + value_safe + '";'
                ' el.dispatchEvent(new Event("input", {bubbles:true}));'
                ' el.dispatchEvent(new Event("change", {bubbles:true})); "OK"; } }'
                ' catch(e) { "ERROR:"+e.message; }'
            )
            return js, "step"
        if action.action_type == "click":
            js = (
                'try { var el = document.querySelector("' + css_safe + '");'
                ' if(!el) { "NOT_FOUND"; } else { el.click(); "OK"; } }'
                ' catch(e) { "ERROR:"+e.message; }'
            )
            return js, "step"
        if action.action_type == "collect":
            if action.extract_attr == "text":
                attr_expr = "el.textContent"
            elif action.extract_attr == "attr":
                fn_safe = action.field_name.replace("\\", "\\\\").replace('"', '\\"')
                attr_expr = 'el.getAttribute("' + fn_safe + '")'
            else:  # html
                attr_expr = "el.innerHTML"
            js = (
                'try { var el = document.querySelector("' + css_safe + '");'
                ' if(!el) { "NOT_FOUND"; } else { var v = ' + attr_expr + ';'
                ' "VALUE:"+String(v); } } catch(e) { "ERROR:"+e.message; }'
            )
            return js, "collect"
        if action.action_type == "select_all":
            js = (
                'try { var els = document.querySelectorAll("' + css_safe + '");'
                ' var arr = []; for(var i=0;i<els.length;i++){'
                ' arr.push({description: els[i].tagName.toLowerCase(), xpath: "",'
                ' css_selector: "' + css_safe + ':nth-of-type("+(i+1)+")"}); }'
                ' JSON.stringify(arr); } catch(e) { "ERROR:"+e.message; }'
            )
            return js, "select_all"
        if action.action_type == "paginate":
            js = (
                'try { var el = document.querySelector("' + css_safe + '");'
                ' if(!el) { "NOT_FOUND"; } else { el.click(); "OK"; } }'
                ' catch(e) { "ERROR:"+e.message; }'
            )
            return js, "paginate"
        # loop 与未知类型由调用方单独处理
        return "", "skip"

    def _execute_loop_iteration(self, step: PickerAction) -> None:
        """执行循环动作的单次迭代：在当前子节点上依次执行子动作。"""
        if self._loop_index >= self._loop_total:
            # 所有迭代完成，推进主游标
            self._exec_index += 1
            QTimer.singleShot(500, self._execute_next_step)
            return

        if not step.children:
            logging.getLogger("ui.element_picker").warning("循环无子动作，跳过")
            self._loop_index += 1
            QTimer.singleShot(200, lambda: self._execute_loop_iteration(step))
            return

        if self._loop_child_index >= len(step.children):
            # 当前迭代的所有子动作完成，进入下一次迭代
            self._loop_child_index = 0
            self._loop_index += 1
            QTimer.singleShot(200, lambda: self._execute_loop_iteration(step))
            return

        child = step.children[self._loop_child_index]
        # 将子动作的 CSS 选择器限定到当前循环子节点
        scoped_css = (
            step.css_selector
            + " > :nth-child(" + str(self._loop_index + 1) + ") "
            + child.css_selector
        )
        scoped_child = PickerAction(
            action_type=child.action_type,
            css_selector=scoped_css,
            xpath=child.xpath,
            description=child.description,
            value=child.value,
            field_name=child.field_name,
            extract_attr=child.extract_attr,
            children=child.children,  # 嵌套循环 MVP 不支持
        )

        js, callback_type = self._build_action_js(scoped_child)
        if callback_type == "skip":
            self._loop_child_index += 1
            QTimer.singleShot(100, lambda: self._execute_loop_iteration(step))
            return

        # 循环内子动作统一走 _on_loop_child_done 回调推进
        self._view.page().runJavaScript(
            js, lambda r: self._on_loop_child_done(r, step)
        )

    def _on_loop_child_done(self, result: Any, step: PickerAction) -> None:
        """循环子动作执行完毕回调：记录结果并推进到下一个子动作。"""
        logging.getLogger("ui.element_picker").info(
            "循环子动作结果 (loop_index=%d, child_index=%d): %s",
            self._loop_index,
            self._loop_child_index,
            result,
        )
        self._loop_child_index += 1
        QTimer.singleShot(300, lambda: self._execute_loop_iteration(step))

    def _on_step_done(self, result: Any, step: PickerAction) -> None:
        """普通动作（input/click）执行完毕回调：记录状态后推进游标。"""
        status = str(result) if result is not None else ""
        if status == "NOT_FOUND":
            self.statusBar().showMessage(
                f"步骤 {self._exec_index + 1}: 未找到元素 {step.css_selector}", 5000
            )
        elif status.startswith("ERROR:"):
            self.statusBar().showMessage(
                f"步骤 {self._exec_index + 1}: 执行出错 {status[6:]}", 5000
            )
        self._exec_index += 1
        QTimer.singleShot(500, self._execute_next_step)

    def _on_collect_done(self, result: Any, step: PickerAction) -> None:
        """采集动作执行完毕回调：emit data_collected 后推进游标。"""
        status = str(result) if result is not None else ""
        if status.startswith("VALUE:"):
            value = status[6:]
            self.data_collected.emit(
                {"field": step.field_name or step.description, "value": value}
            )
        elif status == "NOT_FOUND":
            self.statusBar().showMessage(
                f"采集步骤: 未找到元素 {step.css_selector}", 5000
            )
        elif status.startswith("ERROR:"):
            self.statusBar().showMessage(
                f"采集步骤: 执行出错 {status[6:]}", 5000
            )
        self._exec_index += 1
        QTimer.singleShot(500, self._execute_next_step)

    def _on_select_all_done(self, result: Any, step: PickerAction) -> None:
        """select_all 动作回调：将匹配元素批量加入元素库后推进游标。"""
        if isinstance(result, str) and result.startswith("["):
            try:
                arr = json.loads(result)
            except (TypeError, ValueError):
                arr = []
            for item in arr:
                info = ElementInfo(
                    description=item.get("description", ""),
                    xpath=item.get("xpath", ""),
                    css_selector=item.get("css_selector", ""),
                )
                self._elements.append(info)
            self._refresh_element_list()
            self.elementsChanged.emit()
        elif isinstance(result, str) and result.startswith("ERROR:"):
            self.statusBar().showMessage(
                f"选中全部同类: 执行出错 {result[6:]}", 5000
            )
        self._exec_index += 1
        QTimer.singleShot(500, self._execute_next_step)

    def _on_paginate_done(self, result: Any, step: PickerAction) -> None:
        """paginate 动作回调：等待 loadFinished 后再推进游标，5 秒超时兜底。"""
        status = str(result) if result is not None else ""
        if status != "OK":
            self.statusBar().showMessage(
                f"翻页步骤: 未找到元素或出错 {step.css_selector}", 5000
            )
            self._exec_index += 1
            QTimer.singleShot(500, self._execute_next_step)
            return
        # 等待 loadFinished 信号触发 _on_load_finished 中的分支推进游标
        self._waiting_load_for_step = True
        self._paginate_timeout_timer = QTimer(self)
        self._paginate_timeout_timer.setSingleShot(True)
        self._paginate_timeout_timer.timeout.connect(self._on_paginate_timeout)
        self._paginate_timeout_timer.start(5000)

    def _on_paginate_timeout(self) -> None:
        """翻页等待 loadFinished 超时兜底：强制推进游标。"""
        if not self._waiting_load_for_step:
            return
        self._waiting_load_for_step = False
        self._paginate_timeout_timer = None
        self.statusBar().showMessage(
            "翻页等待 loadFinished 超时，继续执行下一步", 5000
        )
        self._exec_index += 1
        QTimer.singleShot(500, self._execute_next_step)

    # ---------- 验证动作 ----------

    def _on_verify_action(self) -> None:
        """打开验证动作对话框，对选中元素执行输入 / 点击 / 高亮。"""
        if self._view is None:
            QMessageBox.warning(self, "提示", "浏览器尚未加载完成")
            return
        row = self._element_list.currentRow()
        if row < 0 or row >= len(self._elements):
            QMessageBox.warning(self, "提示", "请先在元素库中选择一个元素")
            return

        info = self._elements[row]
        css = info.css_selector or ""
        if not css:
            QMessageBox.warning(self, "提示", "该元素缺少 CSS 选择器，无法验证")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("验证动作")
        layout = QVBoxLayout(dialog)

        type_label = QLabel(f"元素: {info.description}")
        layout.addWidget(type_label)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("动作类型:"))
        type_combo = QComboBox()
        type_combo.addItems(["输入文本", "点击", "高亮定位"])
        type_row.addWidget(type_combo, 1)
        layout.addLayout(type_row)

        value_row = QHBoxLayout()
        value_row.addWidget(QLabel("动作值:"))
        value_edit = QLineEdit()
        value_edit.setPlaceholderText("仅在「输入文本」时使用")
        value_row.addWidget(value_edit, 1)
        layout.addLayout(value_row)

        def _on_type_changed(index: int) -> None:
            # 仅「输入文本」启用值输入框
            value_edit.setEnabled(index == 0)

        type_combo.currentIndexChanged.connect(_on_type_changed)
        _on_type_changed(type_combo.currentIndex())

        btn_row = QHBoxLayout()
        exec_btn = QPushButton("执行", dialog)
        cancel_btn = QPushButton("取消", dialog)
        btn_row.addStretch(1)
        btn_row.addWidget(exec_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        cancel_btn.clicked.connect(dialog.reject)

        def _on_exec() -> None:
            action_type = type_combo.currentText()
            value = value_edit.text()
            js, summary = self._build_verify_js(css, action_type, value)
            dialog.accept()
            self._view.page().runJavaScript(
                js, lambda r: self._on_verify_done(summary, r)
            )

        exec_btn.clicked.connect(_on_exec)
        dialog.exec()

    def _build_verify_js(
        self, css: str, action_type: str, value: str
    ) -> tuple[str, str]:
        """根据动作类型生成在页面中执行的 JS 与结果摘要。

        对 CSS 选择器与动作值中的双引号进行转义，避免 JS 注入。
        """
        css_safe = css.replace("\\", "\\\\").replace('"', '\\"')
        value_safe = value.replace("\\", "\\\\").replace('"', '\\"')

        if action_type == "输入文本":
            js = (
                "try { var el = document.querySelector(\"" + css_safe + "\");"
                " if(!el) { \"NOT_FOUND\"; } else { el.value = \"" + value_safe
                + "\"; \"OK\"; } } catch(e) { \"ERROR:\" + e.message; }"
            )
            summary = f"已输入: {value}"
        elif action_type == "点击":
            js = (
                "try { var el = document.querySelector(\"" + css_safe + "\");"
                " if(!el) { \"NOT_FOUND\"; } else { el.click(); \"OK\"; } }"
                " catch(e) { \"ERROR:\" + e.message; }"
            )
            summary = "已点击"
        else:
            # 高亮定位
            js = (
                "try { var el = document.querySelector(\"" + css_safe + "\");"
                " if(!el) { \"NOT_FOUND\"; } else { el.style.outline = "
                "\"3px solid red\"; setTimeout(function(){ el.style.outline = "
                "\"\"; }, 2000); \"OK\"; } } catch(e) { \"ERROR:\" + e.message; }"
            )
            summary = "已高亮 2 秒"
        return js, summary

    def _on_verify_done(self, summary: str, result: Any) -> None:
        """验证动作执行完毕回调：根据返回状态弹出提示。"""
        status = str(result) if result is not None else ""
        if status == "OK":
            QMessageBox.information(self, "验证结果", summary)
        elif status == "NOT_FOUND":
            QMessageBox.warning(self, "验证结果", "未找到匹配该选择器的元素")
        elif status.startswith("ERROR:"):
            QMessageBox.warning(self, "验证结果", f"执行出错: {status[6:]}")
        else:
            QMessageBox.information(self, "验证结果", summary)

    # ---------- 公共 API ----------

    def elements(self) -> list[ElementInfo]:
        """返回元素库当前内容的浅拷贝。"""
        return list(self._elements)

    def load_url(self, url: str) -> None:
        """外部调用入口：设置地址栏并加载 URL。"""
        self._url_edit.setText(url)
        self._on_navigate()

    # ---------- 关闭 ----------

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """窗口关闭时停止定时器并清理拾取 JS。"""
        if self._pick_timer.isActive():
            self._pick_timer.stop()
        if self._right_click_timer is not None and self._right_click_timer.isActive():
            self._right_click_timer.stop()
        if self._paginate_timeout_timer is not None and self._paginate_timeout_timer.isActive():
            self._paginate_timeout_timer.stop()
        try:
            if self._view is not None and self._picking_js_installed:
                self._view.page().runJavaScript(_UNPICK_JS)
        except Exception:
            pass
        # 显式关闭并清理元素库 dock，避免浮动 dock 作为独立窗口残留
        if hasattr(self, '_dock') and self._dock is not None:
            try:
                if self._dock.isFloating():
                    self._dock.setFloating(False)
                self._dock.close()
                self._dock.deleteLater()
            except Exception:
                pass
        super().closeEvent(event)
