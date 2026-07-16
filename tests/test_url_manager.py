"""URL 管理器（crawler.url_manager.URLManager）单元测试。

覆盖 spec 中「URL 管理器」需求：规范化、去重、域名/正则过滤、广度优先深度、
SQLite 持久化、非 http(s) 协议拒绝。全部离线，无网络依赖。
"""
from __future__ import annotations

import sqlite3

from crawler.models import CrawlConfig
from crawler.url_manager import URLManager


def _cfg(**kwargs) -> CrawlConfig:
    """构造测试用 CrawlConfig。

    默认关闭起始域名限制、清空 start_urls，使任意 host 都能通过域名过滤，
    便于聚焦被测行为。单个测试可覆盖这些默认。
    """
    kwargs.setdefault("restrict_to_start_domains", False)
    kwargs.setdefault("start_urls", [])
    return CrawlConfig(**kwargs)


def test_normalize_dedup_hits():
    """规范化后 https://Example.com:443/a#frag 与 https://example.com/a 视为同一 URL。"""
    mgr = URLManager(_cfg())
    assert mgr.add("https://Example.com:443/a#frag") is True
    # 规范化（去 fragment、小写 host、去默认端口 443）后两者相同 → 去重命中
    assert mgr.add("https://example.com/a") is False
    # seen() 对带端口的等价 URL 也应返回 True
    assert mgr.seen("https://example.com:443/a#section") is True
    mgr.close()


def test_add_same_url_twice_returns_true_then_false():
    """同一 URL 第二次添加返回 False。"""
    mgr = URLManager(_cfg())
    assert mgr.add("https://example.com/page1") is True
    assert mgr.add("https://example.com/page1") is False
    mgr.close()


def test_restrict_to_start_domains_filters_other_host():
    """启用 restrict_to_start_domains 时，外域 URL 被过滤。"""
    config = CrawlConfig(
        start_urls=["https://example.com/"],
        restrict_to_start_domains=True,
    )
    mgr = URLManager(config)
    # 同域 → 接受
    assert mgr.add("https://example.com/page") is True
    # 外域 → 拒绝
    assert mgr.add("https://other.com/x") is False
    mgr.close()


def test_allowed_domains_filter():
    """allowed_domains 非空时，仅允许列表中的域名入队。"""
    config = CrawlConfig(
        allowed_domains=["example.com"],
        restrict_to_start_domains=False,
    )
    mgr = URLManager(config)
    assert mgr.add("https://example.com/a") is True
    assert mgr.add("https://other.com/b") is False
    mgr.close()


def test_whitelist_regex_rejects_non_matching():
    """url_whitelist_regex 非空时，不匹配的 URL 被拒绝。"""
    config = CrawlConfig(
        url_whitelist_regex=["/article/"],
        restrict_to_start_domains=False,
    )
    mgr = URLManager(config)
    assert mgr.add("https://example.com/article/1") is True
    assert mgr.add("https://example.com/page/1") is False
    mgr.close()


def test_blacklist_regex_rejects_matching():
    """url_blacklist_regex 匹配的 URL 被拒绝。"""
    config = CrawlConfig(
        url_blacklist_regex=["/block"],
        restrict_to_start_domains=False,
    )
    mgr = URLManager(config)
    assert mgr.add("https://example.com/ok") is True
    assert mgr.add("https://example.com/block/secret") is False
    mgr.close()


def test_get_returns_depth_from_add():
    """get() 返回的 depth 与 add() 传入的 depth 一致（广度优先）。"""
    mgr = URLManager(_cfg())
    assert mgr.add("https://example.com/depth5", depth=5) is True
    item = mgr.get()
    assert item is not None
    url, depth = item
    assert url == "https://example.com/depth5"
    assert depth == 5
    # 队列空后 get 返回 None
    assert mgr.get() is None
    mgr.close()


def test_sqlite_persistence_survives_reopen(tmp_path):
    """persist_state_sqlite=True 时，close + reopen 后 seen() 仍为 True。"""
    db_path = str(tmp_path / "urls.db")
    config = CrawlConfig(
        persist_state_sqlite=True,
        output_path=db_path,
        restrict_to_start_domains=False,
    )
    mgr1 = URLManager(config)
    assert mgr1.add("https://example.com/persistent") is True
    mgr1.close()

    # 直接读取数据库文件，确认 URL 已落盘
    with sqlite3.connect(db_path) as conn:
        rows = [r[0] for r in conn.execute("SELECT url FROM seen_urls").fetchall()]
    assert "https://example.com/persistent" in rows

    # 重新构造 URLManager，应从 SQLite 恢复 seen 集合
    mgr2 = URLManager(config)
    assert mgr2.seen("https://example.com/persistent") is True
    assert mgr2.seen("https://example.com/never-added") is False
    mgr2.close()


def test_non_http_schemes_rejected():
    """mailto: / javascript: / tel: / data: 等非 http(s) 协议被拒绝。"""
    mgr = URLManager(_cfg())
    assert mgr.add("mailto:test@example.com") is False
    assert mgr.add("javascript:void(0)") is False
    assert mgr.add("tel:+15551234") is False
    assert mgr.add("data:text/plain,hello") is False
    mgr.close()
