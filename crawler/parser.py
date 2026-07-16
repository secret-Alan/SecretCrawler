"""内容解析器。

依据 `crawler/models.py` 中的 `ExtractionRule` / `SelectorType` 对下载到的
HTML / JSON 响应文本进行字段提取，并发现页面中的后续爬取链接与分页链接。

支持的提取方式：
    - CSS Selector  (BeautifulSoup4 + lxml，lxml 不可用时回退 html.parser)
    - XPath         (lxml.etree)
    - 正则表达式    (re)
    - JSON 字段路径 (data.items[*].title 形式)

本模块不依赖 PySide6，仅依赖标准库、bs4 与（可选的）lxml。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

# lxml 在某些环境（如缺少预编译 wheel 的 Python beta 版）可能无法安装。
# 这里做防御性导入：lxml 可用时使用 lxml 作为 BS4 解析器并支持 XPath；
# 不可用时 CSS/正则/JSON/链接发现仍可工作，XPath 规则降级返回空字符串。
try:  # pragma: no cover - 依赖运行环境
    from lxml import etree as _etree
except ImportError:  # lxml 未安装时降级
    _etree = None

from crawler.models import (
    CrawlConfig,
    ExtractionRule,
    ResultItem,
    SelectorType,
)

# 用于缓存“尚未解析”的哨兵，区别于 etree.HTML 可能返回的 None。
_UNSET = object()

# 分页“下一页”文本的匹配模式（中文“下一页”或英文 next/»）。
_NEXT_TEXT_RE = re.compile(r"^\s*(下一页|next\s*»?|»)\s*$", re.IGNORECASE)
# 形如 page=2 的分页参数。
_PAGE_PARAM_RE = re.compile(r"page=\d+", re.IGNORECASE)


class ContentParser:
    """内容解析器：字段提取 + 链接发现 + 分页发现。"""

    def __init__(self, config: CrawlConfig, logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger if logger is not None else logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def extract(
        self,
        url: str,
        response_text: str,
        rules: list[ExtractionRule],
    ) -> ResultItem:
        """按规则列表提取字段，组装为 `ResultItem`。

        每条规则独立 try/except，任何异常或未命中都不会导致整体失败，
        对应字段置为空字符串。
        """
        fields: dict[str, str] = {}
        # 在单次 extract 调用内缓存解析结果，避免重复解析。
        soup: Any = _UNSET
        lxml_tree: Any = _UNSET

        for rule in rules:
            try:
                st = rule.selector_type
                if st == SelectorType.CSS:
                    if soup is _UNSET:
                        soup = self._make_soup(response_text)
                    value = self._extract_css(soup, rule)
                elif st == SelectorType.XPATH:
                    if _etree is None:
                        value = ""  # lxml 不可用，XPath 降级
                    else:
                        if lxml_tree is _UNSET:
                            lxml_tree = _etree.HTML(response_text)
                        value = self._extract_xpath(lxml_tree, rule)
                elif st == SelectorType.REGEX:
                    value = self._extract_regex(response_text, rule)
                elif st == SelectorType.JSON:
                    value = self._extract_json(response_text, rule)
                else:
                    self.logger.debug("未知的选择器类型 %s，规则 %s 跳过", st, rule.name)
                    value = ""
            except Exception as exc:  # noqa: BLE001 - 解析必须容错
                self.logger.debug(
                    "规则 %s 提取异常: %s", rule.name, exc, exc_info=True
                )
                value = ""

            if not value:
                self.logger.debug("规则 %s 未提取到内容", rule.name)
            fields[rule.name] = value

        return ResultItem(url=url, fields=fields, raw_response=response_text)

    def extract_links(self, base_url: str, response_text: str) -> list[str]:
        """发现页面中所有 http/https 绝对链接（去重、保留顺序、去 fragment）。

        若 `config.auto_follow_links` 为 False，直接返回空列表。
        域名过滤交由 URL 管理器负责，此处只做协议与基本合法性过滤。
        """
        if not self.config.auto_follow_links:
            return []

        soup = self._make_soup(response_text)
        seen: set[str] = set()
        result: list[str] = []

        for a in soup.find_all("a"):
            href = a.get("href")
            if not href:
                continue  # 无 href 的 <a> 跳过
            absolute = self._resolve_url(base_url, href)
            if absolute is None:
                continue  # 非 http(s) scheme 跳过
            if absolute in seen:
                continue
            seen.add(absolute)
            result.append(absolute)

        return result

    def extract_pagination_links(self, base_url: str, response_text: str) -> list[str]:
        """发现“下一页”分页链接。

        若 `config.auto_detect_pagination` 为 False，直接返回空列表。
        采用多种启发式策略，保守地返回明确的分页候选。
        """
        if not self.config.auto_detect_pagination:
            return []

        soup = self._make_soup(response_text)
        seen: set[str] = set()
        result: list[str] = []

        def add(href: str | None) -> None:
            if not href:
                return
            absolute = self._resolve_url(base_url, href)
            if absolute is None:
                return
            if absolute in seen:
                return
            seen.add(absolute)
            result.append(absolute)

        # 策略 1：<a rel="next"> 与 <link rel="next">
        for tag in soup.find_all(["a", "link"], attrs={"rel": "next"}):
            add(tag.get("href"))

        # 策略 2：<a> 文本匹配“下一页 / next / »”
        for a in soup.find_all("a"):
            text = a.get_text(strip=True)
            if text and _NEXT_TEXT_RE.match(text):
                add(a.get("href"))

        # 策略 3：URL 中 page=N / p=N 模式推算下一页
        for candidate in self._pagination_url_candidates(base_url, soup):
            add(candidate)

        return result

    # ------------------------------------------------------------------
    # CSS 提取
    # ------------------------------------------------------------------
    def _extract_css(self, soup: BeautifulSoup, rule: ExtractionRule) -> str:
        """使用 bs4 的 select() 执行 CSS 选择器，提取文本或属性。"""
        elements = soup.select(rule.expression)
        values: list[str] = []
        for el in elements:
            if not rule.attribute:
                text = el.get_text(strip=True)
                if text:
                    values.append(text)
            else:
                attr_val = el.get(rule.attribute, "")
                if attr_val:
                    values.append(str(attr_val))
        return " | ".join(values)

    # ------------------------------------------------------------------
    # XPath 提取
    # ------------------------------------------------------------------
    def _extract_xpath(self, tree: Any, rule: ExtractionRule) -> str:
        """使用 lxml etree.xpath 执行 XPath 表达式。

        xpath 结果可能是元素、字符串或属性值，统一处理：
        - 字符串：直接使用；
        - 元素：attribute 为空取 text_content()，否则取对应属性；
        - 其他类型（如 float）：转字符串。
        """
        if _etree is None or tree is None:
            return ""
        results = tree.xpath(rule.expression)
        values: list[str] = []
        for res in results:
            if isinstance(res, str):
                v = res.strip()
            elif _etree is not None and isinstance(res, _etree._Element):
                if not rule.attribute:
                    v = (res.text_content() or "").strip()
                else:
                    v = (res.get(rule.attribute) or "").strip()
            else:
                v = str(res).strip()
            if v:
                values.append(v)
        return " | ".join(values)

    # ------------------------------------------------------------------
    # 正则提取
    # ------------------------------------------------------------------
    def _extract_regex(self, response_text: str, rule: ExtractionRule) -> str:
        """对响应文本应用正则。

        - 有分组：取每个匹配的第 1 组；
        - 无分组：取完整匹配；
        - 多个结果用 ' | ' 连接，单个结果直接返回。
        """
        matches = re.findall(rule.expression, response_text)
        if not matches:
            return ""

        # findall 在有多个分组时返回 tuple 列表；单分组或无分组返回 str 列表。
        if isinstance(matches[0], tuple):
            values = [m[0] for m in matches if m and m[0]]
        else:
            values = [v for v in matches if v]

        if not values:
            return ""
        if len(values) == 1:
            return values[0]
        return " | ".join(values)

    # ------------------------------------------------------------------
    # JSON 提取
    # ------------------------------------------------------------------
    def _extract_json(self, response_text: str, rule: ExtractionRule) -> str:
        """解析 JSON 并按点分路径（支持 [*] 数组展开）提取值。

        路径示例：`data.items[*].title`。
        对缺失的键容忍跳过；终端值转字符串后用 ' | ' 连接。
        """
        data = json.loads(response_text)
        values = self._walk_json(data, rule.expression)
        str_values = [str(v) for v in values if v is not None]
        if not str_values:
            return ""
        if len(str_values) == 1:
            return str_values[0]
        return " | ".join(str_values)

    def _walk_json(self, data: Any, path: str) -> list[Any]:
        """按 `a.b[*].c` 形式遍历 JSON 数据，返回所有终端值。"""
        if not path:
            return [data] if data is not None else []

        current: list[Any] = [data]
        for part in path.split("."):
            if part == "":
                continue
            if part.endswith("[*]"):
                key = part[:-3]
                nxt: list[Any] = []
                for item in current:
                    if key == "":
                        # 根级或隐式数组：item 自身应为列表
                        if isinstance(item, list):
                            nxt.extend(item)
                    elif isinstance(item, dict) and key in item:
                        val = item[key]
                        if isinstance(val, list):
                            nxt.extend(val)
                        elif val is not None:
                            nxt.append(val)
                current = nxt
            else:
                key = part
                nxt = []
                for item in current:
                    if isinstance(item, dict) and key in item:
                        v = item[key]
                        if v is not None:
                            nxt.append(v)
                current = nxt
        return current

    # ------------------------------------------------------------------
    # 分页 URL 推算
    # ------------------------------------------------------------------
    def _pagination_url_candidates(
        self, base_url: str, soup: BeautifulSoup
    ) -> list[str]:
        """根据 URL 中的 page/p 参数推算下一页候选 URL（保守策略）。"""
        parsed = urllib.parse.urlparse(base_url)
        query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)

        # 查找当前 URL 中的 page=N 或 p=N。
        page_key: str | None = None
        page_val: int | None = None
        for k in ("page", "p"):
            for kk, vv in query_pairs:
                if kk == k:
                    try:
                        page_val = int(vv)
                        page_key = k
                    except ValueError:
                        continue
            if page_key is not None:
                break

        if page_key is not None and page_val is not None:
            # 当前 URL 已含 page=N → 推荐 page=N+1
            new_pairs = [
                (k, str(page_val + 1) if k == page_key else v)
                for k, v in query_pairs
            ]
            return [self._build_url(parsed, new_pairs)]

        # 当前 URL 无 page 参数：仅当页面存在多个分页链接时才推荐 ?page=2
        page_link_count = sum(
            1
            for a in soup.find_all("a")
            if _PAGE_PARAM_RE.search(a.get("href", "") or "")
        )
        if page_link_count >= 2:
            filtered = [(k, v) for k, v in query_pairs if k not in ("page", "p")]
            filtered.append(("page", "2"))
            return [self._build_url(parsed, filtered)]

        return []

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _make_soup(self, html: str) -> BeautifulSoup:
        """构造 BeautifulSoup，优先 lxml，不可用时回退 html.parser。"""
        if html is None:
            html = ""
        try:
            return BeautifulSoup(html, "lxml")
        except Exception:
            try:
                return BeautifulSoup(html, "html.parser")
            except Exception:
                return BeautifulSoup("", "html.parser")

    def _resolve_url(self, base_url: str, href: str) -> str | None:
        """将 href 解析为绝对 URL，去 fragment；非 http(s) 返回 None。"""
        try:
            absolute = urllib.parse.urljoin(base_url, href)
        except ValueError:
            return None
        absolute = urllib.parse.urldefrag(absolute)[0]
        scheme = urllib.parse.urlsplit(absolute).scheme.lower()
        if scheme not in ("http", "https"):
            return None
        return absolute

    def _build_url(
        self, parsed: urllib.parse.ParseResult, query_pairs: list[tuple[str, str]]
    ) -> str:
        """依据已解析的 URL 部件与新的 query 参数列表重建 URL。"""
        return urllib.parse.urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                urllib.parse.urlencode(query_pairs),
                "",
            )
        )
