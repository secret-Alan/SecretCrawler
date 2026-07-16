"""爬虫调度器。

实现 spec 中「调度器」需求，协调下载、解析、存储的并发执行：

- 基于 ``concurrent.futures.ThreadPoolExecutor`` 的多线程并发（7.1）
- 全局请求间隔限速：跨线程共享的最小请求间隔（7.2）
- 最大深度控制（起始 URL 深度 0，发现的链接深度 +1）与最大页面数限制（7.3）
- 状态机：IDLE / RUNNING / PAUSED / STOPPING / STOPPED，支持暂停 / 继续 / 优雅停止（7.4）
- 运行时统计（``CrawlStats``）通过回调上抛（7.5）
- 异常隔离：整个 worker 循环体被 try/except 包裹，单个页面异常不会导致调度器崩溃（7.5）

本模块不依赖 PySide6（UI-free），通过回调与 UI 层解耦。所有回调均在工作线程
中被调用，UI 层需自行将回调 marshal 到 UI 线程。

限速说明（7.2）：采用「全局请求间最小间隔」策略——用一个 ``threading.Lock``
加 ``last_request_time`` 记录最近一次请求开始时刻，每次下载前计算需要等待
的时间并 sleep。这不是严格意义的令牌桶，但能满足「请求间隔」需求：全局
范围内任意两次下载开始之间至少间隔 ``config.request_interval`` 秒。

重试计数说明（7.5）：下载器内部已完成重试并记录日志，但未将重试次数暴露给
调度器，因此 ``CrawlStats.retries`` 始终保持为 0，无法精确统计。如需精确计数，
需扩展下载器 API，本模块不做臆造估算。
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as _wait_futures
from enum import Enum
from typing import Callable

from crawler.downloader import HTTPDownloader
from crawler.models import CrawlConfig, CrawlStats, ResultItem, now_ts
from crawler.parser import ContentParser
from crawler.pipeline import Pipeline
from crawler.robots import RobotsChecker
from crawler.status_handler import Action, StatusHandler
from crawler.url_manager import URLManager

# 回调签名（均在工作线程中调用；UI 层需自行 marshal 到 UI 线程）
PageCrawledCb = Callable[[str, int], None]          # (url, status)
ResultFoundCb = Callable[[ResultItem], None]
StatsUpdatedCb = Callable[[CrawlStats], None]
LogCb = Callable[[int, str], None]                  # (level, message)，level 取自 logging 常量
# Task 7 / 11：状态码警告与认证需求回调（工作线程中调用）
StatusWarningCb = Callable[[str, int, str], None]   # (url, status, message)
AuthRequiredCb = Callable[[str, str], None]         # (url, reason)

# 暂停时的轮询间隔（秒）；与 spec「sleep 0.1s and recheck」一致
_PAUSE_POLL_INTERVAL = 0.1
# 队列暂时为空时的宽限等待（秒）：给其它 worker 留出补充链接的时间，避免过早退出
_EMPTY_QUEUE_GRACE = 0.2


def _fmt_log(level_name: str, module: str, source_url: str, current_url: str, depth: int, msg: str) -> str:
    """统一日志格式：[时间戳] [级别] [模块] (来源URL→当前URL, 深度N) 消息。"""
    from datetime import datetime, timezone, timedelta
    ts = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    src = source_url or "-"
    return f"[{ts}] [{level_name}] [{module}] ({src}→{current_url}, 深度{depth}) {msg}"


class _State(Enum):
    """调度器内部状态机。"""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class Scheduler:
    """爬虫调度器，协调下载 / 解析 / 存储的并发执行。

    典型用法::

        scheduler = Scheduler(config, downloader, url_manager, ...)
        scheduler.start()                 # 非阻塞：提交工作线程后立即返回
        while scheduler.is_running:       # 调用方轮询或等待
            time.sleep(0.1)
        print(scheduler.stats.pages_crawled)

    线程安全：状态、统计、限速均通过各自的锁保护，可在多线程下调用
    ``start`` / ``pause`` / ``resume`` / ``stop`` 与读取 ``stats`` / ``is_running``。

    Args:
        config: 爬虫配置（``CrawlConfig``）。
        downloader: HTTP 下载器实例。
        url_manager: URL 管理器实例。
        robots_checker: robots.txt 检查器。
        parser: 内容解析器。
        pipeline: 数据管道（``Pipeline``）。
        logger: 可选日志器；默认 ``logging.getLogger("crawler.scheduler")``。
        on_page_crawled: 单页抓取完成回调 ``callback(url, status)``。
        on_result_found: 解析出结果回调 ``callback(item)``。
        on_stats_updated: 统计更新回调 ``callback(stats_snapshot)``，传入的是快照副本。
        on_log: 日志回调 ``callback(level, message)``，``level`` 取自 ``logging`` 常量。
    """

    def __init__(
        self,
        config: CrawlConfig,
        downloader: HTTPDownloader,
        url_manager: URLManager,
        robots_checker: RobotsChecker,
        parser: ContentParser,
        pipeline: Pipeline,
        logger: logging.Logger | None = None,
        on_page_crawled: PageCrawledCb | None = None,
        on_result_found: ResultFoundCb | None = None,
        on_stats_updated: StatsUpdatedCb | None = None,
        on_log: LogCb | None = None,
        status_handler: StatusHandler | None = None,
        on_status_warning: StatusWarningCb | None = None,
        on_auth_required: AuthRequiredCb | None = None,
    ) -> None:
        self._config = config
        self._downloader = downloader
        self._url_manager = url_manager
        self._robots_checker = robots_checker
        self._parser = parser
        self._pipeline = pipeline
        self._logger = logger or logging.getLogger("crawler.scheduler")

        # 回调
        self._on_page_crawled = on_page_crawled
        self._on_result_found = on_result_found
        self._on_stats_updated = on_stats_updated
        self._on_log = on_log
        # Task 7 / 11：状态码警告与认证需求回调
        self._status_handler = status_handler
        self._on_status_warning = on_status_warning
        self._on_auth_required = on_auth_required
        # Task 7：SLOW_DOWN 动作写入的请求间隔覆盖值（None 表示用 config 默认值）
        self._interval_override: float | None = None

        # 状态机
        self._state: _State = _State.IDLE
        self._state_lock = threading.Lock()

        # 暂停 / 停止信号事件
        # _pause_event：set 表示已暂停（worker 不再取新任务）
        # _stop_event：set 表示已请求停止（worker 优雅退出）
        self._pause_event = threading.Event()
        self._stop_event = threading.Event()

        # 运行时统计
        self._stats = CrawlStats()
        self._stats_lock = threading.Lock()

        # 全局限速：保护 _last_request_time
        self._rate_lock = threading.Lock()
        self._last_request_time = 0.0

        # 工作线程池与监控线程
        self._executor: ThreadPoolExecutor | None = None
        self._futures: list = []
        self._monitor_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """启动调度器：种子 URL 入队、设置运行状态、提交工作线程。

        非阻塞：提交工作线程后立即返回。调用方通过轮询 ``is_running`` 或
        ``stats`` 获知进度。仅当处于 ``IDLE`` 状态时可启动；其它状态调用
        将记录告警并返回（一个 ``Scheduler`` 实例对应一次爬取任务）。
        """
        # 原子地检查并转换状态，防止重复 start
        with self._state_lock:
            if self._state != _State.IDLE:
                self._logger.warning(
                    "调度器不在 IDLE 状态，无法启动 (当前: %s)", self._state.value
                )
                return
            self._state = _State.RUNNING

        # 清理信号
        self._stop_event.clear()
        self._pause_event.clear()
        self._last_request_time = 0.0

        # 种子 URL 入队（深度 0）
        seeded = self._url_manager.add_many(list(self._config.start_urls), 0)
        with self._stats_lock:
            self._stats.started_at = now_ts()

        concurrency = max(1, self._config.concurrency)
        self._log(
            logging.INFO,
            _fmt_log(
                "INFO",
                "scheduler",
                "-",
                "-",
                0,
                f"爬虫已启动：种子 URL {seeded} 个，并发 {concurrency}，"
                f"max_depth={self._config.max_depth}，max_pages={self._config.max_pages}",
            ),
        )

        # 创建线程池并提交 concurrency 个工作循环
        self._executor = ThreadPoolExecutor(max_workers=concurrency)
        self._futures = [
            self._executor.submit(self._worker_loop) for _ in range(concurrency)
        ]

        # 监控线程：等待所有 worker 退出后转换到 STOPPED 并清理资源
        self._monitor_thread = threading.Thread(
            target=self._monitor, name="scheduler-monitor", daemon=True
        )
        self._monitor_thread.start()

    def pause(self) -> None:
        """暂停调度器：工作线程停止取新任务，进行中的请求自然完成。"""
        with self._state_lock:
            if self._state != _State.RUNNING:
                return
            self._state = _State.PAUSED
        self._pause_event.set()
        self._log(logging.INFO, _fmt_log("INFO", "scheduler", "-", "-", 0, "爬虫已暂停"))
        self._fire_on_stats_updated()

    def resume(self) -> None:
        """从暂停状态恢复抓取。"""
        with self._state_lock:
            if self._state != _State.PAUSED:
                return
            self._state = _State.RUNNING
        self._pause_event.clear()
        self._log(logging.INFO, _fmt_log("INFO", "scheduler", "-", "-", 0, "爬虫已恢复"))
        self._fire_on_stats_updated()

    def stop(self) -> None:
        """请求优雅停止：设置 STOPPING，工作线程完成当前请求后退出。

        不会提交新任务。所有工作线程退出后，监控线程将状态转为 ``STOPPED``
        并调用 ``pipeline.close()`` / ``url_manager.close()``。
        """
        with self._state_lock:
            if self._state in (_State.STOPPING, _State.STOPPED):
                return  # 已在停止 / 已停止
            self._state = _State.STOPPING
        self._stop_event.set()
        # 解除暂停，让 worker 能看到停止信号并退出
        self._pause_event.clear()
        self._log(logging.INFO, _fmt_log("INFO", "scheduler", "-", "-", 0, "爬虫停止中…"))
        self._fire_on_stats_updated()

    @property
    def stats(self) -> CrawlStats:
        """返回当前统计的快照副本（线程安全）。"""
        return self._snapshot_stats()

    @property
    def is_running(self) -> bool:
        """调度器是否仍在运行（含 RUNNING / PAUSED / STOPPING）。

        当所有工作线程退出、状态转为 ``STOPPED`` 时返回 ``False``。
        """
        with self._state_lock:
            return self._state in (_State.RUNNING, _State.PAUSED, _State.STOPPING)

    # ------------------------------------------------------------------ #
    # 工作线程
    # ------------------------------------------------------------------ #

    def _worker_loop(self) -> None:
        """工作线程主循环。

        每轮：检查状态 -> 检查 max_pages -> 取任务 -> 处理页面。
        整个循环体被 try/except 包裹，任何未预期异常都被捕获、记录并
        计入 failed，循环继续——单个页面异常不会终止调度器。
        """
        while True:
            try:
                # 1. 检查停止信号
                if self._stop_event.is_set():
                    break
                # 1b. 检查暂停：暂停时短暂等待并重新检查（等待可被停止信号中断）
                if self._pause_event.is_set():
                    if self._stop_event.wait(_PAUSE_POLL_INTERVAL):
                        break
                    continue

                # 2. max_pages 快速路径检查（只读，可能略有过冲；
                #    严格的配额控制由 _claim_page_slot 保证）
                if self._stats.pages_crawled >= self._config.max_pages:
                    break

                # 3. 从队列取任务
                task = self._url_manager.get()
                if task is None:
                    # 队列暂时为空：短暂宽限等待，防止其它 worker 正在
                    # 处理页面、即将补充新链接时本线程过早退出而损失并行度
                    if self._stop_event.wait(_EMPTY_QUEUE_GRACE):
                        break
                    task = self._url_manager.get()
                    if task is None:
                        break  # 队列确认为空，本 worker 退出

                url, depth = task
                self._process_page(url, depth)
            except Exception:
                # 捕获所有未预期异常，记录 traceback，计入失败，继续循环
                tb = traceback.format_exc()
                self._logger.error("worker 未预期异常: %s", tb)
                self._log(
                    logging.ERROR,
                    _fmt_log("ERROR", "scheduler", "-", "-", 0, f"worker 未预期异常:\n{tb}"),
                )
                with self._stats_lock:
                    self._stats.failed += 1
                continue

    def _process_page(self, url: str, depth: int) -> None:
        """处理单个页面：限速 -> robots -> 占配额 -> 下载 -> 统计 -> 解析 -> 存储 -> 链接发现 -> 回调。"""
        # 4. 全局限速
        self._rate_limit()
        # 限速睡眠结束后再次检查停止信号
        if self._stop_event.is_set():
            return

        # 5. robots 检查：被禁止则跳过（不计入 success/failure）
        if self._config.respect_robots and not self._robots_checker.allowed(url):
            self._log(
                logging.WARNING,
                _fmt_log("WARNING", "scheduler", "-", url, depth, "robots 禁止抓取，跳过"),
            )
            return

        # 6. 原子占用页面配额，严格保证不超过 max_pages
        if not self._claim_page_slot():
            return  # 已达 max_pages 上限

        # 7. 下载（下载器内部已 try/except，正常情况下不会抛异常）
        try:
            response = self._downloader.fetch(url)
        except Exception:
            # 防御性兜底：下载器自身不应抛异常
            tb = traceback.format_exc()
            self._logger.error("下载异常 %s: %s", url, tb)
            self._log(
                logging.ERROR,
                _fmt_log("ERROR", "scheduler", "-", url, depth, f"下载异常:\n{tb}"),
            )
            self._record_failure(url, 0)
            return

        status = response.status
        is_failure = bool(response.error) or status >= 400

        # 8. 更新统计（success / failed / status_codes / bytes / queue / discovered）
        self._record_page_status(url, response, status, is_failure)

        # 8b. 状态码合规处理（Task 7）：在统计记录之后、解析之前，根据
        #     StatusHandler 的决策跳过 / 重试 / 降速 / 暂停询问用户。
        if self._status_handler is not None:
            decision = self._status_handler.handle(status, url)
            if decision.action is Action.SKIP:
                self._log(
                    logging.INFO,
                    _fmt_log("INFO", "scheduler", "-", url, depth, decision.message),
                )
                self._fire_page_callbacks(url, status)
                return
            if decision.action is Action.SLOW_DOWN:
                if decision.new_interval is not None:
                    self._interval_override = decision.new_interval
                self._log(
                    logging.WARNING,
                    _fmt_log("WARNING", "scheduler", "-", url, depth, decision.message),
                )
                # 降速后重试一次
                try:
                    response = self._downloader.fetch(url)
                    status = response.status
                except Exception:
                    pass
            if decision.action is Action.RETRY:
                try:
                    response = self._downloader.fetch(url)
                    status = response.status
                except Exception:
                    pass
            if decision.action is Action.PAUSE_ASK_USER:
                self._log(
                    logging.WARNING,
                    _fmt_log("WARNING", "scheduler", "-", url, depth, decision.message),
                )
                if self._on_status_warning is not None:
                    try:
                        self._on_status_warning(url, status, decision.message)
                    except Exception:
                        pass
                # 暂停调度器，worker 不再取新任务，等待 UI 恢复 / 停止
                self._pause_event.set()
                with self._state_lock:
                    if self._state == _State.RUNNING:
                        self._state = _State.PAUSED
                # 不在此 worker 中死循环等待；直接返回，由 poll 处理后续
                self._fire_page_callbacks(url, status)
                return

        # 重试 / 降速重试后 status 可能变化，重新计算失败标志
        is_failure = bool(response.error) or status >= 400

        self._log(
            logging.INFO if not is_failure else logging.WARNING,
            _fmt_log(
                "INFO" if not is_failure else "WARNING",
                "scheduler",
                "-",
                url,
                depth,
                f"抓取 {url} -> {status}"
                + (f" ({response.error})" if is_failure else ""),
            ),
        )

        # 9 & 10. 仅成功时解析 + 存储 + 链接发现（失败则跳过解析）
        if not is_failure:
            self._handle_success(url, depth, response)

        # 10b. 认证检测（Task 11）：成功解析后，若响应表明需要登录且用户未
        #      提供认证信息，则回调 UI 并暂停调度器，等待用户填写认证后恢复。
        if self._on_auth_required is not None:
            try:
                from crawler.auth_detector import should_prompt_auth

                if should_prompt_auth(response, self._config):
                    reason = (
                        f"HTTP {status}"
                        if status in (401, 407)
                        else "检测到登录页特征"
                    )
                    try:
                        self._on_auth_required(url, reason)
                    except Exception:
                        pass
                    self._pause_event.set()
                    with self._state_lock:
                        if self._state == _State.RUNNING:
                            self._state = _State.PAUSED
            except Exception:
                pass

        # 11. 页面完成回调 + 统计回调
        self._fire_page_callbacks(url, status)

    # ------------------------------------------------------------------ #
    # 处理成功路径：解析 / 存储 / 链接发现
    # ------------------------------------------------------------------ #

    def _handle_success(self, url: str, depth: int, response) -> None:
        """成功响应的后续处理：字段提取 -> 管道存储 -> 结果回调 -> 链接发现。

        解析与链接发现各自独立 try/except，保证解析失败不影响链接发现。
        """
        # 9. 字段提取
        item: ResultItem | None = None
        try:
            item = self._parser.extract(url, response.text, self._config.extraction_rules)
        except Exception:
            tb = traceback.format_exc()
            self._logger.error("解析异常 %s: %s", url, tb)
            self._log(
                logging.ERROR,
                _fmt_log("ERROR", "scheduler", "-", url, depth, f"解析异常:\n{tb}"),
            )

        if item is not None:
            # 9a. 管道存储
            try:
                self._pipeline.process(item)
            except Exception as exc:
                self._logger.error("pipeline.process 异常 %s: %s", url, exc, exc_info=True)
                self._log(
                    logging.ERROR,
                    _fmt_log("ERROR", "scheduler", "-", url, depth, f"存储异常: {exc}"),
                )
            # 9b. 结果回调
            if self._on_result_found is not None:
                try:
                    self._on_result_found(item)
                except Exception as exc:
                    self._logger.error("on_result_found 回调异常: %s", exc, exc_info=True)

        # 10. 链接发现（深度未超上限时）
        if depth < self._config.max_depth:
            try:
                links: list[str] = []
                if self._config.auto_follow_links:
                    links.extend(self._parser.extract_links(url, response.text))
                # extract_pagination_links 内部已检查 auto_detect_pagination 开关
                links.extend(self._parser.extract_pagination_links(url, response.text))
                if links:
                    added = self._url_manager.add_many(links, depth + 1)
                    if added:
                        self._log(
                            logging.DEBUG,
                            _fmt_log(
                                "DEBUG",
                                "scheduler",
                                "-",
                                url,
                                depth,
                                f"发现 {added} 个新链接 (depth={depth + 1})",
                            ),
                        )
            except Exception as exc:
                self._logger.error("链接发现异常 %s: %s", url, exc, exc_info=True)
                self._log(
                    logging.ERROR,
                    _fmt_log("ERROR", "scheduler", "-", url, depth, f"链接发现异常: {exc}"),
                )

    # ------------------------------------------------------------------ #
    # 限速（7.2）
    # ------------------------------------------------------------------ #

    def _rate_limit(self) -> None:
        """全局请求间最小间隔限速。

        用 ``threading.Lock`` 保护 ``_last_request_time``。每次调用：
        1. 计算距离上次请求开始时刻的间隔，得出需等待的时间 ``wait``；
        2. 将 ``_last_request_time`` 预约为 ``now + wait``（这样并发线程
           会依次预约到间隔递增的时刻，精确实现全局 QPS = 1/request_interval）；
        3. 锁外 sleep ``wait``（可被停止信号中断，避免停止时长时间阻塞）。

        在锁内仅做时间计算与预约，不持有锁睡眠，从而允许多个 worker 并行预约。
        """
        interval = (
            self._interval_override
            if self._interval_override is not None
            else self._config.request_interval
        )
        if interval <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            wait = interval - (now - self._last_request_time)
            if wait < 0:
                wait = 0.0
            # 预约本次请求的开始时刻
            self._last_request_time = now + wait
        if wait > 0:
            # 可中断的等待：停止信号到达时立即返回
            self._stop_event.wait(wait)

    # ------------------------------------------------------------------ #
    # 统计与配额
    # ------------------------------------------------------------------ #

    def _claim_page_slot(self) -> bool:
        """原子占用一个页面配额，严格保证 ``pages_crawled`` 不超过 ``max_pages``。

        返回 ``True`` 表示占用成功，可继续下载；``False`` 表示已达上限，
        调用方应停止抓取。``pages_crawled`` 在此处递增（既作配额控制，也
        作统计计数），下载成功 / 失败后再分别更新 ``success`` / ``failed``。
        """
        with self._stats_lock:
            if self._stats.pages_crawled >= self._config.max_pages:
                return False
            self._stats.pages_crawled += 1
            return True

    def _record_page_status(self, url: str, response, status: int, is_failure: bool) -> None:
        """下载完成后更新 success/failed/status_codes/bytes/queue/discovered。"""
        qsize = self._url_manager.queue_size
        discovered = self._url_manager.discovered_count
        content_len = len(response.content or b"")
        with self._stats_lock:
            self._stats.queue_size = qsize
            self._stats.urls_discovered = discovered
            if is_failure:
                self._stats.failed += 1
            else:
                self._stats.success += 1
            self._stats.status_codes[status] = (
                self._stats.status_codes.get(status, 0) + 1
            )
            self._stats.bytes_downloaded += content_len

    def _record_failure(self, url: str, status: int) -> None:
        """下载阶段意外异常时记录失败并触发回调。"""
        qsize = self._url_manager.queue_size
        discovered = self._url_manager.discovered_count
        with self._stats_lock:
            self._stats.queue_size = qsize
            self._stats.urls_discovered = discovered
            self._stats.failed += 1
            self._stats.status_codes[status] = (
                self._stats.status_codes.get(status, 0) + 1
            )
        self._fire_page_callbacks(url, status)

    def _snapshot_stats(self) -> CrawlStats:
        """返回当前统计的快照副本（深拷贝 status_codes 字典）。"""
        with self._stats_lock:
            return CrawlStats(
                pages_crawled=self._stats.pages_crawled,
                urls_discovered=self._stats.urls_discovered,
                queue_size=self._stats.queue_size,
                success=self._stats.success,
                failed=self._stats.failed,
                retries=self._stats.retries,
                status_codes=dict(self._stats.status_codes),
                started_at=self._stats.started_at,
                bytes_downloaded=self._stats.bytes_downloaded,
            )

    # ------------------------------------------------------------------ #
    # 回调
    # ------------------------------------------------------------------ #

    def _log(self, level: int, message: str) -> None:
        """同时输出到 logger 与 on_log 回调。"""
        self._logger.log(level, message)
        if self._on_log is not None:
            try:
                self._on_log(level, message)
            except Exception as exc:
                self._logger.error("on_log 回调异常: %s", exc, exc_info=True)

    def _fire_page_callbacks(self, url: str, status: int) -> None:
        """触发 on_page_crawled 与 on_stats_updated 回调。"""
        if self._on_page_crawled is not None:
            try:
                self._on_page_crawled(url, status)
            except Exception as exc:
                self._logger.error("on_page_crawled 回调异常: %s", exc, exc_info=True)
        self._fire_on_stats_updated()

    def _fire_on_stats_updated(self) -> None:
        """触发 on_stats_updated 回调，传入统计快照。"""
        if self._on_stats_updated is not None:
            try:
                self._on_stats_updated(self._snapshot_stats())
            except Exception as exc:
                self._logger.error("on_stats_updated 回调异常: %s", exc, exc_info=True)

    # ------------------------------------------------------------------ #
    # 监控线程
    # ------------------------------------------------------------------ #

    def _monitor(self) -> None:
        """监控线程：等待所有工作线程退出，然后转换到 STOPPED 并清理资源。"""
        futures = list(self._futures)
        if futures:
            _wait_futures(futures)

        # 关闭线程池（此时所有 future 已完成，wait=True 会立即返回）
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=True)
            except Exception as exc:
                self._logger.error("executor.shutdown 异常: %s", exc, exc_info=True)

        # 转换到 STOPPED 状态
        with self._state_lock:
            self._state = _State.STOPPED

        # 清理资源：管道与 URL 管理器
        try:
            self._pipeline.close()
        except Exception as exc:
            self._logger.error("pipeline.close 异常: %s", exc, exc_info=True)
        try:
            self._url_manager.close()
        except Exception as exc:
            self._logger.error("url_manager.close 异常: %s", exc, exc_info=True)

        self._log(logging.INFO, _fmt_log("INFO", "scheduler", "-", "-", 0, "爬虫已停止"))
        self._fire_on_stats_updated()
