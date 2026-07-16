"""合规评估模块。

在爬取前对目标站点进行合规评估，包括：

    - 敏感数据扫描：检测页面中可能包含的邮箱、手机号、身份证号以及商业秘密
      关键词。
    - 用户协议(ToS)扫描：探测 ``/terms``、``/tos``、``/agreement``、``/legal``
      等常见服务条款页面，识别是否存在禁止爬取/抓取/采集的条款。

设计要点：

    - 纯逻辑模块，无 PySide6 依赖。
    - 优先使用注入的 ``fetcher``（通常是
      :class:`crawler.downloader.HTTPDownloader.fetch`）；未注入时回退到
      :func:`urllib.request.urlopen`，并使用 5 秒超时与项目默认声明的 Bot UA。
    - 所有网络抓取均包裹在 try/except 中，任何异常 / 超时都跳过，绝不抛出。
"""

from __future__ import annotations

import logging
import re
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

# 敏感数据正则
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_ID_CARD_RE = re.compile(r"\d{17}[\dXx]")

# 商业秘密关键词（小写匹配）
_COMMERCIAL_SECRET_KEYWORDS = [
    "机密",
    "绝密",
    "proprietary",
    "confidential",
    "trade secret",
]

# ToS 禁止爬取的关键短语（小写匹配）
_TOS_PROHIBIT_PHRASES = [
    "禁止爬取",
    "禁止抓取",
    "no scraping",
    "scraping prohibited",
    "不得采集",
    "禁止采集",
]

# ToS 探测路径（按优先级顺序）
_TOS_PATHS = ["/terms", "/tos", "/agreement", "/legal"]


@dataclass
class ComplianceReport:
    """合规评估结果。

    Attributes:
        sensitive_hits: 检测到的敏感项描述列表（如 "邮箱: user@example.com"）。
        sensitive_categories: 命中的敏感数据类别（如 "email"、"phone"、
            "id_card"、"commercial_secret"）。
        tos_prohibits: 用户协议是否存在禁止爬取/抓取/采集的条款。
        tos_evidence: 命中的禁止条款原文短语列表。
        assessed: 评估是否成功运行；抓取失败等无法评估的情况下为 False。
    """

    sensitive_hits: list[str] = field(default_factory=list)
    sensitive_categories: list[str] = field(default_factory=list)
    tos_prohibits: bool = False
    tos_evidence: list[str] = field(default_factory=list)
    assessed: bool = True


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
        except Exception as exc:  # noqa: BLE001 - 抓取失败需跳过
            logger.debug("fetcher 抓取 %s 异常: %s", url, exc)
            return None
    return _urlopen_fetch(url)


class ComplianceAssessor:
    """合规评估器。

    在爬取前评估目标站点的敏感数据暴露情况与用户协议限制。所有评估均不会抛出
    异常；网络抓取失败时相应部分标记为未评估。
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("crawler.compliance")

    def assess(
        self,
        start_urls: list[str],
        fetcher: Callable[[str], Response] | None = None,
    ) -> ComplianceReport:
        """对目标站点进行合规评估。

        Args:
            start_urls: 起始 URL 列表，取第一条推导 ``scheme://host`` 并作为
                敏感数据扫描的抓取目标。
            fetcher: 可选的 ``fetcher(url) -> Response``；为 ``None`` 时回退到
                :func:`urllib.request.urlopen`。

        Returns:
            :class:`ComplianceReport`。起始 URL 为空时 ``assessed=False``。
            所有网络抓取均包裹在 try/except 中，绝不抛出异常。
        """

        if not start_urls:
            return ComplianceReport(assessed=False)

        parts = urlsplit(start_urls[0])
        scheme = parts.scheme.lower()
        host = parts.hostname
        if not scheme or not host:
            return ComplianceReport(assessed=False)
        base = f"{scheme}://{host}"

        sensitive_hits: list[str] = []
        sensitive_categories: list[str] = []
        seen_hits: set[str] = set()
        assessed = True

        # ---- 敏感数据扫描：抓取首个起始 URL ----
        page_text = ""
        try:
            resp = _fetch(start_urls[0], fetcher, self._logger)
            if resp is not None:
                page_text = getattr(resp, "text", "") or ""
            else:
                assessed = False
        except Exception as exc:  # noqa: BLE001 - 抓取失败需跳过
            self._logger.debug(
                "敏感数据扫描抓取 %s 异常: %s", start_urls[0], exc
            )
            assessed = False

        if page_text:
            self._scan_sensitive(
                page_text, sensitive_hits, sensitive_categories, seen_hits
            )

        # ---- ToS 扫描：探测常见服务条款页面 ----
        tos_prohibits = False
        tos_evidence: list[str] = []
        for path in _TOS_PATHS:
            tos_url = base + path
            tos_text = ""
            try:
                resp = _fetch(tos_url, fetcher, self._logger)
                if resp is not None and getattr(resp, "status", 0) == 200:
                    tos_text = getattr(resp, "text", "") or ""
            except Exception as exc:  # noqa: BLE001 - 抓取失败需跳过
                self._logger.debug("ToS 扫描抓取 %s 异常: %s", tos_url, exc)
                tos_text = ""

            if tos_text:
                matched = self._scan_tos(tos_text)
                if matched:
                    tos_prohibits = True
                    tos_evidence.extend(matched)
                # 找到首个 ToS 页面即停止（无论是否命中禁止条款）
                break

        return ComplianceReport(
            sensitive_hits=sensitive_hits,
            sensitive_categories=sensitive_categories,
            tos_prohibits=tos_prohibits,
            tos_evidence=tos_evidence,
            assessed=assessed,
        )

    @staticmethod
    def _scan_sensitive(
        text: str,
        hits: list[str],
        categories: list[str],
        seen: set[str],
    ) -> None:
        """扫描文本中的敏感数据，结果追加到 ``hits`` / ``categories``。

        同一描述只追加一次（去重）。``categories`` 中每个类别最多出现一次。
        """

        # 邮箱
        for match in _EMAIL_RE.findall(text):
            desc = f"邮箱: {match}"
            if desc not in seen:
                seen.add(desc)
                hits.append(desc)
                if "email" not in categories:
                    categories.append("email")

        # 手机号（中国大陆移动号码）
        for match in _PHONE_RE.findall(text):
            desc = f"手机号: {match}"
            if desc not in seen:
                seen.add(desc)
                hits.append(desc)
                if "phone" not in categories:
                    categories.append("phone")

        # 身份证号（18 位）
        for match in _ID_CARD_RE.findall(text):
            desc = f"身份证号: {match}"
            if desc not in seen:
                seen.add(desc)
                hits.append(desc)
                if "id_card" not in categories:
                    categories.append("id_card")

        # 商业秘密关键词（大小写不敏感子串匹配）
        lowered = text.lower()
        for kw in _COMMERCIAL_SECRET_KEYWORDS:
            if kw.lower() in lowered:
                desc = f"商业秘密关键词: {kw}"
                if desc not in seen:
                    seen.add(desc)
                    hits.append(desc)
                    if "commercial_secret" not in categories:
                        categories.append("commercial_secret")

    @staticmethod
    def _scan_tos(text: str) -> list[str]:
        """扫描 ToS 文本中的禁止爬取短语，返回命中的短语列表。"""

        lowered = text.lower()
        matched: list[str] = []
        for phrase in _TOS_PROHIBIT_PHRASES:
            if phrase.lower() in lowered:
                matched.append(phrase)
        return matched

    def redact_content(self, text: str) -> str:
        """对文本中的敏感数据进行脱敏替换。

        - 邮箱 → ``[REDACTED-EMAIL]``
        - 手机号 → ``[REDACTED-PHONE]``
        - 身份证号 → ``[REDACTED-ID]``
        - 商业秘密关键词 → ``[REDACTED-SECRET]``

        其余内容保持不变。身份证号先于手机号替换，以避免 18 位身份证号中
        的 11 位子串被误判为手机号。
        """

        if not text:
            return text
        text = _ID_CARD_RE.sub("[REDACTED-ID]", text)
        text = _PHONE_RE.sub("[REDACTED-PHONE]", text)
        text = _EMAIL_RE.sub("[REDACTED-EMAIL]", text)
        for kw in _COMMERCIAL_SECRET_KEYWORDS:
            text = re.sub(
                re.escape(kw), "[REDACTED-SECRET]", text, flags=re.IGNORECASE
            )
        return text
