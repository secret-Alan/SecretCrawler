"""应用启动闪屏。

显示 app.ico 图标、应用名称、启动进度条、更新下载进度条与状态文本。
主窗口就绪后调用 set_ready()，闪屏会在至少显示 5 秒后自行关闭。
用户也可点击右上角关闭按钮提前退出应用。
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_LOG = logging.getLogger("ui.splash")

# 闪屏最少显示时长（秒），避免一闪而过
MIN_DISPLAY_SECONDS = 5.0


def _load_high_res_pixmap(icon_path: str, target_size: int) -> QPixmap:
    """从 ico 加载最大尺寸帧，避免 Qt 默认取最小帧再放大导致模糊。

    优先用 PIL 从 ico 提取最大尺寸帧转 PNG 再交给 QPixmap；
    PIL 不可用或失败时回退为 QPixmap 直接加载（仍由调用方 scaled）。
    """
    # 优先：PIL 提取 ico 中最大尺寸帧
    try:
        from PIL import IcoImagePlugin

        with open(icon_path, "rb") as fp:
            ico = IcoImagePlugin.IcoFile(fp)
            sizes = list(ico.sizes())
            if not sizes:
                raise RuntimeError("ico 无可用帧")
            # 选最大尺寸（按面积）
            best = max(sizes, key=lambda s: s[0] * s[1])
            frame = ico.getimage(best)
        frame = frame.convert("RGBA")
        _LOG.debug("splash 图标使用 PIL 提取 %s 帧渲染", best)
        buf = io.BytesIO()
        frame.save(buf, format="PNG")
        buf.seek(0)
        pm = QPixmap()
        pm.loadFromData(buf.getvalue(), "PNG")
        if not pm.isNull():
            return pm.scaled(
                target_size, target_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        _LOG.debug("splash PIL 提取帧后 QPixmap 加载为空，回退")
    except ImportError as e:
        _LOG.debug("splash PIL 不可用，回退默认加载: %s", e)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("splash PIL 提取 ico 帧失败，回退默认加载: %s", e)

    # 回退：直接用 QPixmap 加载（Qt 可能选小帧，但仍尽量缩放）
    pm = QPixmap(icon_path)
    if pm.isNull():
        return pm
    return pm.scaled(
        target_size, target_size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class SplashScreen(QWidget):
    """启动闪屏控件（无边框顶层窗口）。

    至少显示 ``MIN_DISPLAY_SECONDS`` 秒；主窗口就绪后调用 ``set_ready()``，
    闪屏会在达到最短显示时长后自动关闭。用户也可点击右上角 ✕ 按钮提前
    退出应用（通过 ``close_requested`` 信号）。
    """

    # 用户点击关闭按钮时发出，主程序应连接到 sys.exit(0)
    close_requested = Signal()

    def __init__(self, icon_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        # 不显示任务栏图标
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedSize(480, 360)

        # 5 秒最小显示计时
        self._start_time = time.monotonic()
        self._ready = False

        # 整体布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        # 图标（从 ico 最大帧提取以保证清晰）
        self._icon_label = QLabel(self)
        pixmap = _load_high_res_pixmap(icon_path, 128)
        if not pixmap.isNull():
            self._icon_label.setPixmap(pixmap)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._icon_label)

        # 应用名称
        self._title_label = QLabel("专业网络爬虫", self)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self._title_label.font()
        font.setPointSize(16)
        font.setBold(True)
        self._title_label.setFont(font)
        layout.addWidget(self._title_label)

        # 启动进度条
        self._startup_bar = QProgressBar(self)
        self._startup_bar.setRange(0, 100)
        self._startup_bar.setValue(0)
        self._startup_bar.setTextVisible(True)
        self._startup_bar.setFormat("启动中... %p%")
        layout.addWidget(self._startup_bar)

        # 更新下载进度条
        self._update_bar = QProgressBar(self)
        self._update_bar.setRange(0, 100)
        self._update_bar.setValue(0)
        self._update_bar.setTextVisible(True)
        self._update_bar.setFormat("更新检查... %p%")
        layout.addWidget(self._update_bar)

        # 状态文本
        self._status_label = QLabel("正在初始化…", self)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet("color: #555;")
        layout.addWidget(self._status_label)

        layout.addStretch(1)

        # 右上角关闭按钮（自有控件，覆盖在 frameless 窗口上）
        self._close_btn = QPushButton("✕", self)
        self._close_btn.setFixedSize(28, 28)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(
            "QPushButton { border: none; border-radius: 4px;"
            " font-size: 14px; color: #555; background: transparent; }"
            "QPushButton:hover { background: rgba(0,0,0,0.10); }"
            "QPushButton:pressed { background: rgba(0,0,0,0.18); }"
        )
        self._close_btn.setToolTip("退出应用")
        self._close_btn.clicked.connect(self._on_close_clicked)
        self._reposition_close_btn()

        # 100ms 轮询定时器：达到 5 秒且 ready 后自动关闭
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._try_finish)
        self._timer.start()

    # ---- 内部辅助 ----

    def _reposition_close_btn(self) -> None:
        """把关闭按钮放到右上角。"""
        margin = 8
        size = self._close_btn.width()
        self._close_btn.setGeometry(
            self.width() - size - margin, margin, size, size
        )

    def _on_close_clicked(self) -> None:
        """关闭按钮被点击：发出 close_requested 信号并关闭闪屏。"""
        self.close_requested.emit()
        self.close()

    def _try_finish(self) -> None:
        """达到最短显示时长且主窗口就绪时关闭闪屏。"""
        if not self._ready:
            return
        if time.monotonic() - self._start_time >= MIN_DISPLAY_SECONDS:
            self.close()

    def resizeEvent(self, event) -> None:  # noqa: N802
        """窗口尺寸变化时同步重定位关闭按钮。"""
        super().resizeEvent(event)
        self._reposition_close_btn()

    def closeEvent(self, event) -> None:  # noqa: N802
        """关闭时停止轮询定时器。"""
        if hasattr(self, "_timer"):
            self._timer.stop()
        super().closeEvent(event)

    # ---- 公共 API ----

    def set_startup_progress(self, value: int) -> None:
        """设置启动进度（0-100）。"""
        self._startup_bar.setValue(int(value))

    def set_update_progress(self, value: int) -> None:
        """设置更新下载进度（0-100）。"""
        self._update_bar.setValue(int(value))

    def set_status(self, text: str) -> None:
        """设置状态文本。"""
        self._status_label.setText(text)

    def set_ready(self) -> None:
        """主窗口就绪后调用；闪屏会在达到最短显示时长后自行关闭。"""
        self._ready = True
        self._try_finish()

    def finish(self, main_window: QWidget) -> None:
        """已废弃：保留以兼容旧调用。新代码请使用 set_ready()。"""
        self.set_ready()
