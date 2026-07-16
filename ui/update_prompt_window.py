"""更新提示窗口：发现新版本时的三选项模态对话框。

提供三个选项：「不再提醒我该版本的更新」、「稍后提醒我」、「立即更新」，
用户选择后可通过 ``get_choice()`` 读取用户决策。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


class UpdatePromptWindow(QDialog):
    """更新提示窗口（模态，三选项）。

    选项语义：
        - ``CHOICE_IGNORE``：不再提醒该版本，应写入本地忽略列表
        - ``CHOICE_LATER``：本次跳过，下次启动仍提示
        - ``CHOICE_UPDATE``：立即更新，应触发更新进度窗口
    """

    CHOICE_IGNORE = "ignore"
    CHOICE_LATER = "later"
    CHOICE_UPDATE = "update"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("更新提示")
        self.setMinimumWidth(400)

        self._choice: str | None = None

        layout = QVBoxLayout(self)

        # 标题
        self._title_label = QLabel("发现新版本", self)
        title_font = self._title_label.font()
        title_font.setPointSize(14)
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        layout.addWidget(self._title_label)

        # 版本号
        self._version_label = QLabel("新版本：", self)
        layout.addWidget(self._version_label)

        # 更新说明
        self._notes_label = QLabel("", self)
        self._notes_label.setWordWrap(True)
        layout.addWidget(self._notes_label)

        # 按钮行
        btn_layout = QHBoxLayout()
        self._ignore_btn = QPushButton("不再提醒我该版本的更新", self)
        self._later_btn = QPushButton("稍后提醒我", self)
        self._update_btn = QPushButton("立即更新", self)
        self._update_btn.setDefault(True)

        self._ignore_btn.clicked.connect(self._on_ignore)
        self._later_btn.clicked.connect(self._on_later)
        self._update_btn.clicked.connect(self._on_update)

        btn_layout.addWidget(self._ignore_btn)
        btn_layout.addWidget(self._later_btn)
        btn_layout.addWidget(self._update_btn)
        layout.addLayout(btn_layout)

    # ---- 内部槽 ----

    def _on_ignore(self) -> None:
        self._choice = self.CHOICE_IGNORE
        self.accept()

    def _on_later(self) -> None:
        self._choice = self.CHOICE_LATER
        self.accept()

    def _on_update(self) -> None:
        self._choice = self.CHOICE_UPDATE
        self.accept()

    # ---- 公共 API ----

    def set_version_info(self, version: str, notes: str = "") -> None:
        """设置版本信息与可选的更新说明。

        ``notes`` 为空字符串时隐藏更新说明标签。
        """
        self._version_label.setText(f"新版本：v{version}")
        if notes:
            self._notes_label.setText(notes)
            self._notes_label.show()
        else:
            self._notes_label.clear()
            self._notes_label.hide()

    def get_choice(self) -> str | None:
        """返回用户选择（``CHOICE_*`` 之一），未选择时返回 ``None``。"""
        return self._choice
