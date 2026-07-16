"""浏览器启动器 —— 通过 subprocess 启动 Chrome / Edge 浏览器进程。

本模块用于「通过网页爬寻」功能中，当用户选择 Google Chrome 或 Microsoft Edge
时，通过 ``subprocess.Popen`` 启动对应浏览器进程并附加远程调试参数。

公共 API:
    launch_browser(browser_type, url) -> subprocess.Popen | None
    find_browser_path(browser_type) -> str | None
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Optional


_logger = logging.getLogger("crawler.browser_launcher")


# Windows 默认安装路径（按优先级排序）
_CHROME_PATHS_WIN = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

_EDGE_PATHS_WIN = [
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def _find_via_registry(exe_name: str) -> Optional[str]:
    """通过 Windows 注册表 App Paths 探测浏览器路径。

    查询 ``HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\<exe_name>``
    的 ``(Default)`` 值。失败时返回 None。
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _ = winreg.QueryValueEx(key, None)
            if value and os.path.isfile(value):
                return value
    except OSError:
        pass
    except Exception as exc:  # noqa: BLE001
        _logger.debug("注册表探测 %s 失败: %s", exe_name, exc)
    return None


def _find_via_path(exe_name: str) -> Optional[str]:
    """通过 PATH 环境变量查找可执行文件。"""
    path = shutil.which(exe_name)
    return path if path and os.path.isfile(path) else None


def find_browser_path(browser_type: str) -> Optional[str]:
    """探测指定浏览器类型的可执行文件路径。

    探测顺序：Windows 注册表 → 默认安装路径 → PATH 环境变量。

    Args:
        browser_type: 浏览器类型，``"chrome"`` / ``"edge"`` / ``"secretcrawler"``。

    Returns:
        可执行文件绝对路径；未找到返回 None。``secretcrawler`` 固定返回 None
        （由调用方使用内嵌浏览器）。
    """
    bt = browser_type.lower()
    if bt == "secretcrawler":
        return None
    if bt == "chrome":
        exe_name = "chrome.exe" if sys.platform == "win32" else "google-chrome"
        paths = _CHROME_PATHS_WIN if sys.platform == "win32" else []
    elif bt == "edge":
        exe_name = "msedge.exe" if sys.platform == "win32" else "microsoft-edge"
        paths = _EDGE_PATHS_WIN if sys.platform == "win32" else []
    else:
        return None

    # 1. 注册表
    path = _find_via_registry(exe_name)
    if path:
        return path
    # 2. 默认安装路径
    for p in paths:
        if os.path.isfile(p):
            return p
    # 3. PATH
    return _find_via_path(exe_name)


def launch_browser(browser_type: str, url: str) -> Optional[subprocess.Popen]:
    """启动指定浏览器进程并打开目标 URL。

    附加 ``--remote-debugging-port=9222`` 和 ``--user-data-dir=<temp>`` 参数，
    便于后续通过 DevTools Protocol 操控。

    Args:
        browser_type: ``"chrome"`` / ``"edge"`` / ``"secretcrawler"``。
        url: 启动时打开的目标 URL。

    Returns:
        启动成功返回 ``subprocess.Popen`` 实例；失败（浏览器未安装或启动异常）
        返回 None，由调用方提示用户并回退到内嵌浏览器。
    """
    bt = browser_type.lower()
    if bt == "secretcrawler":
        _logger.debug("browser_type=secretcrawler，跳过外部启动")
        return None

    exe_path = find_browser_path(bt)
    if not exe_path:
        _logger.warning("未找到浏览器可执行文件: %s", bt)
        return None

    # 独立的临时用户数据目录，避免污染用户主配置
    user_data_dir = tempfile.mkdtemp(prefix=f"ic_{bt}_")
    args = [
        exe_path,
        "--remote-debugging-port=9222",
        f"--user-data-dir={user_data_dir}",
        url,
    ]
    try:
        # 使用 DETACHED_PROCESS 避免子进程随主进程退出（Windows）
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            args,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _logger.info("已启动 %s (pid=%s) 打开 %s", bt, proc.pid, url)
        return proc
    except (FileNotFoundError, OSError) as exc:
        _logger.warning("启动浏览器 %s 失败: %s", bt, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        _logger.exception("启动浏览器 %s 时发生未知异常", bt)
        return None
