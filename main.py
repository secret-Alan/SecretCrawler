"""应用入口：启动专业网络爬虫 GUI（Task 16）。

运行方式：
    python main.py

若缺少依赖（PySide6 / requests / bs4 / lxml / openpyxl），将以纯文本形式
打印缺失项并退出码 1；若依赖齐全则启动 PySide6 主窗口。

打包（PyInstaller）见仓库根目录的 ``build.py``。
"""
from __future__ import annotations

import os
import shutil
import sys


# ---------------------------------------------------------------------------
# 资源路径解析：兼容源码运行与 PyInstaller 冻结运行
# ---------------------------------------------------------------------------
def _app_dir() -> str:
    """应用所在目录。

    - 源码运行：``main.py`` 所在目录
    - PyInstaller 冻结：``Crawler.exe`` 所在目录
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resource_path(rel: str) -> str:
    """打包内嵌资源绝对路径。

    源码运行时指向项目根目录；PyInstaller 冻结后指向 ``sys._MEIPASS``
    （运行期临时解压目录）。
    """
    base = getattr(sys, "_MEIPASS", _app_dir())
    return os.path.join(base, rel)


def _ensure_examples_dir() -> None:
    """首次启动时若 ``<app_dir>/examples`` 缺失，从打包资源中还原。

    仅在目标目录不存在时执行；用户已删改的本地 ``examples/`` 不会被覆盖。
    """
    target = os.path.join(_app_dir(), "examples")
    if os.path.isdir(target):
        return
    bundled = _resource_path("examples")
    if not os.path.isdir(bundled):
        return
    os.makedirs(target, exist_ok=True)
    for name in os.listdir(bundled):
        src = os.path.join(bundled, name)
        dst = os.path.join(target, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    print(f"[startup] 已从内置资源恢复 {target}")


def _ensure_skill_dir() -> None:
    """首次启动时确保 ``<app_dir>/skill`` 目录存在。

    用于存放用户导出 / 导入的 ``.scskill`` 脚本文件。仅在目录缺失时创建，
    不会覆盖用户已有的脚本。
    """
    target = os.path.join(_app_dir(), "skill")
    os.makedirs(target, exist_ok=True)


# ---------------------------------------------------------------------------
# 依赖检查
# ---------------------------------------------------------------------------
def _check_dependencies() -> list[str]:
    """返回缺失的依赖模块名列表。

    使用 ``__import__`` 逐个探测；任一导入失败即记入缺失列表。
    """
    missing: list[str] = []
    for mod in ("PySide6", "requests", "bs4", "lxml", "openpyxl"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    return missing


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main() -> int:
    """应用主函数：检查依赖后启动 GUI。"""
    missing = _check_dependencies()
    if missing:
        # 友好提示（不依赖 GUI，因为 PySide6 可能正是缺失项）
        print(f"缺少依赖：{', '.join(missing)}")
        print("请运行：pip install -r requirements.txt")
        return 1

    # 延迟导入：仅当依赖齐全时才加载 PySide6，避免在缺失依赖时产生额外报错
    from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon, QMenu
    from PySide6.QtGui import QIcon, QAction
    from PySide6.QtCore import QObject, QThread, Signal, QEventLoop, QTimer, Qt
    import threading

    # Windows 任务栏：把当前进程注册为独立 AppUserModelID，
    # 这样任务栏图标不会和 python.exe 合并显示，自定义图标才能生效。
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Crawler.InternetBug.1"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("专业网络爬虫")
    app.setOrganizationName("Crawler")

    # 应用图标：优先用打包内嵌的 app.ico，源码运行时回退到项目根目录
    icon_path = _resource_path("app.ico")
    if os.path.isfile(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    from ui.splash import SplashScreen

    # 立即创建并显示闪屏（在所有其他初始化之前，让用户第一时间看到反馈）
    splash = SplashScreen(icon_path)
    splash.set_status("正在初始化…")
    splash.set_startup_progress(5)
    # 用户点击闪屏右上角 ✕ 时直接退出应用
    splash.close_requested.connect(lambda: sys.exit(0))
    splash.show()
    app.processEvents()  # 强制刷新 UI，确保闪屏立即绘制

    # 后台线程结果共享容器（主线程在闪屏关闭后读取）
    update_result: dict = {}     # 更新检查结果: {has_update, remote_version, ignored}
    readme_content: dict = {}    # README 抓取结果: {content: str}

    # 后台更新检查 Worker：在专用线程中执行 GitHubUpdater.run()，
    # 通过 Qt 信号把进度回调投递回主线程更新闪屏。
    # 类定义放在 main() 内部，确保模块顶层不强制依赖 PySide6。
    class _UpdateWorker(QObject):
        """在后台线程执行 GitHub 更新检查，通过信号回传进度与结果。"""

        progress = Signal(int, str)   # (pct, message)
        finished = Signal(dict)       # result dict

        def __init__(self, app_dir: str) -> None:
            super().__init__()
            self._app_dir = app_dir

        def run(self) -> None:
            from crawler.updater import GitHubUpdater

            updater = GitHubUpdater(
                self._app_dir,
                progress=lambda pct, msg: self.progress.emit(pct, msg),
            )
            result = updater.run()
            self.finished.emit(result)

    splash.set_startup_progress(15)
    app.processEvents()

    # 启动后台更新检查线程（非阻塞，不等待它完成）
    update_worker = _UpdateWorker(_app_dir())
    update_thread = QThread()
    update_worker.moveToThread(update_thread)
    # set_update_progress 只接收 int，这里适配 (int, str) 信号同时刷新进度条与状态。
    # 闪屏关闭（销毁）后通过 _splash_alive 标志停止转发，避免访问已删除的 C++ 对象。
    _splash_alive = [True]

    def _on_update_progress(pct: int, msg: str) -> None:
        if _splash_alive[0]:
            splash.set_update_progress(pct)
            splash.set_status(msg)

    update_worker.progress.connect(_on_update_progress)
    # worker.run() 返回后：存储结果 + 让线程退出事件循环
    update_worker.finished.connect(lambda r: update_result.update(r))
    update_worker.finished.connect(update_thread.quit)
    update_thread.started.connect(update_worker.run)
    update_thread.start()

    # 启动后台 README 抓取线程（刷新本地缓存，不阻塞主线程）
    def _fetch_readme_bg() -> None:
        from crawler.updater import GitHubUpdater

        try:
            u = GitHubUpdater(_app_dir())
            readme_content["content"] = u.fetch_readme()
        except Exception:
            pass

    readme_thread = threading.Thread(target=_fetch_readme_bg, daemon=True)
    readme_thread.start()

    splash.set_startup_progress(30)
    splash.set_status("正在加载主窗口…")
    app.processEvents()

    _ensure_examples_dir()
    _ensure_skill_dir()

    splash.set_startup_progress(60)
    app.processEvents()

    from ui.main_window import MainWindow

    window = MainWindow()

    splash.set_startup_progress(100)
    splash.set_status("就绪")
    app.processEvents()

    # 闪屏关闭后由 destroyed 信号驱动后续启动流程，避免 QEventLoop 轮询阻塞主线程。
    # _splash_closed_handled 确保 destroyed 在边缘场景多次发射时回调只执行一次；
    # _splash_alive 在回调入口置 False，使后台进度信号不再转发到已销毁的闪屏。
    _splash_closed_handled = [False]

    def _on_splash_closed() -> None:
        if _splash_closed_handled[0]:
            return
        _splash_closed_handled[0] = True
        _splash_alive[0] = False

        # --- 创建系统托盘 ---
        tray = QSystemTrayIcon(QIcon(icon_path), app)
        tray.setToolTip("专业网络爬虫")
        tray.show()

        # 托盘菜单
        tray_menu = QMenu()
        show_action = tray_menu.addAction("显示主窗口")
        pause_action = tray_menu.addAction("暂停任务")
        resume_action = tray_menu.addAction("继续任务")
        stop_action = tray_menu.addAction("停止任务")
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction("退出")
        tray.setContextMenu(tray_menu)

        # 菜单信号
        show_action.triggered.connect(lambda: (window.showNormal(), window.activateWindow()))
        pause_action.triggered.connect(lambda: window._on_pause_toggle() if window._engine else None)
        resume_action.triggered.connect(lambda: window._on_pause_toggle() if window._engine else None)
        stop_action.triggered.connect(lambda: window._on_stop() if window._engine else None)
        quit_action.triggered.connect(app.quit)

        # 双击托盘图标恢复主窗口
        tray.activated.connect(
            lambda reason: (window.showNormal(), window.activateWindow())
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick
            else None
        )

        # 点击通知消息恢复主窗口
        tray.messageClicked.connect(lambda: (window.showNormal(), window.activateWindow()))

        # 警告时弹出托盘通知
        window.warning_triggered.connect(
            lambda title, msg: tray.showMessage(title, msg, QSystemTrayIcon.MessageLevel.Warning, 5000)
        )

        # 托盘状态切换（简化实现：切换 tooltip 文字）
        def _on_tray_state_changed(state: str) -> None:
            if state == "active":
                tray.setToolTip("● 运行中 - 专业网络爬虫")
            elif state == "warning":
                tray.setToolTip("▲ 警告 - 专业网络爬虫")
            else:
                tray.setToolTip("专业网络爬虫")

        window.tray_state_changed.connect(_on_tray_state_changed)

        # 保持托盘引用，避免被 GC 回收
        window._tray = tray

        # --- 显示主窗口（先于用户协议，使主窗口与协议窗口同时可见） ---
        # 锁定爬取：在用户协议接受前禁止开始爬取（开始按钮禁用）
        window.set_crawl_locked(True)
        window.show()
        app.processEvents()

        # --- 用户协议 ---
        from crawler.updater import GitHubUpdater
        from ui.agreement_window import AgreementWindow

        updater = GitHubUpdater(_app_dir())
        agreement = AgreementWindow()
        # 优先使用后台线程已抓取的 README；未完成则同步抓取（读本地缓存或下载）
        if "content" in readme_content:
            readme_text = readme_content["content"]
        else:
            readme_text = updater.fetch_readme()
        agreement.set_content(readme_text)
        if agreement.exec() == QDialog.DialogCode.Rejected:
            sys.exit(0)
        # 用户已同意，解锁爬取
        window.set_crawl_locked(False)

        # --- 更新内容（版本变更时展示） ---
        last_ver = updater.get_last_version()
        current_ver = updater._local_version()
        if (current_ver != last_ver
                and updater._compare_versions(current_ver, last_ver) > 0):
            logs = updater.fetch_update_log(last_ver, current_ver)
            if logs:
                from ui.update_log_window import UpdateLogWindow

                log_window = UpdateLogWindow()
                log_window.set_logs(logs)
                log_window.exec()
            updater.save_last_version(current_ver)

        # 等待后台更新检查线程完成（带超时），以便读取 update_result
        # （此为有界的短时等待，不阻塞启动；保留既有 QEventLoop 模式）
        app.processEvents()
        if update_thread.isRunning():
            _update_wait = QEventLoop()
            _update_timeout = QTimer()
            _update_timeout.setSingleShot(True)
            _update_timeout.timeout.connect(_update_wait.quit)
            update_worker.finished.connect(_update_wait.quit)
            _update_timeout.start(3000)
            _update_wait.exec()
            _update_timeout.stop()
            app.processEvents()

        # --- 更新提示（有新版本且未被忽略时展示） ---
        has_update = update_result.get("has_update", False)
        remote_version = update_result.get("remote_version")
        if (has_update and remote_version
                and not updater.is_ignored_version(remote_version)):
            from ui.update_prompt_window import UpdatePromptWindow

            prompt = UpdatePromptWindow()
            prompt.set_version_info(remote_version)
            prompt.exec()
            choice = prompt.get_choice()
            if choice == UpdatePromptWindow.CHOICE_IGNORE:
                updater.ignore_version(remote_version)
            elif choice == UpdatePromptWindow.CHOICE_UPDATE:
                from ui.update_progress_window import UpdateProgressWindow
                from crawler.updater import CRITICAL_FILES

                progress_win = UpdateProgressWindow()
                progress_win.show()

                def _do_update() -> None:
                    u = GitHubUpdater(_app_dir())
                    if remote_version:
                        u._target_branch = f"v{remote_version}"
                    total = len(CRITICAL_FILES)
                    for i, f in enumerate(CRITICAL_FILES):
                        progress_win.set_progress(int(i / total * 100), f)
                        u._download_file(f)
                    progress_win.set_progress(100, "完成")
                    progress_win.show_success()

                def _on_restart() -> None:
                    try:
                        python = sys.executable
                        os.execv(python, [python] + sys.argv)
                    except Exception:
                        pass
                    sys.exit(0)

                progress_win.restart_requested.connect(_on_restart)
                threading.Thread(target=_do_update, daemon=True).start()

    # 闪屏启用「关闭即销毁」，使 destroyed 信号在闪屏关闭时发射；
    # 在 set_ready 之前连接 destroyed，确保信号不丢失。
    # set_ready 触发闪屏在至少显示 5 秒后自行关闭。
    splash.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    splash.destroyed.connect(lambda: _on_splash_closed())
    splash.set_ready()

    # 主窗口已于用户协议前显示，无需再次显示
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
