"""官方 API 探测器。

根据 spec 的「官方 API 探测」要求，根据起始 URL 探测目标站点是否暴露
官方 API（``/api``、``/api/v1``、``/swagger.json``、``/openapi.json``、
``/api-docs``，以及 ``robots.txt`` 中对 ``/api`` 的 Disallow 声明）。

设计要点：
    - 纯逻辑模块，无 PySide6 依赖。
    - 优先使用注入的 ``fetcher``（通常是 :class:`crawler.downloader.HTTPDownloader.fetch`）；
      未注入时回退到 :func:`urllib.request.urlopen`，并使用 5 秒超时与
      项目默认声明的 Bot UA。
    - 每个探测点均包裹在 try/except 中，任何异常 / 超时都跳过该探测，绝不抛出。
    - 用户已经在使用 API（起始 URL 路径以 ``/api`` 开头）时直接返回 ``None``。
"""

from __future__ import annotations

import logging
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from crawler.models import Response

# 回退到 urlopen 时使用的默认 Bot UA（项目声明）
_DEFAULT_UA = (
    "InternetCrawlerBot/1.0 "
    "(+https://github.com/secret-Alan/InternetCrawler)"
)

# urlopen 回退的单请求超时（秒）
_URLOPEN_TIMEOUT = 5.0


@dataclass
class ApiInfo:
    """探测到的官方 API 信息。

    Attributes:
        base_url: 探测基准地址 (``scheme://host``)。
        evidence: 命中的 API 指标列表（人类可读）。
        suggested_endpoints: 建议尝试的端点完整 URL 列表。
    """

    base_url: str
    evidence: list[str] = field(default_factory=list)
    suggested_endpoints: list[str] = field(default_factory=list)


def _is_using_api(start_urls: list[str]) -> bool:
    """判断用户起始 URL 是否已经在使用 API。

    返回 ``True`` 表示第一条起始 URL 的路径以 ``/api`` 开头。
    """

    if not start_urls:
        return False
    parts = urlsplit(start_urls[0])
    return parts.path.startswith("/api")


def _urlopen_fetch(url: str) -> Response | None:
    """使用 urllib 回退抓取单个 URL。

    返回 :class:`Response`，或失败时返回 ``None``。绝不抛出异常。
    """

    try:
        req = urllib.request.Request(url, headers={"User-Agent": _DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=_URLOPEN_TIMEOUT) as fp:
            raw = fp.read()
            status = fp.getcode() or 0
            try:
                headers = dict(fp.headers.items())
            except Exception:  # noqa: BLE001 - headers 不应失败，保守处理
                headers = {}
        text = raw.decode("utf-8", errors="replace")
        return Response(
            url=url,
            status=status,
            headers=headers,
            text=text,
            content=raw,
        )
    except Exception:  # noqa: BLE001 - 任何失败均按跳过处理
        return None


def _fetch(
    url: str,
    fetcher: Callable[[str], Response] | None,
    logger: logging.Logger,
) -> Response | None:
    """统一抓取入口：优先注入的 fetcher，否则回退到 urlopen。"""

    if fetcher is not None:
        try:
            return fetcher(url)
        except Exception as exc:  # noqa: BLE001 - 探测点失败需跳过
            logger.debug("fetcher 抓取 %s 异常: %s", url, exc)
            return None
    return _urlopen_fetch(url)


def _content_type(headers: dict) -> str:
    """从响应头中提取 ``Content-Type``（小写）。"""

    for key, value in (headers or {}).items():
        if key.lower() == "content-type":
            return (value or "").lower()
    return ""


def detect_official_api(
    start_urls: list[str],
    fetcher: Callable[[str], Response] | None = None,
) -> ApiInfo | None:
    """探测目标站点是否暴露官方 API。

    Args:
        start_urls: 起始 URL 列表，取第一条推导 ``scheme://host``。
        fetcher: 可选的 ``fetcher(url) -> Response``；为 ``None`` 时回退到
            :func:`urllib.request.urlopen`。

    Returns:
        命中任何指标时返回 :class:`ApiInfo`；起始 URL 为空、用户已在用 API
        或未命中任何指标时返回 ``None``。
    """

    logger = logging.getLogger("crawler.api_detector")

    if not start_urls:
        return None

    parts = urlsplit(start_urls[0])
    scheme = parts.scheme.lower()
    host = parts.hostname
    if not scheme or not host:
        return None
    base = f"{scheme}://{host}"

    # 用户已经在使用 API：无需探测
    if _is_using_api(start_urls):
        return None

    evidence: list[str] = []
    suggested: list[str] = []

    # /robots.txt：检查是否声明 Disallow: /api
    robots_url = base + "/robots.txt"
    resp = _fetch(robots_url, fetcher, logger)
    if resp is not None and getattr(resp, "status", 0) == 200:
        robots_text = (getattr(resp, "text", "") or "").lower()
        if "disallow: /api" in robots_text:
            evidence.append("robots.txt 声明 Disallow: /api")

    # /api, /api/v1：状态 200 且 content-type 指示 json / text
    for path in ("/api", "/api/v1"):
        url = base + path
        resp = _fetch(url, fetcher, logger)
        if resp is not None and getattr(resp, "status", 0) == 200:
            ctype = _content_type(getattr(resp, "headers", {}) or {})
            if "json" in ctype or "text" in ctype:
                evidence.append(f"存在 {path} 端点")
                suggested.append(url)

    # /swagger.json, /openapi.json：状态 200 且响应体以 { 开头
    for path in ("/swagger.json", "/openapi.json"):
        url = base + path
        resp = _fetch(url, fetcher, logger)
        if resp is not None and getattr(resp, "status", 0) == 200:
            content = getattr(resp, "content", b"") or b""
            if isinstance(content, bytes):
                prefix = content[:1]
            else:
                prefix = str(content)[:1].encode("utf-8", errors="replace")
            if prefix == b"{":
                evidence.append(f"发现 {path} 规范文件")

    # /api-docs：状态 200 即视为存在
    api_docs_path = "/api-docs"
    url = base + api_docs_path
    resp = _fetch(url, fetcher, logger)
    if resp is not None and getattr(resp, "status", 0) == 200:
        evidence.append(f"存在 {api_docs_path}")

    if evidence:
        return ApiInfo(
            base_url=base,
            evidence=evidence,
            suggested_endpoints=suggested,
        )
    return None
