"""robots.txt 合规检查器。

根据 spec 的「robots.txt 遵守」要求，为目标站点下载并解析 robots.txt，
使用 :class:`urllib.robotparser.RobotFileParser` 判断给定 URL 是否允许
当前 User-Agent 抓取。

设计要点：
    - 按 scheme+host 缓存每个站点的解析结果，避免对每个 URL 重复下载。
    - 缺失 / 解析失败 / 下载失败的 robots.txt 一律视为「全部允许」
      （遵循 robots.txt 规范约定：找不到 robots.txt 即表示无限制）。
    - 通过 :class:`threading.Lock` 保护缓存访问；下载过程不持锁，
      使用双重检查保证每个域名至多下载一次。
"""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from crawler.models import CrawlConfig, Response

# 无法从配置中确定 User-Agent 时使用的默认 UA
_DEFAULT_USER_AGENT = (
    "InternetCrawlerBot/1.0 "
    "(+https://github.com/secret-Alan/InternetCrawler)"
)

# 未注入 fetcher 时，urlopen 回退的超时时间（秒）
_URLOPEN_TIMEOUT = 10.0


class RobotsChecker:
    """检查 URL 是否被目标站点 robots.txt 允许抓取。

    Args:
        config: 爬虫配置，主要用于读取 User-Agent 与 ``respect_robots`` 开关。
        logger: 可选日志器；默认使用 ``logging.getLogger("crawler.robots")``。
        fetcher: 可选的可调用对象，签名 ``fetcher(url) -> Response``，
            用于复用下载器（含代理 / 自定义头等配置）。为 ``None`` 时回退到
            :func:`urllib.request.urlopen`。
    """

    def __init__(
        self,
        config: CrawlConfig,
        logger: logging.Logger | None = None,
        fetcher: Callable[[str], Response] | None = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger("crawler.robots")
        self._fetcher = fetcher
        # 缓存：key = "scheme://host"，value 为 RobotFileParser 或 None。
        # None 哨兵表示「无 robots.txt / 全部允许」。
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self._user_agent = self._resolve_user_agent()

    def allowed(self, url: str) -> bool:
        """判断 ``url`` 是否被 robots.txt 允许抓取。

        返回 ``True`` 表示允许抓取；当 robots.txt 不可用 / 解析失败 /
        未启用遵守时也返回 ``True``。
        """
        # 未启用 robots 遵守：直接放行
        if not getattr(self._config, "respect_robots", True):
            return True

        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        if not scheme or not host:
            # 无法识别站点根，保守放行并记录
            self._logger.warning("无法解析 URL 的 scheme/host，放行: %s", url)
            return True

        cache_key = f"{scheme}://{host}"

        # 快速路径：原子地读取缓存命中状态与缓存值
        with self._lock:
            cached = self._cache.get(cache_key)
            hit = cache_key in self._cache
        if hit:
            if cached is None:
                # None 哨兵：robots.txt 不可用 -> 全部允许
                return True
            return cached.can_fetch(self._user_agent, url)

        # 慢速路径：下载并解析 robots.txt（不持锁，避免阻塞其它域名查询）
        parser = self._load_robots(scheme, host)

        with self._lock:
            # 双重检查：其它线程可能已抢先填充缓存
            if cache_key in self._cache:
                cached = self._cache[cache_key]
            else:
                self._cache[cache_key] = parser
                cached = parser

        if cached is None:
            return True
        return cached.can_fetch(self._user_agent, url)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _resolve_user_agent(self) -> str:
        """确定生效的 User-Agent。

        优先级：``config.headers`` 中的 ``User-Agent`` >
        ``config.user_agents`` 的第一项 > 默认 Mozilla UA。
        """
        headers = getattr(self._config, "headers", None) or {}
        for key, value in headers.items():
            if key.lower() == "user-agent" and value:
                return value
        user_agents = getattr(self._config, "user_agents", None) or []
        if user_agents:
            return user_agents[0]
        return _DEFAULT_USER_AGENT

    def _load_robots(self, scheme: str, host: str) -> RobotFileParser | None:
        """下载并解析指定站点的 robots.txt。

        Returns:
            解析好的 :class:`RobotFileParser`；若不可用 / 失败则返回 ``None``
            （表示「全部允许」）。
        """
        robots_url = f"{scheme}://{host}/robots.txt"
        text = self._fetch_robots_text(robots_url)
        if text is None:
            return None
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.parse(text.splitlines())
        except Exception as exc:  # 解析异常：保守放行
            self._logger.warning(
                "解析 robots.txt 失败，按全部允许处理: %s (%s)", robots_url, exc
            )
            return None
        self._logger.debug("已加载 robots.txt: %s", robots_url)
        return parser

    def _fetch_robots_text(self, robots_url: str) -> str | None:
        """获取 robots.txt 文本内容。

        优先使用注入的 ``fetcher``，否则回退到 :func:`urllib.request.urlopen`。
        任何错误（404 / 超时 / 连接失败）均返回 ``None``，由调用方按「全部允许」处理。
        """
        if self._fetcher is not None:
            return self._fetch_with_fetcher(robots_url)
        return self._fetch_with_urllib(robots_url)

    def _fetch_with_fetcher(self, robots_url: str) -> str | None:
        """通过注入的 fetcher 下载 robots.txt 文本。"""
        try:
            resp = self._fetcher(robots_url)  # type: ignore[operator]
        except Exception as exc:
            self._logger.info(
                "通过 fetcher 获取 robots.txt 异常，按全部允许处理: %s (%s)",
                robots_url,
                exc,
            )
            return None
        # Response 可能携带 error 字段或非 2xx 状态码
        error = getattr(resp, "error", "") or ""
        status = getattr(resp, "status", 0) or 0
        if error:
            self._logger.info(
                "robots.txt 下载失败 (%s)，按全部允许处理: %s", error, robots_url
            )
            return None
        if status and not (200 <= status < 300):
            self._logger.info(
                "robots.txt 返回非 2xx 状态 %s，按全部允许处理: %s",
                status,
                robots_url,
            )
            return None
        return getattr(resp, "text", "") or ""

    def _fetch_with_urllib(self, robots_url: str) -> str | None:
        """通过 urllib.request.urlopen 回退下载 robots.txt 文本。"""
        try:
            req = urllib.request.Request(
                robots_url, headers={"User-Agent": self._user_agent}
            )
            with urllib.request.urlopen(req, timeout=_URLOPEN_TIMEOUT) as fp:
                raw = fp.read()
        except urllib.error.HTTPError as exc:
            # 404 等：robots.txt 不存在 -> 全部允许
            self._logger.info(
                "robots.txt HTTP %s，按全部允许处理: %s", exc.code, robots_url
            )
            return None
        except urllib.error.URLError as exc:
            self._logger.info(
                "robots.txt 下载失败 (%s)，按全部允许处理: %s",
                exc.reason,
                robots_url,
            )
            return None
        except Exception as exc:
            self._logger.info(
                "robots.txt 下载异常 (%s)，按全部允许处理: %s", exc, robots_url
            )
            return None

        # robots.txt 规范为 ASCII/UTF-8，按 UTF-8 解码即可
        return raw.decode("utf-8", errors="replace")


# ==================================================================
# 模块级公开 API：供 UI（robots.txt 查看器）等模块直接复用
# ==================================================================


def fetch_raw_robots(
    base_url: str, fetcher: Callable[[str], Response] | None = None
) -> tuple[str, int]:
    """下载指定站点的 robots.txt 原文。

    本函数为 UI 专用入口，不依赖 :class:`RobotsChecker` 的缓存 / 配置，
    绝不向调用方抛异常。

    Args:
        base_url: 形如 ``https://example.com`` 的站点根（scheme://host，无路径）。
        fetcher: 可选的可调用对象，签名 ``fetcher(url) -> Response``；
            为 ``None`` 时回退到 :func:`urllib.request.urlopen`。

    Returns:
        ``(text, status)`` 二元组：

            - 成功：``(robots.txt 原文, 200)``
            - HTTP 错误（如 404）：``("", http_code)``
            - 网络 / 解析错误：``("", 0)``
    """
    robots_url = base_url.rstrip("/") + "/robots.txt"

    # ---- 注入 fetcher 路径 ----
    if fetcher is not None:
        try:
            resp = fetcher(robots_url)
        except Exception:
            return ("", 0)
        if resp is not None and getattr(resp, "status", 0) == 200:
            return (getattr(resp, "text", "") or "", 200)
        return ("", getattr(resp, "status", 0) if resp is not None else 0)

    # ---- urllib 回退路径 ----
    try:
        req = urllib.request.Request(
            robots_url, headers={"User-Agent": _DEFAULT_USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=_URLOPEN_TIMEOUT) as fp:
            raw = fp.read()
            status = fp.getcode() or 200
    except urllib.error.HTTPError as exc:
        # 404 等：返回具体状态码便于 UI 提示
        return ("", exc.code)
    except urllib.error.URLError:
        return ("", 0)
    except Exception:
        return ("", 0)

    return (raw.decode("utf-8", errors="replace"), status)


def parse_robots_summary(
    raw_text: str, user_agent: str = "*"
) -> dict:
    """解析 robots.txt 原文，返回 UA 视角下的摘要信息。

    Args:
        raw_text: robots.txt 原文（UTF-8 文本）。
        user_agent: 要查询的 User-Agent，默认 ``"*"``。

    Returns:
        形如 ``{"allowed": bool, "crawl_delay": float | None, "sitemaps": list[str]}``
        的字典。任何异常都按「全部允许、无 delay、无 sitemap」保守返回。
    """
    fallback: dict = {
        "allowed": True,
        "crawl_delay": None,
        "sitemaps": [],
    }
    if not raw_text:
        return fallback
    try:
        parser = RobotFileParser()
        parser.parse(raw_text.splitlines())
        allowed = bool(parser.can_fetch(user_agent, "/"))
        delay = parser.crawl_delay(user_agent)
        if delay is not None:
            try:
                crawl_delay: float | None = float(delay)
            except (TypeError, ValueError):
                crawl_delay = None
        else:
            crawl_delay = None
        sitemaps = parser.site_maps() or []
        return {
            "allowed": allowed,
            "crawl_delay": crawl_delay,
            "sitemaps": list(sitemaps),
        }
    except Exception:
        return fallback
