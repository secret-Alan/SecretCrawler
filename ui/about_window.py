"""关于软件窗口 —— 显示应用信息、版本、开源协议与贡献者，并可检查更新。

模态对话框。点击「检查更新」会在后台线程中调用
:class:`crawler.updater.GitHubUpdater` 检查版本，避免阻塞 UI。
"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_REPO_URL = "https://github.com/secret-Alan/InternetCrawler"


class AboutWindow(QDialog):
    """「关于软件」模态对话框。

    展示应用图标、名称、版本、开源协议、仓库地址与贡献者；
    提供「检查更新」按钮，在后台线程执行版本检查，结果通过信号回到主线程处理。
    """

    # 后台检查完成时发出：(has_update, remote_version)
    # remote_version 为 str 或 None
    _check_result_ready = Signal(bool, object)

    def __init__(self, app_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._app_dir = app_dir
        self.setWindowTitle("关于软件")
        self.setMinimumSize(450, 400)

        # 读取本地版本
        self._local_version = self._read_version()
        # 读取贡献者
        contributors_text = self._read_contributors()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ---- 顶部：图标 + 名称 + 版本 ----
        top_layout = QHBoxLayout()
        self._icon_label = QLabel(self)
        icon_path = os.path.join(app_dir, "app.ico")
        pixmap = QPixmap(icon_path)
        if not pixmap.isNull():
            self._icon_label.setPixmap(
                pixmap.scaled(
                    64, 64,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self._icon_label.setText("📦")
            self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_layout.addWidget(self._icon_label)

        title_col = QVBoxLayout()
        name_label = QLabel("专业网络爬虫", self)
        name_font = name_label.font()
        name_font.setPointSize(16)
        name_font.setBold(True)
        name_label.setFont(name_font)
        title_col.addWidget(name_label)

        self._version_label = QLabel(f"版本: {self._local_version}", self)
        title_col.addWidget(self._version_label)
        top_layout.addLayout(title_col)
        top_layout.addStretch(1)
        layout.addLayout(top_layout)

        # ---- 信息区 ----
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        license_label = QLabel("本软件基于 GPL-3.0 协议开源", self)
        license_label.setWordWrap(True)
        form.addRow("协议:", license_label)

        repo_label = QLabel(
            f'开源仓库: <a href="{_REPO_URL}">{_REPO_URL}</a>', self
        )
        repo_label.setOpenExternalLinks(True)
        repo_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        form.addRow("仓库:", repo_label)

        self._contributors_label = QLabel(contributors_text, self)
        self._contributors_label.setWordWrap(True)
        form.addRow("贡献者:", self._contributors_label)

        layout.addLayout(form)
        layout.addStretch(1)

        # ---- 按钮行 ----
        btn_layout = QHBoxLayout()
        self._check_update_btn = QPushButton("检查更新", self)
        self._close_btn = QPushButton("关闭", self)
        btn_layout.addWidget(self._check_update_btn)
        btn_layout.addStretch(1)
        btn_layout.addWidget(self._close_btn)
        layout.addLayout(btn_layout)

        # 信号连接
        self._close_btn.clicked.connect(self.accept)
        self._check_update_btn.clicked.connect(self._on_check_update)
        self._check_result_ready.connect(self._on_check_result)

    # ---- 内部读取辅助 ----

    def _read_version(self) -> str:
        """读取 version.txt，缺失或异常返回 '0.0.0'。"""
        try:
            path = os.path.join(self._app_dir, "version.txt")
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                return content or "0.0.0"
        except Exception:  # noqa: BLE001
            pass
        return "0.0.0"

    def _read_contributors(self) -> str:
        """读取 contributors.txt，缺失则返回加载中提示。"""
        try:
            path = os.path.join(self._app_dir, "contributors.txt")
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                return "".join(lines).rstrip("\n") or "贡献者信息加载中…"
        except Exception:  # noqa: BLE001
            pass
        return "贡献者信息加载中…"

    # ---- 检查更新 ----

    def _on_check_update(self) -> None:
        """点击「检查更新」：禁用按钮，后台线程执行版本检查。"""
        self._check_update_btn.setEnabled(False)
        self._check_update_btn.setText("检查中…")

        def _do_check() -> None:
            try:
                from crawler.updater import GitHubUpdater

                updater = GitHubUpdater(self._app_dir)
                has_update, remote_version = updater.check_update()
            except Exception:  # noqa: BLE001
                has_update, remote_version = False, None
            self._check_result_ready.emit(has_update, remote_version)

        threading.Thread(target=_do_check, daemon=True).start()

    def _on_check_result(self, has_update: bool, remote_version: object) -> None:
        """后台检查完成：恢复按钮并处理结果。"""
        self._check_update_btn.setEnabled(True)
        self._check_update_btn.setText("检查更新")

        if not has_update or not remote_version:
            QMessageBox.information(self, "检查更新", "当前已是最新版本")
            return

        remote_ver = str(remote_version)
        from crawler.updater import GitHubUpdater

        updater = GitHubUpdater(self._app_dir)

        # 懒导入避免潜在的循环引用
        try:
            from ui.update_prompt_window import UpdatePromptWindow
        except Exception:  # noqa: BLE001
            QMessageBox.information(
                self, "检查更新",
                f"发现新版本 {remote_ver}，请前往仓库下载更新。",
            )
            return

        prompt = UpdatePromptWindow(self)
        try:
            prompt.set_version_info(self._local_version, remote_ver)
        except Exception:  # noqa: BLE001
            pass

        prompt.exec()

        choice = getattr(prompt, "choice", None)
        choice_value = choice() if callable(choice) else choice

        if choice_value == getattr(UpdatePromptWindow, "CHOICE_UPDATE", None):
            # 用户选择立即更新：提示重启以生效
            QMessageBox.information(
                self, "检查更新",
                f"将更新到 {remote_ver}，请重启应用以完成更新。",
            )
        elif choice_value == getattr(UpdatePromptWindow, "CHOICE_IGNORE", None):
            try:
                updater.ignore_version(remote_ver)
            except Exception:  # noqa: BLE001
                pass
        # CHOICE_LATER 或其它：什么都不做
