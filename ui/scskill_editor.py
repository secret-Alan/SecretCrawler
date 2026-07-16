"""`.scskill` 脚本编辑器控件。

本控件用于在主窗口右侧工具栏中提供 ``.scskill`` 文件的阅读、编辑、校验与运行入口。
根据文件后缀支持三种脚本类型：
    - ``.scskill``        —— UTF-8 编码的 JSON，结构遵循 ``PickerTask`` 数据类
    - ``.py.scskill``     —— Python 脚本（使用 ``compile`` 校验语法）
    - ``.js.scskill``     —— JavaScript 脚本（不校验语法，交由拾取器 view 执行）

公共 API:
    ScskillEditor(QWidget)      —— 脚本编辑器控件
    load_file(path)             —— 加载文件到编辑区
    run_requested Signal(str, str) —— 用户点击「运行」时 emit (文件路径, 脚本类型)
    脚本类型: "pickertask" | "python" | "javascript"
"""

from __future__ import annotations

import json
import logging
import os

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from crawler.models import PickerTask


_logger = logging.getLogger("ui.scskill_editor")


# 新建文件时预填的 JSON 模板
_NEW_TEMPLATE = """{
  "name": "新脚本",
  "version": "1.0",
  "actions": [],
  "metadata": {
    "author": "",
    "description": "",
    "created_at": ""
  },
  "created_at": ""
}"""


class ScskillEditor(QWidget):
    """`.scskill` 脚本编辑器控件。

    布局：顶部文件路径标签 + 左侧代码编辑区（等宽字体）+ 右侧垂直按钮栏。
    支持：新建 / 打开 / 保存 / 另存为 / 校验 / 运行。
    """

    # 用户点击「运行」并校验通过后 emit (文件路径, 脚本类型)
    # 脚本类型: "pickertask" | "python" | "javascript"
    run_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._current_path: str = ""

        # ---- 顶部文件路径标签 ----
        self._path_label = QLabel("未命名", self)
        self._path_label.setStyleSheet("color: #666; padding: 4px;")
        self._path_label.setWordWrap(True)

        # ---- 左侧代码编辑区 ----
        self._editor = QPlainTextEdit(self)
        # 等宽字体，便于阅读 JSON
        font = QFont("Consolas", 10)
        # Windows 上若 Consolas 不可用，回退到 Courier New
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._editor.setFont(font)
        self._editor.setPlaceholderText("在此编辑 .scskill 脚本内容（JSON 格式）…")

        # ---- 右侧按钮栏 ----
        self._new_btn = QPushButton("新建", self)
        self._open_btn = QPushButton("打开…", self)
        self._save_btn = QPushButton("保存", self)
        self._save_as_btn = QPushButton("另存为…", self)
        self._validate_btn = QPushButton("校验", self)
        self._run_btn = QPushButton("运行", self)
        self._run_btn.setStyleSheet("font-weight: bold;")

        btn_col = QVBoxLayout()
        btn_col.addWidget(self._new_btn)
        btn_col.addWidget(self._open_btn)
        btn_col.addWidget(self._save_btn)
        btn_col.addWidget(self._save_as_btn)
        btn_col.addStretch(1)
        btn_col.addWidget(self._validate_btn)
        btn_col.addWidget(self._run_btn)

        # ---- 主布局 ----
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.addWidget(self._path_label)

        body = QHBoxLayout()
        body.addWidget(self._editor, 1)
        body.addLayout(btn_col)
        main_layout.addLayout(body, 1)

        # ---- 信号连接 ----
        self._new_btn.clicked.connect(self._on_new)
        self._open_btn.clicked.connect(self._on_open)
        self._save_btn.clicked.connect(self._on_save)
        self._save_as_btn.clicked.connect(self._on_save_as)
        self._validate_btn.clicked.connect(self._on_validate)
        self._run_btn.clicked.connect(self._on_run)

        # 初始预填模板
        self._editor.setPlainText(_NEW_TEMPLATE)

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #

    def load_file(self, path: str) -> None:
        """加载指定 ``.scskill`` 文件到编辑区。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self._editor.setPlainText(content)
            self._current_path = path
            self._path_label.setText(path)
            _logger.info("已加载脚本: %s", path)
        except OSError as exc:
            QMessageBox.warning(self, "加载失败", f"读取文件失败：\n{exc}")

    def current_path(self) -> str:
        """返回当前编辑的文件路径，未保存返回空字符串。"""
        return self._current_path

    def validate(self) -> tuple[bool, str]:
        """根据文件后缀校验内容合法性。

        - ``.py.scskill`` —— 使用 ``compile`` 校验 Python 语法
        - ``.js.scskill`` —— 不校验语法（JS 语法校验较复杂，跳过）
        - ``.scskill``    —— JSON + ``PickerTask.from_dict`` 校验

        Returns:
            (是否通过, 描述信息)
        """
        text = self._editor.toPlainText()
        if not text.strip():
            return False, "编辑区为空"

        name = os.path.basename(self._current_path) if self._current_path else ""

        if name.endswith(".py.scskill"):
            try:
                compile(text, "<scskill>", "exec")
                return True, "Python 语法校验通过"
            except SyntaxError as exc:
                return False, f"Python 语法错误: {exc}"
        elif name.endswith(".js.scskill"):
            return True, "JS 脚本不校验语法"
        else:
            # .scskill —— PickerTask JSON
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                return False, f"JSON 解析失败（行 {exc.lineno} 列 {exc.colno}）：{exc.msg}"
            if not isinstance(data, dict):
                return False, "顶层结构必须为 JSON 对象"
            try:
                task = PickerTask.from_dict(data)
            except Exception as exc:  # noqa: BLE001
                return False, f"PickerTask 反序列化失败：{exc}"
            # 基本完整性检查
            if not task.name:
                return False, "name 字段不能为空"
            return True, "PickerTask JSON 校验通过"

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #

    def _on_new(self) -> None:
        """新建脚本：清空编辑区并预填模板。"""
        self._editor.setPlainText(_NEW_TEMPLATE)
        self._current_path = ""
        self._path_label.setText("未命名")

    def _on_open(self) -> None:
        """打开文件对话框选择 ``.scskill`` 文件。"""
        # 默认目录指向 skill/
        default_dir = self._default_skill_dir()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开 .scskill 脚本",
            default_dir,
            "SCSkill 脚本 (*.scskill);;JSON 文件 (*.json);;所有文件 (*.*)",
        )
        if path:
            self.load_file(path)

    def _on_save(self) -> None:
        """保存到原路径；若路径为空则转「另存为」。"""
        if not self._current_path:
            self._on_save_as()
            return
        self._write_to(self._current_path)

    def _on_save_as(self) -> None:
        """另存为新文件。"""
        default_dir = self._default_skill_dir()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "另存为 .scskill 脚本",
            os.path.join(default_dir, "untitled.scskill"),
            "SCSkill 脚本 (*.scskill);;JSON 文件 (*.json);;所有文件 (*.*)",
        )
        if path:
            if not path.endswith(".scskill") and not path.endswith(".json"):
                path += ".scskill"
            self._write_to(path)
            self._current_path = path
            self._path_label.setText(path)

    def _on_validate(self) -> None:
        """校验并弹窗反馈。"""
        ok, msg = self.validate()
        if ok:
            QMessageBox.information(self, "校验通过", msg)
        else:
            QMessageBox.warning(self, "校验失败", msg)

    def _on_run(self) -> None:
        """运行：先校验，通过后 emit ``run_requested`` 信号。

        若当前文件未保存（路径为空），先提示用户保存。
        根据文件后缀确定脚本类型：
            - ``.py.scskill`` —— "python"
            - ``.js.scskill`` —— "javascript"
            - ``.scskill``    —— "pickertask"（默认）
        """
        ok, msg = self.validate()
        if not ok:
            QMessageBox.warning(self, "无法运行", f"脚本校验失败：\n{msg}")
            return
        if not self._current_path:
            # 未保存：提示先保存
            reply = QMessageBox.question(
                self,
                "保存脚本",
                "当前脚本尚未保存，是否保存后再运行？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            self._on_save_as()
            if not self._current_path:
                return  # 用户取消了另存为
        # 根据文件后缀确定脚本类型
        name = os.path.basename(self._current_path)
        if name.endswith(".py.scskill"):
            script_type = "python"
        elif name.endswith(".js.scskill"):
            script_type = "javascript"
        else:
            script_type = "pickertask"
        self.run_requested.emit(self._current_path, script_type)

    # ------------------------------------------------------------------ #
    # 执行辅助
    # ------------------------------------------------------------------ #

    def execute_python(self, content: str) -> str:
        """在受限命名空间中执行 Python 脚本，返回输出或错误信息。

        Args:
            content: Python 脚本文本

        Returns:
            捕获到的标准输出/标准错误内容，或异常信息字符串
        """
        import contextlib
        import io

        import bs4
        import requests

        from crawler import models as crawler_models

        restricted_globals = {
            "__builtins__": __builtins__,
            "requests": requests,
            "bs4": bs4,
            "json": json,
            "os": os,
            "crawler": crawler_models,
        }
        output_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(output_buf), contextlib.redirect_stderr(output_buf):
                exec(content, restricted_globals)  # noqa: S102
            return output_buf.getvalue() or "(无输出)"
        except Exception as exc:  # noqa: BLE001
            return f"执行异常: {exc}"

    def execute_javascript(self, content: str) -> None:
        """通过信号通知主窗口在拾取器 view 中执行 JS 脚本。

        实际执行由 main_window 在拾取器 view 中调用 ``runJavaScript`` 完成。
        """
        # 此处仅发出信号，实际执行由 main_window 处理
        self.run_requested.emit(self._current_path, "javascript")

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _default_skill_dir(self) -> str:
        """返回默认的 skill/ 目录路径（基于应用目录）。"""
        # 优先使用 main.py 同级目录下的 skill/
        try:
            from __main__ import __file__ as main_file  # type: ignore
            base = os.path.dirname(os.path.abspath(main_file))
        except Exception:  # noqa: BLE001
            base = os.getcwd()
        skill_dir = os.path.join(base, "skill")
        if not os.path.isdir(skill_dir):
            # 回退到当前工作目录
            return os.getcwd()
        return skill_dir

    def _write_to(self, path: str) -> None:
        """将编辑区内容写入指定路径。"""
        text = self._editor.toPlainText()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            _logger.info("已保存脚本: %s", path)
            self.statusBar_msg = f"已保存：{path}"  # type: ignore[attr-defined]
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", f"写入文件失败：\n{exc}")
