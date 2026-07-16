"""内容解析器（crawler.parser.ContentParser）单元测试。

覆盖 spec 中「内容解析器」需求：CSS 文本/属性、正则、JSON 路径、链接发现、
多值合并、缺失返回空、XPath（lxml 可用时；不可用则降级返回空）。全部离线。
"""
from __future__ import annotations

import pytest

from crawler.models import CrawlConfig, ExtractionRule, SelectorType
from crawler.parser import ContentParser


def _parser() -> ContentParser:
    """构造默认 ContentParser。"""
    return ContentParser(CrawlConfig())


def test_css_text_extraction():
    """CSS 选择器提取文本：h1.t → "Hello"。"""
    html = '<h1 class="t">Hello</h1>'
    rule = ExtractionRule(
        name="title", selector_type=SelectorType.CSS, expression="h1.t"
    )
    item = _parser().extract("https://x.com/", html, [rule])
    assert item.fields["title"] == "Hello"


def test_css_attribute_extraction():
    """CSS 选择器提取属性：a 的 href → "/x"。"""
    html = '<a href="/x">link</a>'
    rule = ExtractionRule(
        name="link",
        selector_type=SelectorType.CSS,
        expression="a",
        attribute="href",
    )
    item = _parser().extract("https://x.com/", html, [rule])
    assert item.fields["link"] == "/x"


def test_regex_extraction():
    """正则提取分组：id=(\\d+) → "123 | 456"。"""
    text = "some page id=123 and more id=456"
    rule = ExtractionRule(
        name="id", selector_type=SelectorType.REGEX, expression=r"id=(\d+)"
    )
    item = _parser().extract("https://x.com/", text, [rule])
    # 多个匹配用 " | " 连接
    assert item.fields["id"] == "123 | 456"


def test_json_path_extraction():
    """JSON 路径 data.items[*].title → "a | b"。"""
    text = '{"data":{"items":[{"title":"a"},{"title":"b"}]}}'
    rule = ExtractionRule(
        name="titles",
        selector_type=SelectorType.JSON,
        expression="data.items[*].title",
    )
    item = _parser().extract("https://x.com/", text, [rule])
    assert item.fields["titles"] == "a | b"


def test_extract_links_resolves_relative():
    """extract_links 将相对链接解析为绝对链接，丢弃非 http(s) scheme。"""
    html = '<a href="/a">a</a><a href="/b">b</a><a href="mailto:x@y">m</a>'
    links = _parser().extract_links("https://x.com/", html)
    assert links == ["https://x.com/a", "https://x.com/b"]


def test_multi_value_css_joined_with_pipe():
    """多个匹配元素的文本用 " | " 连接。"""
    html = "<ul><li>a</li><li>b</li></ul>"
    rule = ExtractionRule(name="items", selector_type=SelectorType.CSS, expression="li")
    item = _parser().extract("https://x.com/", html, [rule])
    assert item.fields["items"] == "a | b"


def test_missing_match_returns_empty_string():
    """未命中选择器返回空字符串而非崩溃。"""
    html = "<div>nothing here</div>"
    rule = ExtractionRule(
        name="missing", selector_type=SelectorType.CSS, expression="h1.absent"
    )
    item = _parser().extract("https://x.com/", html, [rule])
    assert item.fields["missing"] == ""


def test_xpath_extraction_when_lxml_available():
    """lxml 可用时，XPath //h1/text() → "Hello"。"""
    pytest.importorskip("lxml")
    html = "<html><body><h1>Hello</h1></body></html>"
    rule = ExtractionRule(
        name="h1", selector_type=SelectorType.XPATH, expression="//h1/text()"
    )
    item = _parser().extract("https://x.com/", html, [rule])
    assert item.fields["h1"] == "Hello"


def test_xpath_degraded_when_lxml_missing(monkeypatch):
    """lxml 不可用时（_etree=None），XPath 规则降级返回空字符串。"""
    import crawler.parser as parser_mod

    monkeypatch.setattr(parser_mod, "_etree", None)
    html = "<h1>Hello</h1>"
    rule = ExtractionRule(
        name="h1", selector_type=SelectorType.XPATH, expression="//h1/text()"
    )
    item = _parser().extract("https://x.com/", html, [rule])
    assert item.fields["h1"] == ""
