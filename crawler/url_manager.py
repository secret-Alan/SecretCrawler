"""URL 管理器：待抓队列、去重、URL 规范化与过滤。

本模块不依赖 PySide6（UI-free），实现 spec 中「URL 管理器」需求：

- 内存去重（set）+ 可选 SQLite 持久化去重（重启可恢复）
- URL 规范化（去除 fragment、相对转绝对、补全协议、小写 scheme/host、
  去默认端口、合并末尾斜杠）
- URL 过滤：正则白名单、正则黑名单、限定域名（allowed_domains 与
  restrict_to_start_domains）
- 广度优先队列（collections.deque），按深度出队
- 入队/出队线程安全（threading.Lock）
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from collections import deque
from urllib.parse import SplitResult, urlsplit, urlunsplit

from crawler.models import CrawlConfig

# 需要直接跳过的非爬取协议
_SKIP_SCHEMES = {"mailto", "javascript", "tel", "data"}

# 各协议的默认端口（规范化时去除）
_DEFAULT_PORTS = {"http": 80, "https": 443}


class URLManager:
    """URL 管理器：维护待抓队列与已发现集合，支持规范化、去重、过滤。

    线程安全：所有对内部队列和去重集合的读写均通过 ``threading.Lock``
    保护，可在多线程调度器中安全使用。

    Args:
        config: 爬虫配置（CrawlConfig）。
        logger: 可选日志器，默认 ``logging.getLogger("crawler.url_manager")``。
    """

    def __init__(self, config: CrawlConfig, logger: logging.Logger | None = None):
        self._config = config
        self._logger = logger or logging.getLogger("crawler.url_manager")

        # 待抓队列：广度优先，popleft 出队、append 入队
        self._queue: deque[tuple[str, int]] = deque()
        # 已发现 URL 集合（去重，内存）
        self._seen: set[str] = set()
        # 已发现 URL 总数（成功加入 seen 的唯一 URL 数，含重启恢复）
        self._discovered: int = 0
        # 线程安全锁，保护 _queue / _seen / _discovered
        self._lock = threading.Lock()

        # 预编译正则表达式，避免每次 add 时重复编译
        self._whitelist_patterns = [re.compile(p) for p in config.url_whitelist_regex]
        self._blacklist_patterns = [re.compile(p) for p in config.url_blacklist_regex]

        # 预计算允许域名集合与起始域名集合（均小写）
        self._allowed_domains: set[str] = {d.lower() for d in config.allowed_domains}
        self._start_hosts: set[str] = set()
        if config.start_urls:
            for url in config.start_urls:
                host = urlsplit(url).hostname
                if host:
                    self._start_hosts.add(host.lower())

        # SQLite 持久化连接（可选）
        self._conn: sqlite3.Connection | None = None
        if config.persist_state_sqlite:
            self._init_sqlite()

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #

    def add(self, url: str, depth: int = 0) -> bool:
        """添加 URL 到待抓队列。

        依次执行：规范化 -> 协议/域名/正则过滤 -> 去重 -> 入队。
        返回 True 表示成功入队；返回 False 表示重复或被过滤。

        Args:
            url: 待添加的 URL（通常为绝对 URL）。
            depth: URL 的爬取深度，起始 URL 一般为 0。

        Returns:
            True 如果 URL 被接受并入队，False 如果重复或被过滤。
        """
        # 1. 规范化（去 fragment、小写 scheme/host、去默认端口、合并末尾斜杠）
        normalized = self._normalize(url)
        if normalized is None:
            self._logger.debug("过滤 URL（规范化失败）: %s", url)
            return False

        parts = urlsplit(normalized)

        # 2. 协议过滤：仅允许 http/https，跳过 mailto/javascript/tel/data
        scheme = parts.scheme.lower()
        if scheme in _SKIP_SCHEMES:
            self._logger.debug("过滤 URL（特殊协议 %s）: %s", scheme, url)
            return False
        if scheme not in ("http", "https"):
            self._logger.debug("过滤 URL（非 http/https 协议 %s）: %s", scheme, url)
            return False

        # 3. 域名过滤
        if not self._passes_domain_filter(parts):
            self._logger.debug("过滤 URL（域名限制）: %s", url)
            return False

        # 4. 正则白名单：非空时必须匹配至少一条（search，非 fullmatch）
        if self._whitelist_patterns:
            if not any(p.search(normalized) for p in self._whitelist_patterns):
                self._logger.debug("过滤 URL（白名单不匹配）: %s", url)
                return False

        # 5. 正则黑名单：匹配任意一条即拒绝
        if any(p.search(normalized) for p in self._blacklist_patterns):
            self._logger.debug("过滤 URL（黑名单匹配）: %s", url)
            return False

        # 6. 去重 + 入队（加锁，保证线程安全）
        with self._lock:
            if normalized in self._seen:
                return False
            self._seen.add(normalized)
            self._discovered += 1
            self._queue.append((normalized, depth))
            # SQLite 持久化：写入 seen_urls 表
            if self._conn is not None:
                try:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO seen_urls (url) VALUES (?)",
                        (normalized,),
                    )
                    self._conn.commit()
                except sqlite3.Error as exc:
                    self._logger.warning("SQLite 写入 seen_urls 失败: %s", exc)
        return True

    def add_many(self, urls: list[str], depth: int) -> int:
        """批量添加 URL，返回成功入队的数量。

        Args:
            urls: URL 列表。
            depth: 所有 URL 的爬取深度。

        Returns:
            成功入队的 URL 数量。
        """
        count = 0
        for url in urls:
            if self.add(url, depth):
                count += 1
        return count

    def get(self) -> tuple[str, int] | None:
        """从队列左侧取出一个 URL（广度优先）。

        线程安全。返回 ``(url, depth)`` 元组；队列空时返回 ``None``。

        Returns:
            ``(url, depth)`` 或 ``None``。
        """
        with self._lock:
            if self._queue:
                return self._queue.popleft()
            return None

    def seen(self, url: str) -> bool:
        """检查 URL 是否已在去重集合中（不添加）。

        对入参做与 ``add`` 相同的规范化后再查 seen 集合，
        确保判定结果与 ``add`` 一致。

        Args:
            url: 待检查的 URL。

        Returns:
            True 如果 URL 已在 seen 集合中。
        """
        normalized = self._normalize(url)
        if normalized is None:
            return False
        with self._lock:
            return normalized in self._seen

    @property
    def queue_size(self) -> int:
        """当前待抓队列长度。"""
        with self._lock:
            return len(self._queue)

    @property
    def discovered_count(self) -> int:
        """已发现 URL 总数（成功加入去重集合的唯一 URL 数）。"""
        with self._lock:
            return self._discovered

    def close(self) -> None:
        """关闭 SQLite 连接（如果已打开）。"""
        if self._conn is not None:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                pass
            finally:
                self._conn = None

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize(url: str) -> str | None:
        """规范化 URL。

        处理步骤：
        - 去除 fragment（#...）
        - 小写 scheme 和 host
        - 去除默认端口（http:80, https:443）
        - 合并末尾斜杠（根路径 ``/`` 除外）
        - 保留 userinfo 与 query

        使用 ``urllib.parse.urlsplit`` 解析，``urlunsplit`` 重建。
        规范化是确定性的：相同输入始终产生相同输出。

        Args:
            url: 原始 URL。

        Returns:
            规范化后的 URL 字符串；无法规范化（无 scheme/host/非法端口）
            时返回 None。
        """
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if not scheme:
            return None  # 无协议，无法处理（无法补全为绝对 URL）

        host = parts.hostname
        if not host:
            return None  # 无主机名，无法处理
        host = host.lower()

        # 解析端口（parts.port 可能抛出 ValueError）
        try:
            port = parts.port
        except ValueError:
            return None  # 端口非法

        # 构建 netloc：去除默认端口，保留非默认端口
        netloc = host
        if port is not None and port != _DEFAULT_PORTS.get(scheme):
            netloc = f"{host}:{port}"

        # 保留 userinfo（如果有）
        if parts.username:
            userinfo = parts.username
            if parts.password:
                userinfo += f":{parts.password}"
            netloc = f"{userinfo}@{netloc}"

        # 路径处理：合并末尾斜杠（根路径 '/' 除外）
        path = parts.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
            if not path:
                path = "/"

        # 重建 URL（丢弃 fragment，保留 query）
        return urlunsplit((scheme, netloc, path, parts.query, ""))

    def _passes_domain_filter(self, parts: SplitResult) -> bool:
        """检查 URL 是否通过域名过滤规则。

        规则：
        - ``allowed_domains`` 非空时，URL 的 host 必须在允许列表中。
        - ``restrict_to_start_domains`` 为 True 且 ``start_urls`` 非空时，
          URL 的 host 必须匹配某条起始 URL 的 host。

        Args:
            parts: urlsplit 返回的 SplitResult。

        Returns:
            True 如果通过域名过滤。
        """
        host = parts.hostname
        if not host:
            return False
        host = host.lower()

        # allowed_domains：非空时 URL 的 host 必须在允许列表中
        if self._allowed_domains and host not in self._allowed_domains:
            return False

        # restrict_to_start_domains：启用时 URL 的 host 必须匹配起始 URL 的 host
        if self._config.restrict_to_start_domains and self._start_hosts:
            if host not in self._start_hosts:
                return False

        return True

    def _init_sqlite(self) -> None:
        """初始化 SQLite 持久化连接，加载已存在的 seen URL。

        路径规则：若 ``config.output_path`` 以 ``.db`` 或 ``.sqlite`` 结尾，
        则用作数据库文件路径；否则使用 ``urls.db``。
        数据库表结构：``seen_urls(url TEXT PRIMARY KEY)``。
        """
        path = self._config.output_path
        if not (path.endswith(".db") or path.endswith(".sqlite")):
            path = "urls.db"

        # 确保父目录存在
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # check_same_thread=False：允许跨线程使用（写操作仍由 _lock 保护）
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_urls (url TEXT PRIMARY KEY)"
        )
        self._conn.commit()

        # 加载已存在的 seen URL 到内存集合，并同步 discovered 计数
        try:
            cur = self._conn.execute("SELECT url FROM seen_urls")
            for row in cur:
                self._seen.add(row[0])
            cur.close()
            self._discovered = len(self._seen)
        except sqlite3.Error as exc:
            self._logger.warning("SQLite 加载 seen_urls 失败: %s", exc)
