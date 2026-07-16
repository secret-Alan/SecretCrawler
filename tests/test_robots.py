"""robots.txt 合规检查器（crawler.robots.RobotsChecker）单元测试。

覆盖 spec 中「robots.txt 遵守」需求：允许/禁止判定、缓存命中、缺失 robots
视为全部允许、respect_robots=False 短路放行。全部离线，使用注入的假 fetcher。
"""
from __future__ import annotations

from crawler.models import CrawlConfig, Response
from crawler.robots import RobotsChecker


class _CountingFetcher:
    """记录调用次数的假 fetcher，对所有 URL 返回同一预设 Response。"""

    def __init__(self, response: Response):
        self.response = response
        self.calls = 0

    def __call__(self, url: str) -> Response:
        self.calls += 1
        return self.response


def test_robots_allow_and_deny():
    """robots.txt 禁止 /private → /public 允许、/private/secret 拒绝。"""
    body = "User-agent: *\nDisallow: /private\n"
    fetcher = _CountingFetcher(
        Response(url="https://site.com/robots.txt", status=200, text=body)
    )
    checker = RobotsChecker(CrawlConfig(respect_robots=True), fetcher=fetcher)
    assert checker.allowed("https://site.com/public") is True
    assert checker.allowed("https://site.com/private/secret") is False


def test_robots_cache_fetcher_called_once():
    """同域名二次查询命中缓存，fetcher 仅被调用一次。"""
    body = "User-agent: *\nDisallow: /private\n"
    fetcher = _CountingFetcher(
        Response(url="https://site.com/robots.txt", status=200, text=body)
    )
    checker = RobotsChecker(CrawlConfig(respect_robots=True), fetcher=fetcher)
    checker.allowed("https://site.com/a")
    checker.allowed("https://site.com/b")
    assert fetcher.calls == 1


def test_missing_robots_allows_all():
    """fetcher 返回 404/error → 视为全部允许。"""
    fetcher = _CountingFetcher(
        Response(
            url="https://site.com/robots.txt",
            status=404,
            text="",
            error="404 Not Found",
        )
    )
    checker = RobotsChecker(CrawlConfig(respect_robots=True), fetcher=fetcher)
    assert checker.allowed("https://site.com/anything") is True


def test_respect_robots_false_short_circuits():
    """respect_robots=False 时始终放行，且不调用 fetcher。"""
    body = "User-agent: *\nDisallow: /\n"
    fetcher = _CountingFetcher(
        Response(url="https://site.com/robots.txt", status=200, text=body)
    )
    checker = RobotsChecker(CrawlConfig(respect_robots=False), fetcher=fetcher)
    # 即便 robots 全禁，也放行
    assert checker.allowed("https://site.com/private/secret") is True
    assert fetcher.calls == 0
