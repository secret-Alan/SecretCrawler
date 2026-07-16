"""HTTP downloader based on the `requests` library.

This module is UI-free (no PySide6 imports). It implements the "HTTP 下载器"
requirement of the crawler spec:

- GET / POST (POST sends ``config.post_data`` as form data)
- Custom request headers and cookies applied to every request, layered on top
  of sensible default headers (User-Agent / Accept / Accept-Language).
- Proxy pool rotation (round-robin, one proxy per ``fetch`` call).
- User-Agent rotation (round-robin, one UA per ``fetch`` call).
- Configurable timeout and max redirects.
- Retry with exponential backoff on connection errors, timeouts and 5xx
  responses (``min(base * 2**attempt, 60)`` seconds, base = 1.0).
- Response encoding auto-detection (``response.encoding`` then
  ``response.apparent_encoding``).
- A structured :class:`crawler.models.Response` is always returned; ``fetch``
  never raises.

The downloader is synchronous. Concurrency / scheduling is the scheduler's
job, not ours.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from crawler.models import CrawlConfig, Response

# Declared crawler identity User-Agent.
_DEFAULT_USER_AGENT = (
    "InternetCrawlerBot/1.0 "
    "(+https://github.com/secret-Alan/InternetCrawler)"
)

# Default headers applied to every request unless overridden by the user.
_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": _DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# Exponential backoff parameters (seconds).
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 60.0


class HTTPDownloader:
    """Synchronous HTTP downloader backed by a ``requests.Session``.

    A single instance is intended to be reused across many ``fetch`` calls so
    the underlying TCP connection pool stays warm. ``fetch`` is defensive: it
    never raises; on failure it returns a :class:`Response` with ``.error``
    set (and ``.status`` possibly 0).
    """

    def __init__(self, config: CrawlConfig, logger: logging.Logger | None = None):
        self._config = config
        self._logger = logger or logging.getLogger("crawler.downloader")

        # One Session per downloader instance gives us keep-alive and
        # connection pooling. max_redirects is a session-level setting.
        self._session = requests.Session()
        self._session.max_redirects = config.max_redirects

        # Round-robin counters for proxy and User-Agent selection. The lock
        # guards ONLY the counter increments (per spec); the actual HTTP call
        # is never serialized through this lock.
        self._lock = threading.Lock()
        self._proxy_index = 0
        self._ua_index = 0

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def fetch(self, url: str) -> Response:
        """Fetch a single URL with retries.

        Always returns a :class:`Response`. On total failure ``.error`` is
        non-empty and ``.status`` may be 0.
        """

        try:
            return self._fetch_with_retries(url)
        except Exception as exc:  # noqa: BLE001 - defensive: fetch must not raise
            self._logger.error(
                "fetch %s unexpected error: %s", url, exc, exc_info=True
            )
            return Response(
                url=url,
                status=0,
                error=f"{type(exc).__name__}: {exc}",
            )

    # ------------------------------------------------------------------ #
    # Retry loop
    # ------------------------------------------------------------------ #

    def _fetch_with_retries(self, url: str) -> Response:
        cfg = self._config
        log = self._logger

        # Pick proxy + UA once per fetch call. Round-robin counters persist
        # across fetch calls (and are advanced under the lock).
        proxy_url = self._pick_proxy()
        ua = self._pick_user_agent()

        # Layered headers: defaults -> UA override -> user-supplied (wins).
        headers: dict[str, str] = dict(_DEFAULT_HEADERS)
        if ua:
            headers["User-Agent"] = ua
        if cfg.headers:
            headers.update(cfg.headers)

        proxies = self._build_proxies(proxy_url)
        cookies = cfg.cookies or None
        post_data = cfg.post_data or None
        method = cfg.method.upper()

        total_attempts = max(1, cfg.max_retries + 1)
        last_error = ""
        last_response: Response | None = None

        for attempt in range(total_attempts):
            start = time.monotonic()
            try:
                if method == "POST":
                    raw = self._session.post(
                        url,
                        headers=headers,
                        cookies=cookies,
                        proxies=proxies,
                        timeout=cfg.timeout,
                        allow_redirects=True,
                        data=post_data,
                    )
                else:
                    raw = self._session.get(
                        url,
                        headers=headers,
                        cookies=cookies,
                        proxies=proxies,
                        timeout=cfg.timeout,
                        allow_redirects=True,
                    )
            except requests.exceptions.RequestException as exc:
                elapsed_ms = (time.monotonic() - start) * 1000.0
                last_error = f"{type(exc).__name__}: {exc}"
                last_response = None
                log.warning(
                    "fetch %s attempt %d/%d failed: %s (%.1f ms)",
                    url, attempt + 1, total_attempts, last_error, elapsed_ms,
                )
                if attempt < total_attempts - 1:
                    self._sleep_backoff(attempt)
                continue

            elapsed_ms = (time.monotonic() - start) * 1000.0
            status = raw.status_code

            # 5xx -> retryable.
            if 500 <= status < 600:
                last_error = f"HTTP {status}"
                last_response = self._build_response(
                    raw, url, elapsed_ms, error=last_error
                )
                log.warning(
                    "fetch %s attempt %d/%d got HTTP %d (%.1f ms); retrying",
                    url, attempt + 1, total_attempts, status, elapsed_ms,
                )
                if attempt < total_attempts - 1:
                    self._sleep_backoff(attempt)
                continue

            # Success path: 2xx / 3xx / 4xx (non-5xx).
            result = self._build_response(raw, url, elapsed_ms, error="")
            log.info(
                "fetch %s -> %d (%.1f ms)",
                result.url, result.status, result.elapsed_ms,
            )
            return result

        # All retries exhausted.
        if last_response is not None:
            # Last attempt returned a 5xx response; reuse its fields and mark
            # the error so the caller can detect failure.
            last_response.error = last_error or "all retries failed"
            log.error(
                "fetch %s failed after %d attempts: %s",
                url, total_attempts, last_response.error,
            )
            return last_response

        log.error(
            "fetch %s failed after %d attempts: %s",
            url, total_attempts, last_error or "all retries failed",
        )
        return Response(
            url=url,
            status=0,
            error=last_error or "all retries failed",
        )

    # ------------------------------------------------------------------ #
    # Round-robin selection helpers (lock-guarded increments only)
    # ------------------------------------------------------------------ #

    def _pick_proxy(self) -> str | None:
        proxies = self._config.proxies
        if not proxies:
            return None
        with self._lock:
            idx = self._proxy_index % len(proxies)
            self._proxy_index = (self._proxy_index + 1) % len(proxies)
        return proxies[idx]

    def _pick_user_agent(self) -> str:
        uas = self._config.user_agents
        if not uas:
            return _DEFAULT_USER_AGENT
        if not self._config.rotate_user_agents:
            return uas[0]
        with self._lock:
            idx = self._ua_index % len(uas)
            self._ua_index = (self._ua_index + 1) % len(uas)
        return uas[idx]

    # ------------------------------------------------------------------ #
    # Small static helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_proxies(proxy_url: str | None) -> dict[str, str] | None:
        """Convert a single proxy URL into requests' proxies dict.

        ``socks5://`` URLs are passed through verbatim; PySocks (if installed)
        handles them. Returns None when no proxy is configured so requests
        uses the environment / direct connection.
        """

        if not proxy_url:
            return None
        return {"http": proxy_url, "https": proxy_url}

    @staticmethod
    def _build_response(
        raw: requests.Response,
        request_url: str,
        elapsed_ms: float,
        error: str,
    ) -> Response:
        """Translate a ``requests.Response`` into our :class:`Response`."""

        # Encoding auto-detection: prefer the server-declared encoding, else
        # fall back to chardet's apparent_encoding sniffed from the body.
        encoding = raw.encoding or raw.apparent_encoding or ""
        if encoding and not raw.encoding:
            # Ensure .text decodes with the detected encoding (apparent_encoding
            # is not applied automatically by .text in all requests versions).
            raw.encoding = encoding

        try:
            text = raw.text
        except (UnicodeDecodeError, LookupError):
            # Lossless fallback so callers always get a str.
            text = (raw.content or b"").decode("latin-1", errors="replace")

        try:
            header_dict = dict(raw.headers)
        except Exception:  # noqa: BLE001 - headers should never fail, but be safe
            header_dict = {}

        return Response(
            url=raw.url or request_url,
            status=raw.status_code,
            headers=header_dict,
            text=text,
            content=raw.content or b"",
            encoding=encoding,
            elapsed_ms=elapsed_ms,
            error=error,
        )

    def _sleep_backoff(self, attempt: int) -> None:
        """Sleep ``min(base * 2**attempt, 60)`` seconds before the next retry."""

        delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)
        if delay > 0:
            time.sleep(delay)
