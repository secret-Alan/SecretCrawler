"""Data models for the web crawler.

This module is UI-free (no PySide6 imports) and contains only data definitions
plus serialization helpers used by the crawler engine and the UI layer.

Classes defined here:
    SelectorType    - enum of supported selector languages
    ExtractionRule  - a single named field extraction rule
    CrawlConfig     - full crawler configuration (mirrors the UI config panel)
    Response        - structured download result
    ResultItem      - one extracted record from a page
    CrawlStats      - mutable runtime statistics updated by the scheduler
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SelectorType(str, Enum):
    """Supported selector / extraction languages."""

    CSS = "CSS"
    XPATH = "XPATH"
    REGEX = "REGEX"
    JSON = "JSON"


@dataclass
class ExtractionRule:
    """A single named extraction rule.

    Attributes:
        name: Field name to populate (e.g. "title").
        selector_type: One of SelectorType.
        expression: The selector/xpath/regex/json path string.
        attribute: For CSS rules, "" means extracted text, otherwise the
            attribute name to read (e.g. "href"). Ignored by other selector
            types.
    """

    name: str
    selector_type: SelectorType = SelectorType.CSS
    expression: str = ""
    attribute: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ExtractionRule":
        selector_type = data.get("selector_type", SelectorType.CSS)
        if isinstance(selector_type, str):
            try:
                selector_type = SelectorType(selector_type.upper())
            except ValueError:
                selector_type = SelectorType.CSS
        return cls(
            name=data.get("name", ""),
            selector_type=selector_type,
            expression=data.get("expression", ""),
            attribute=data.get("attribute", ""),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "selector_type": self.selector_type.value,
            "expression": self.expression,
            "attribute": self.attribute,
        }


@dataclass
class ElementInfo:
    """可视化拾取的网页元素信息。"""
    description: str        # 元素描述，如 "div#header" 或 "<button class='btn'>"
    xpath: str              # 生成的 XPath 选择器
    css_selector: str       # 生成的 CSS 选择器
    action_type: str = ""   # 动作类型：input / click / highlight
    action_value: str = ""  # 动作值（如输入文本）


@dataclass
class CrawlConfig:
    """Full crawler configuration.

    Mirrors every field exposed by the UI "爬虫配置面板" requirement.

    Storage format valid values: "CSV", "JSON", "EXCEL", "SQLITE", "NONE".
    HTTP method valid values: "GET", "POST".
    Each proxy string looks like "http://host:port" or "socks5://host:port".
    """

    # Seed URLs and crawl scope
    start_urls: list[str] = field(default_factory=list)
    max_depth: int = 3
    max_pages: int = 100
    concurrency: int = 5
    request_interval: float = 0.5
    timeout: float = 30.0
    max_retries: int = 3
    max_redirects: int = 10

    # Request customization
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    proxies: list[str] = field(default_factory=list)
    user_agents: list[str] = field(default_factory=list)
    rotate_user_agents: bool = True

    # robots / domain filtering
    respect_robots: bool = True
    allowed_domains: list[str] = field(default_factory=list)
    restrict_to_start_domains: bool = True
    url_whitelist_regex: list[str] = field(default_factory=list)
    url_blacklist_regex: list[str] = field(default_factory=list)

    # Extraction
    extraction_rules: list[ExtractionRule] = field(default_factory=list)
    auto_follow_links: bool = True
    auto_detect_pagination: bool = True

    # Storage
    storage_format: str = "CSV"
    output_path: str = "output"

    # File download
    download_files: bool = False
    download_url_regex: str = ""
    download_dir: str = "downloads"

    # File download filter (extension / substring black-white lists)
    download_ext_whitelist: list[str] = field(default_factory=list)
    download_ext_blacklist: list[str] = field(default_factory=list)
    download_str_whitelist: list[str] = field(default_factory=list)
    download_str_blacklist: list[str] = field(default_factory=list)

    # Persistence / dedup
    persist_state_sqlite: bool = False

    # Compliance gating (status-code / robots / API detection)
    compliance_check_enabled: bool = True

    # HTTP method
    method: str = "GET"
    post_data: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "CrawlConfig":
        """Build a CrawlConfig from a (possibly partial) dict.

        Missing keys fall back to dataclass defaults. Nested ExtractionRule
        lists are reconstructed via ExtractionRule.from_dict.
        """

        if not isinstance(data, dict):
            return cls()

        # Build kwargs for every field declared on the dataclass.
        field_names = {f.name for f in dataclasses.fields(cls)}
        kwargs: dict[str, Any] = {}
        for name in field_names:
            if name not in data:
                continue
            value = data[name]
            if name == "extraction_rules" and isinstance(value, list):
                kwargs[name] = [
                    ExtractionRule.from_dict(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                kwargs[name] = value
        return cls(**kwargs)

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict representation."""

        result = dataclasses.asdict(self)
        # dataclasses.asdict recursively converts nested dataclasses to dicts,
        # but it leaves SelectorType enum values as-is. Convert them to plain
        # strings so the result is JSON-serializable.
        rules = result.get("extraction_rules")
        if isinstance(rules, list):
            result["extraction_rules"] = [
                {
                    "name": rule["name"],
                    "selector_type": (
                        rule["selector_type"].value
                        if isinstance(rule["selector_type"], SelectorType)
                        else str(rule["selector_type"])
                    ),
                    "expression": rule["expression"],
                    "attribute": rule["attribute"],
                }
                for rule in rules
            ]
        return result


@dataclass
class Response:
    """Structured download result returned by the Downloader.

    Attributes:
        url: Final URL after redirects.
        status: HTTP status code (0 if the request failed before a response).
        headers: Response headers (lower-cased keys optional, kept as-is).
        text: Decoded response body text.
        content: Raw response body bytes.
        encoding: Encoding used to decode text (may be detected by requests).
        elapsed_ms: Round-trip time in milliseconds.
        error: Non-empty when the download failed; contains the error message.
    """

    url: str
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    content: bytes = b""
    encoding: str = ""
    elapsed_ms: float = 0.0
    error: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Response":
        content = data.get("content", b"")
        # Tolerate content stored as a list of ints (JSON has no bytes type).
        if isinstance(content, list):
            content = bytes(content)
        return cls(
            url=data.get("url", ""),
            status=data.get("status", 0),
            headers=data.get("headers", {}),
            text=data.get("text", ""),
            content=content,
            encoding=data.get("encoding", ""),
            elapsed_ms=data.get("elapsed_ms", 0.0),
            error=data.get("error", ""),
        )

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "status": self.status,
            "headers": dict(self.headers),
            "text": self.text,
            "content": list(self.content),
            "encoding": self.encoding,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


@dataclass
class ResultItem:
    """A single extracted record from a page.

    Attributes:
        url: Source page URL the record was extracted from.
        fields: Mapping of field name -> extracted value. Multi-values are
            joined with " | " to keep the structure simple (str values only).
        raw_response: Optional raw HTML/text snapshot of the source response,
            used by the UI "view raw response" feature.
    """

    url: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    raw_response: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ResultItem":
        return cls(
            url=data.get("url", ""),
            fields=dict(data.get("fields", {})),
            raw_response=data.get("raw_response", ""),
        )

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "fields": dict(self.fields),
            "raw_response": self.raw_response,
        }


@dataclass
class CrawlStats:
    """Mutable runtime statistics, updated by the scheduler during a crawl.

    Attributes:
        pages_crawled: Number of pages actually downloaded.
        urls_discovered: Total number of URLs ever seen (incl. duplicates).
        queue_size: Current size of the pending URL queue.
        success: Number of successful page downloads.
        failed: Number of pages that failed after all retries.
        retries: Total number of retry attempts performed.
        status_codes: Mapping of HTTP status code -> count.
        started_at: Unix timestamp of crawl start (0 if not started).
        bytes_downloaded: Total response body bytes received.
    """

    pages_crawled: int = 0
    urls_discovered: int = 0
    queue_size: int = 0
    success: int = 0
    failed: int = 0
    retries: int = 0
    status_codes: dict[int, int] = field(default_factory=dict)
    started_at: float = 0.0
    bytes_downloaded: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "CrawlStats":
        status_codes = data.get("status_codes", {})
        # JSON keys are strings; coerce back to int where possible.
        coerced: dict[int, int] = {}
        for key, value in status_codes.items():
            try:
                coerced[int(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return cls(
            pages_crawled=data.get("pages_crawled", 0),
            urls_discovered=data.get("urls_discovered", 0),
            queue_size=data.get("queue_size", 0),
            success=data.get("success", 0),
            failed=data.get("failed", 0),
            retries=data.get("retries", 0),
            status_codes=coerced,
            started_at=data.get("started_at", 0.0),
            bytes_downloaded=data.get("bytes_downloaded", 0),
        )

    def to_dict(self) -> dict:
        return {
            "pages_crawled": self.pages_crawled,
            "urls_discovered": self.urls_discovered,
            "queue_size": self.queue_size,
            "success": self.success,
            "failed": self.failed,
            "retries": self.retries,
            "status_codes": {str(k): v for k, v in self.status_codes.items()},
            "started_at": self.started_at,
            "bytes_downloaded": self.bytes_downloaded,
        }


def now_ts() -> float:
    """Convenience helper returning the current unix timestamp.

    Provided here so scheduler/tests can centralize timestamp generation.
    """

    return time.time()


@dataclass
class PickerAction:
    """可视化拾取器设计的单个动作步骤（EasySpider 风格）。"""
    action_type: str        # input / click / collect / select_all / paginate / loop
    css_selector: str       # 目标元素的 CSS 选择器
    xpath: str = ""         # 目标元素的 XPath（可选，备用）
    description: str = ""   # 元素描述，如 "div#header"
    value: str = ""         # 输入文本值（仅 action_type == "input" 使用）
    field_name: str = ""    # 采集字段名（仅 action_type == "collect" 使用）
    extract_attr: str = "text"  # 采集属性：text / attr / html（仅 collect 使用）
    children: list = field(default_factory=list)  # 循环子动作（仅 action_type == "loop" 使用）

    def to_dict(self) -> dict:
        return {
            "action_type": self.action_type,
            "css_selector": self.css_selector,
            "xpath": self.xpath,
            "description": self.description,
            "value": self.value,
            "field_name": self.field_name,
            "extract_attr": self.extract_attr,
            "children": [c.to_dict() if isinstance(c, PickerAction) else c for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PickerAction":
        children_data = data.get("children") or []
        return cls(
            action_type=data.get("action_type", ""),
            css_selector=data.get("css_selector", ""),
            xpath=data.get("xpath", ""),
            description=data.get("description", ""),
            value=data.get("value", ""),
            field_name=data.get("field_name", ""),
            extract_attr=data.get("extract_attr", "text"),
            children=[cls.from_dict(c) if isinstance(c, dict) else c for c in children_data],
        )


@dataclass
class PickerTask:
    """可视化拾取器设计的完整任务（一组有序动作步骤）。"""
    name: str = ""
    actions: list = field(default_factory=list)  # list[PickerAction]
    created_at: str = ""
    version: str = "1.0"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "actions": [a.to_dict() if isinstance(a, PickerAction) else a for a in self.actions],
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PickerTask":
        actions_data = data.get("actions") or []
        return cls(
            name=data.get("name", ""),
            version=data.get("version", "1.0"),
            actions=[PickerAction.from_dict(a) if isinstance(a, dict) else a for a in actions_data],
            metadata=data.get("metadata", {}) or {},
            created_at=data.get("created_at", ""),
        )
