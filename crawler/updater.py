"""GitHub 更新与文件补全模块。

通过 GitHub 仓库检查更新并补全本地缺失文件。所有网络操作均有超时与
异常捕获，失败时静默返回 False，不抛异常，不阻断应用启动。
"""

from __future__ import annotations

import logging
import os
import re
from functools import cmp_to_key
from typing import Callable, Optional

import requests

# GitHub 仓库信息
REPO_OWNER = "secret-Alan"
REPO_NAME = "InternetCrawler"
# 仓库默认分支（仅作为回退）
DEFAULT_BRANCH = "main"
# 版本分支命名规则：vX.Y.Z
VERSION_BRANCH_REGEX = r"^v\d+\.\d+\.\d+$"
# GitHub API 基址
GITHUB_API = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"
# raw 文件基址（用于直接下载文件内容，默认指向 main 分支）
RAW_BASE = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{DEFAULT_BRANCH}"
# 本地版本文件名
VERSION_FILE = "version.txt"
# 网络请求超时（秒）
REQUEST_TIMEOUT = 10
# 需要检查/补全的关键文件清单（相对仓库根的路径）
# 列出应用运行所需的关键资源与模块文件
CRITICAL_FILES = [
    "app.ico",
    "main.py",
    "requirements.txt",
    "crawler/__init__.py",
    "crawler/models.py",
    "crawler/downloader.py",
    "crawler/parser.py",
    "crawler/pipeline.py",
    "crawler/scheduler.py",
    "crawler/engine.py",
    "crawler/robots.py",
    "crawler/url_management.py",
    "crawler/updater.py",
    "ui/__init__.py",
    "ui/main_window.py",
    "ui/config_panel.py",
    "ui/widgets.py",
    "ui/log_view.py",
    "ui/result_table.py",
    "ui/stats_view.py",
    "ui/help_window.py",
    "ui/splash.py",
]

# 进度回调类型：(进度百分比 0-100, 状态文本) -> None
ProgressCallback = Callable[[int, str], None]


class GitHubUpdater:
    """GitHub 更新器：检查版本与补全缺失文件。"""

    def __init__(self, app_dir: str, logger: Optional[logging.Logger] = None,
                 progress: Optional[ProgressCallback] = None) -> None:
        self._app_dir = os.path.abspath(app_dir)
        self._logger = logger or logging.getLogger("crawler.updater")
        self._progress = progress or (lambda pct, msg: None)
        # 当前补全文件所用的目标分支（版本分支或 main 回退）
        self._target_branch = DEFAULT_BRANCH

    def _local_version(self) -> str:
        """读取本地版本号（<app_dir>/version.txt）。不存在返回 '0.0.0'。"""
        try:
            path = os.path.join(self._app_dir, VERSION_FILE)
            if not os.path.isfile(path):
                return "0.0.0"
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            return content or "0.0.0"
        except Exception as e:  # noqa: BLE001
            self._logger.debug("读取本地版本失败: %s", e)
            return "0.0.0"

    def _list_version_branches(self) -> list[str]:
        """枚举仓库中所有符合 vX.Y.Z 命名的分支。
        任何异常或非 200 响应均返回空列表，不抛异常。"""
        try:
            resp = requests.get(
                f"{GITHUB_API}/branches",
                params={"per_page": 100},
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/vnd.github+json"},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not isinstance(data, list):
                return []
            names = [b.get("name", "") for b in data if isinstance(b, dict)]
            return [n for n in names if n and re.fullmatch(VERSION_BRANCH_REGEX, n)]
        except Exception as e:  # noqa: BLE001
            self._logger.debug("列举版本分支失败: %s", e)
            return []

    def select_latest_branch(self, branches: list[str]) -> str:
        """从分支名列表中选择语义版本最高的分支。
        空列表返回 'main' 并记录警告。"""
        if not branches:
            self._logger.warning("未找到 vX.Y.Z 版本分支，回退 main 分支补全文件")
            return DEFAULT_BRANCH

        def _parse(name: str) -> tuple[int, int, int]:
            body = name[1:] if name.lower().startswith("v") else name
            parts = body.split(".")
            try:
                return (int(parts[0]), int(parts[1]), int(parts[2]))
            except (ValueError, IndexError):
                return (0, 0, 0)

        best = branches[0]
        best_ver = _parse(best)
        for b in branches[1:]:
            v = _parse(b)
            if v > best_ver:
                best = b
                best_ver = v
        return best

    def _remote_version(self) -> Optional[str]:
        """获取远程版本号。
        优先从 vX.Y.Z 版本分支中选取最高版本；若无版本分支，回退到
        GitHub releases/latest 的 tag_name 或 raw version.txt。
        同时将所选分支写入 self._target_branch 供文件补全使用。
        失败返回 None。"""
        # 1) 优先：版本分支 vX.Y.Z
        try:
            branches = self._list_version_branches()
            if branches:
                latest = self.select_latest_branch(branches)
                self._target_branch = latest
                version = latest[1:] if latest.lower().startswith("v") else latest
                if version:
                    return version
        except Exception as e:  # noqa: BLE001
            self._logger.debug("解析版本分支失败: %s", e)

        # 回退到 main 分支
        self._target_branch = DEFAULT_BRANCH

        # 2) GitHub releases/latest 的 tag_name
        try:
            resp = requests.get(
                f"{GITHUB_API}/releases/latest",
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/vnd.github+json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                tag = data.get("tag_name")
                if tag:
                    tag = tag.strip()
                    if tag.lower().startswith("v"):
                        tag = tag[1:]
                    if tag:
                        return tag
        except Exception as e:  # noqa: BLE001
            self._logger.debug("获取 GitHub release 版本失败: %s", e)

        # 3) raw version.txt（位于 main 分支）
        try:
            resp = requests.get(
                f"{RAW_BASE}/{VERSION_FILE}",
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                text = resp.text.strip()
                if text:
                    if text.lower().startswith("v"):
                        text = text[1:]
                    return text
        except Exception as e:  # noqa: BLE001
            self._logger.debug("获取 raw version.txt 失败: %s", e)

        return None

    @staticmethod
    def _compare_versions(v1: str, v2: str) -> int:
        """比较两个语义版本号（如 '1.2.3'）。返回 >0 表示 v1>v2，<0 表示 v1<v2，0 表示相等。
        容错：非数字段按 0 处理。"""
        def _split(v: str) -> list[int]:
            parts: list[int] = []
            for seg in (v or "").split("."):
                try:
                    parts.append(int(seg.strip()))
                except (ValueError, TypeError):
                    parts.append(0)
            return parts

        p1 = _split(v1)
        p2 = _split(v2)
        # 对齐长度
        length = max(len(p1), len(p2))
        p1 += [0] * (length - len(p1))
        p2 += [0] * (length - len(p2))
        for a, b in zip(p1, p2):
            if a > b:
                return 1
            if a < b:
                return -1
        return 0

    def check_update(self) -> tuple[bool, Optional[str]]:
        """检查是否有更新。返回 (has_update, remote_version)。
        远程版本不可获取时返回 (False, None)。"""
        remote = self._remote_version()
        if not remote:
            return (False, None)
        local = self._local_version()
        has_update = self._compare_versions(remote, local) > 0
        return (has_update, remote)

    def _file_exists_locally(self, rel_path: str) -> bool:
        """检查本地是否存在某文件（且非空）。"""
        try:
            path = os.path.join(self._app_dir, rel_path)
            return os.path.isfile(path) and os.path.getsize(path) > 0
        except Exception as e:  # noqa: BLE001
            self._logger.debug("检查本地文件存在性失败 (%s): %s", rel_path, e)
            return False

    def _download_file(self, rel_path: str) -> bool:
        """从仓库下载单个文件到本地对应路径。成功返回 True。
        创建必要的父目录；失败返回 False。使用 self._target_branch 指定分支。"""
        url = (
            f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/"
            f"{self._target_branch}/{rel_path}"
        )
        local_path = os.path.join(self._app_dir, rel_path)
        try:
            parent = os.path.dirname(local_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(resp.content)
            self._logger.info("补全文件成功: %s", rel_path)
            return True
        except Exception as e:  # noqa: BLE001
            self._logger.warning("下载文件失败 (%s): %s", rel_path, e)
            return False

    def repair_missing_files(self) -> int:
        """检查并补全缺失的关键文件。返回补全的文件数。"""
        total = len(CRITICAL_FILES)
        if total == 0:
            return 0
        repaired = 0
        processed = 0
        for rel_path in CRITICAL_FILES:
            processed += 1
            if self._file_exists_locally(rel_path):
                pct = int(processed / total * 100)
                self._progress(pct, f"已存在: {rel_path}")
                continue
            pct = int((processed - 1) / total * 100)
            self._progress(pct, f"补全文件: {rel_path}")
            if self._download_file(rel_path):
                repaired += 1
            pct = int(processed / total * 100)
            self._progress(pct, f"已处理: {rel_path}")
        self._progress(100, f"文件检查完成，补全 {repaired} 个文件")
        return repaired

    def fetch_readme(self) -> str:
        """下载 README.md（main 分支）。成功则缓存到本地并返回文本；
        失败时回退到本地缓存；缓存不存在则返回内置默认说明。永不抛异常。"""
        url = (
            f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/"
            f"{DEFAULT_BRANCH}/README.md"
        )
        local_path = os.path.join(self._app_dir, "README.md")
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                text = resp.text
                try:
                    with open(local_path, "w", encoding="utf-8") as f:
                        f.write(text)
                except Exception as e:  # noqa: BLE001
                    self._logger.debug("写入本地 README 缓存失败: %s", e)
                return text
        except Exception as e:  # noqa: BLE001
            self._logger.debug("下载 README 失败: %s", e)
        # 回退到本地缓存
        try:
            if os.path.isfile(local_path):
                with open(local_path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception as e:  # noqa: BLE001
            self._logger.debug("读取本地 README 缓存失败: %s", e)
        # 内置默认说明
        return (
            "# 专业网络爬虫\n\n"
            "请阅读本软件的使用说明与免责声明。\n\n"
            "使用本软件即表示您同意遵守相关法律法规与网站使用条款。\n"
        )

    def fetch_update_log(self, from_version: str,
                         to_version: str) -> list[tuple[str, str]]:
        """获取版本范围内的更新日志。
        返回 (version_string, markdown_content) 元组列表，按版本降序排列。
        版本范围 (from_version, to_version] —— 严格大于 from_version，
        小于等于 to_version。永不抛异常。"""
        results: list[tuple[str, str]] = []
        try:
            versions: list[str] = []
            branches = self._list_version_branches()
            for b in branches:
                ver = b[1:] if b.lower().startswith("v") else b
                if not ver:
                    continue
                # 严格大于 from_version，且 <= to_version
                if (self._compare_versions(ver, from_version) > 0
                        and self._compare_versions(ver, to_version) <= 0):
                    versions.append(ver)
            if not versions:
                # 回退：无版本分支时仅尝试 to_version 本身
                if to_version:
                    versions = [to_version]
            # 去重并按版本降序排序
            versions = sorted(
                set(versions),
                key=cmp_to_key(self._compare_versions),
                reverse=True,
            )
            for ver in versions:
                url = (
                    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/"
                    f"{DEFAULT_BRANCH}/what_is_upgraded-v{ver}.md"
                )
                try:
                    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
                    if resp.status_code == 200:
                        results.append((ver, resp.text))
                except Exception as e:  # noqa: BLE001
                    self._logger.debug("下载更新日志失败 (v%s): %s", ver, e)
        except Exception as e:  # noqa: BLE001
            self._logger.debug("获取更新日志失败: %s", e)
        return results

    def fetch_contributors(self) -> str:
        """下载 contributors.txt（main 分支）。成功则缓存到本地并返回文本；
        失败时回退到本地缓存；缓存不存在则返回空字符串。永不抛异常。"""
        url = (
            f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/"
            f"{DEFAULT_BRANCH}/contributors.txt"
        )
        local_path = os.path.join(self._app_dir, "contributors.txt")
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                text = resp.text
                try:
                    with open(local_path, "w", encoding="utf-8") as f:
                        f.write(text)
                except Exception as e:  # noqa: BLE001
                    self._logger.debug("写入本地 contributors 缓存失败: %s", e)
                return text
        except Exception as e:  # noqa: BLE001
            self._logger.debug("下载 contributors 失败: %s", e)
        try:
            if os.path.isfile(local_path):
                with open(local_path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception as e:  # noqa: BLE001
            self._logger.debug("读取本地 contributors 缓存失败: %s", e)
        return ""

    def is_ignored_version(self, version: str) -> bool:
        """检查某版本是否在忽略列表中。任何异常返回 False。"""
        try:
            path = os.path.join(self._app_dir, "ignored_versions.txt")
            if not os.path.isfile(path):
                return False
            with open(path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f.readlines()]
            return version in lines
        except Exception as e:  # noqa: BLE001
            self._logger.debug("读取忽略版本列表失败: %s", e)
            return False

    def ignore_version(self, version: str) -> None:
        """将某版本追加到忽略列表（每行一个）。文件不存在则创建。永不抛异常。"""
        try:
            path = os.path.join(self._app_dir, "ignored_versions.txt")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{version}\n")
        except Exception as e:  # noqa: BLE001
            self._logger.debug("写入忽略版本失败: %s", e)

    def get_last_version(self) -> str:
        """读取上次记录的版本号。文件不存在或为空返回 '0.0.0'。永不抛异常。"""
        try:
            path = os.path.join(self._app_dir, "last_version.txt")
            if not os.path.isfile(path):
                return "0.0.0"
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            return content or "0.0.0"
        except Exception as e:  # noqa: BLE001
            self._logger.debug("读取上次版本失败: %s", e)
            return "0.0.0"

    def save_last_version(self, version: str) -> None:
        """记录版本号到 last_version.txt（覆盖写入）。永不抛异常。"""
        try:
            path = os.path.join(self._app_dir, "last_version.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(version)
        except Exception as e:  # noqa: BLE001
            self._logger.debug("保存上次版本失败: %s", e)

    def run(self) -> dict:
        """执行完整更新流程：检查版本 + 检查忽略 + 拉取贡献者 + 补全缺失文件。
        返回 dict: {"has_update": bool, "remote_version": str|None, "ignored": bool}。
        不会自动下载更新文件，仅补全缺失的关键文件。任何异常都不抛出。"""
        try:
            self._progress(0, "开始检查更新...")
            has_update, remote_version = self.check_update()
            was_ignored = False
            if has_update and remote_version:
                if self.is_ignored_version(remote_version):
                    was_ignored = True
                    has_update = False
                    self._progress(15, f"版本 {remote_version} 已被忽略")
                else:
                    self._progress(20, f"发现新版本: {remote_version}")
            else:
                self._progress(20, "版本检查完成")
            # 静默拉取贡献者列表（best-effort）
            try:
                self.fetch_contributors()
            except Exception as e:  # noqa: BLE001
                self._logger.debug("拉取贡献者失败: %s", e)
            self._progress(30, "开始检查缺失文件...")
            try:
                self.repair_missing_files()
            except Exception as e:  # noqa: BLE001
                self._logger.debug("补全缺失文件失败: %s", e)
            self._progress(100, "更新流程完成")
            return {
                "has_update": has_update,
                "remote_version": remote_version,
                "ignored": was_ignored,
            }
        except Exception as e:  # noqa: BLE001
            self._logger.error("更新流程异常: %s", e)
            return {
                "has_update": False,
                "remote_version": None,
                "ignored": False,
            }
