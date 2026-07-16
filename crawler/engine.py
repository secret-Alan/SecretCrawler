"""爬虫引擎 —— 核心组件装配层与 Qt 信号桥接层（Task 8）。

本模块是 UI 层与 ``crawler`` 核心（UI-free）模块之间的桥梁：

- 8.1 装配：在 ``__init__`` 中实例化下载器 / URL 管理器 / robots 检查器 /
  内容解析器 / 数据管道 / 调度器，把引擎自己的回调注入调度器。
- 8.2 回调桥接：调度器回调（``on_page_crawled`` / ``on_result_found`` /
  ``on_stats_updated`` / ``on_log``）均在工作线程中被调用。Qt 的自动连接
  （``AutoConnection``）会跨线程走 ``QueuedConnection``，因此在工作线程中
  ``emit`` 信号是安全的——信号会被投递到接收方所在的线程（UI 主线程）
  的事件循环中执行。所以这里只需在回调里直接 emit 信号即可。
- 8.3 状态转发：``start`` / ``pause`` / ``resume`` / ``stop`` 转发到调度器，
  并 emit ``state_changed``；用一个 300ms 的 ``QTimer``（在主线程上运行）
  轮询 ``scheduler.is_running``，当其转为 ``False`` 时 emit ``finished`` 与
  ``state_changed("STOPPED")``。

重要限制：``QTimer`` 依赖 Qt 事件循环。若引擎在没有事件循环的环境下使用
（例如某些单元测试），轮询定时器不会触发，``finished`` 信号也不会发出；
调度器本身仍会正常执行并完成，只是引擎层无法感知到「完成」事件。UI 层
始终运行 ``QApplication`` 事件循环，因此实际使用中不受影响。
"""

from __future__ import annotations

import copy
import logging
import threading
import traceback

from PySide6.QtCore import QObject, QTimer, Signal

from crawler.compliance import ComplianceAssessor
from crawler.downloader import HTTPDownloader
from crawler.models import CrawlConfig, CrawlStats, ResultItem
from crawler.parser import ContentParser
from crawler.pipeline import PipelineFactory
from crawler.robots import RobotsChecker
from crawler.scheduler import Scheduler
from crawler.status_handler import StatusHandler
from crawler.url_manager import URLManager

# 轮询调度器是否完成的定时器间隔（毫秒）
_POLL_INTERVAL_MS = 300


class CrawlEngine(QObject):
    """爬虫引擎：装配核心组件并把调度器回调桥接为 Qt 信号。

    所有信号均在主线程被消费（UI 槽函数连接到这些信号）。调度器回调虽然
    在工作线程触发，但 Qt 自动连接会以排队方式跨线程投递，因此这里是
    线程安全的。

    Signals:
        page_crawled(str, int): 单页抓取完成 ``(url, status)``。
        result_found(object): 解析出一条结果 ``ResultItem``。
        stats_updated(object): 统计快照 ``CrawlStats``（独立副本）。
        log_message(int, str): 日志 ``(level, message)``，``level`` 取自
            ``logging`` 常量（如 ``logging.INFO``）。
        state_changed(str): 状态变化，取值为
            ``"IDLE" / "RUNNING" / "PAUSED" / "STOPPING" / "STOPPED"``。
        finished(): 爬取完全停止时发出一次。
        error(str): 致命错误消息。
    """

    # ---- Qt 信号 ----
    # 调度器回调工作线程触发；Qt 自动连接跨线程走 QueuedConnection，
    # 因此信号会在接收方（主线程）的事件循环中派发，安全。
    page_crawled = Signal(str, int)        # url, status
    result_found = Signal(object)          # ResultItem
    stats_updated = Signal(object)         # CrawlStats（快照副本）
    log_message = Signal(int, str)         # logging level int, message
    state_changed = Signal(str)            # "IDLE"/"RUNNING"/"PAUSED"/"STOPPING"/"STOPPED"
    finished = Signal()                    # 爬取完全停止时发出一次
    error = Signal(str)                    # 致命错误消息
    # Task 7 / 9 / 11 / 15：合规与探测信号（payload 均为 dict）
    status_warning = Signal(dict)          # {"url","status","message"}
    api_detected = Signal(dict)            # {"base_url","evidence","suggested_endpoints"}
    auth_required = Signal(dict)           # {"url","reason"}
    compliance_report = Signal(dict)       # 合规评估结果
    tos_warning = Signal(dict)             # {"evidence"}

    def __init__(self, config: CrawlConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config

        # 8.1 装配核心组件
        self._logger = logging.getLogger("crawler.engine")

        # 可选：将日志写入文件（追加模式）。消息本身已含统一格式前缀，
        # 故 formatter 仅输出 %(message)s。
        if config.output_path:
            try:
                import os
                _out = config.output_path
                if os.path.isdir(_out):
                    _log_file = os.path.join(_out, "crawler.log")
                else:
                    _parent = os.path.dirname(os.path.abspath(_out))
                    os.makedirs(_parent, exist_ok=True)
                    _log_file = os.path.join(_parent, "crawler.log")
                _fh = logging.FileHandler(_log_file, mode="a", encoding="utf-8")
                _fh.setFormatter(logging.Formatter("%(message)s"))
                self._logger.addHandler(_fh)
            except Exception as _exc:
                self._logger.warning("无法创建日志文件: %s", _exc)

        self._downloader = HTTPDownloader(config, logger=self._logger)
        self._url_manager = URLManager(config, logger=self._logger)
        self._robots_checker = RobotsChecker(
            config, logger=self._logger, fetcher=self._downloader.fetch
        )
        self._parser = ContentParser(config, logger=self._logger)
        self._pipeline = PipelineFactory.build(config, logger=self._logger)

        # Task 7：状态码合规处理器（base_interval 与配置请求间隔一致）
        self._status_handler = StatusHandler(base_interval=config.request_interval)
        # Task 15：合规评估器（供预评估与下载脱敏复用）
        self._compliance_assessor = ComplianceAssessor()

        # Task 15.3：把合规评估器注入文件下载管道，用于下载前脱敏
        self._install_compliance_assessor(self._pipeline, self._compliance_assessor)

        self._scheduler = Scheduler(
            config,
            self._downloader,
            self._url_manager,
            self._robots_checker,
            self._parser,
            self._pipeline,
            logger=self._logger,
            on_page_crawled=self._on_page_crawled,
            on_result_found=self._on_result_found,
            on_stats_updated=self._on_stats_updated,
            on_log=self._on_log,
            status_handler=self._status_handler,
            on_status_warning=self._on_status_warning,
            on_auth_required=self._on_auth_required,
        )

        # 引擎级状态字符串（避免直接读取调度器私有属性）
        self._state: str = "IDLE"

        # 完成轮询定时器：在 start() 中惰性创建并启动
        self._poll_timer: QTimer | None = None

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """启动爬取。

        非阻塞：转发到 ``scheduler.start()``（内部提交工作线程后立即返回），
        emit ``state_changed("RUNNING")``，并启动 300ms 轮询定时器以在
        完成时 emit ``finished``。
        """
        try:
            # 仅在 IDLE 状态下可启动，避免重复启动导致定时器重复
            if self._state != "IDLE":
                self._logger.warning(
                    "引擎不在 IDLE 状态，忽略 start (当前: %s)", self._state
                )
                return
            self._scheduler.start()
            # 仅在调度器确实进入运行态后才推进引擎状态
            if self._scheduler.is_running:
                self._set_state("RUNNING")
                self._start_poll_timer()
                # Task 9 / 15：启动预探测后台线程（守护线程，非阻塞，advisory）
                self._launch_pre_crawl_checks()
        except Exception as exc:
            tb = traceback.format_exc()
            self._logger.error("start 异常: %s\n%s", exc, tb)
            self.error.emit(str(exc))

    def pause(self) -> None:
        """暂停爬取：转发到调度器并 emit 状态。"""
        try:
            self._scheduler.pause()
            # 仅在调度器确实转为 PAUSED 时才同步引擎状态
            if self._state == "RUNNING":
                self._set_state("PAUSED")
        except Exception as exc:
            tb = traceback.format_exc()
            self._logger.error("pause 异常: %s\n%s", exc, tb)
            self.error.emit(str(exc))

    def resume(self) -> None:
        """从暂停恢复：转发到调度器并 emit 状态。"""
        try:
            self._scheduler.resume()
            if self._state == "PAUSED":
                self._set_state("RUNNING")
        except Exception as exc:
            tb = traceback.format_exc()
            self._logger.error("resume 异常: %s\n%s", exc, tb)
            self.error.emit(str(exc))

    def stop(self) -> None:
        """请求优雅停止：转发到调度器，emit ``STOPPING``。

        实际停止后由轮询定时器 emit ``STOPPED`` + ``finished``。
        """
        try:
            self._scheduler.stop()
            if self._state in ("RUNNING", "PAUSED"):
                self._set_state("STOPPING")
        except Exception as exc:
            tb = traceback.format_exc()
            self._logger.error("stop 异常: %s\n%s", exc, tb)
            self.error.emit(str(exc))

    @property
    def stats(self) -> CrawlStats:
        """返回当前统计快照（调度器已返回线程安全的副本）。"""
        return self._scheduler.stats

    @property
    def is_running(self) -> bool:
        """引擎是否处于活动状态（RUNNING / PAUSED / STOPPING）。"""
        return self._state in ("RUNNING", "PAUSED", "STOPPING")

    @property
    def state(self) -> str:
        """引擎当前状态字符串。"""
        return self._state

    # ------------------------------------------------------------------ #
    # 调度器回调（在工作线程中被调用）—— 直接 emit 信号即可
    # ------------------------------------------------------------------ #

    def _on_page_crawled(self, url: str, status: int) -> None:
        """单页抓取完成回调：emit ``page_crawled``。"""
        self.page_crawled.emit(url, status)

    def _on_result_found(self, item: ResultItem) -> None:
        """解析出结果回调：emit ``result_found``。"""
        self.result_found.emit(item)

    def _on_stats_updated(self, stats: CrawlStats) -> None:
        """统计更新回调：emit 一份独立副本，避免跨线程读写竞争。

        调度器传入的已经是快照副本，但这里再 ``copy.copy`` 一次，确保即便
        后续有代码就地修改该对象也不会影响已 emit 出去的引用。
        """
        try:
            snapshot = copy.copy(stats)
            # status_codes 是 dict，浅拷贝后仍与原对象共享该字典；再复制一份
            snapshot.status_codes = dict(stats.status_codes)
        except Exception:
            snapshot = stats
        self.stats_updated.emit(snapshot)

    def _on_log(self, level: int, message: str) -> None:
        """日志回调：emit ``log_message`` 并转发到 Python ``logging``。"""
        self.log_message.emit(level, message)
        try:
            self._logger.log(level, message)
        except Exception:
            pass

    def _on_status_warning(self, url: str, status: int, message: str) -> None:
        """Task 7：状态码警告回调（工作线程触发）：emit ``status_warning``。"""
        self.status_warning.emit({"url": url, "status": status, "message": message})

    def _on_auth_required(self, url: str, reason: str) -> None:
        """Task 11：认证需求回调（工作线程触发）：emit ``auth_required``。"""
        self.auth_required.emit({"url": url, "reason": reason})

    # ------------------------------------------------------------------ #
    # 完成轮询定时器
    # ------------------------------------------------------------------ #

    def _start_poll_timer(self) -> None:
        """惰性创建并启动 300ms 轮询定时器。

        ``start()`` 从 UI 线程调用，因此 ``QTimer(self)`` 创建在主线程上，
        其 ``timeout`` 信号也在主线程派发——满足 Qt 跨线程定时器约束。
        """
        if self._poll_timer is None:
            self._poll_timer = QTimer(self)
            self._poll_timer.setInterval(_POLL_INTERVAL_MS)
            self._poll_timer.timeout.connect(self._on_poll)
        self._poll_timer.start()

    def _on_poll(self) -> None:
        """轮询槽：调度器一旦停止，emit ``STOPPED`` + ``finished`` 并停表。"""
        if self._scheduler.is_running:
            return
        if self._state == "STOPPED":
            # 已通知过，避免重复 emit
            if self._poll_timer is not None:
                self._poll_timer.stop()
            return
        self._set_state("STOPPED")
        # 发出完成信号（仅一次，因为上面已挡掉 STOPPED 重复进入）
        self.finished.emit()
        if self._poll_timer is not None:
            self._poll_timer.stop()

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _set_state(self, new_state: str) -> None:
        """更新引擎状态并 emit ``state_changed``。"""
        if self._state == new_state:
            return
        self._state = new_state
        self.state_changed.emit(new_state)

    # ------------------------------------------------------------------ #
    # Task 9 / 15：预探测后台线程（非阻塞、advisory）
    # ------------------------------------------------------------------ #

    def _launch_pre_crawl_checks(self) -> None:
        """启动官方 API 探测与合规评估的守护线程。

        两个线程均为 daemon、非阻塞，任何异常都被吞掉，绝不影响 ``start()``
        返回或主爬取流程。结果通过 ``api_detected`` / ``compliance_report`` /
        ``tos_warning`` 信号上抛，UI 自行决定如何响应。
        """
        # Task 9：官方 API 探测
        def _api_check() -> None:
            try:
                from crawler.api_detector import detect_official_api

                info = detect_official_api(
                    self._config.start_urls, fetcher=self._downloader.fetch
                )
                if info is not None:
                    self.api_detected.emit(
                        {
                            "base_url": info.base_url,
                            "evidence": info.evidence,
                            "suggested_endpoints": info.suggested_endpoints,
                        }
                    )
            except Exception:
                pass

        threading.Thread(target=_api_check, name="api-detector", daemon=True).start()

        # Task 15：合规评估（仅在启用时运行）
        def _compliance_check() -> None:
            try:
                report = self._compliance_assessor.assess(
                    self._config.start_urls, fetcher=self._downloader.fetch
                )
                self.compliance_report.emit(
                    {
                        "assessed": report.assessed,
                        "sensitive_hits": report.sensitive_hits,
                        "sensitive_categories": report.sensitive_categories,
                        "tos_prohibits": report.tos_prohibits,
                        "tos_evidence": report.tos_evidence,
                    }
                )
                if report.tos_prohibits:
                    self.tos_warning.emit({"evidence": report.tos_evidence})
            except Exception:
                pass

        if self._config.compliance_check_enabled:
            threading.Thread(
                target=_compliance_check, name="compliance-check", daemon=True
            ).start()

    # ------------------------------------------------------------------ #
    # Task 15.3：把合规评估器注入文件下载管道
    # ------------------------------------------------------------------ #

    @staticmethod
    def _install_compliance_assessor(pipeline, assessor) -> None:
        """把 ``ComplianceAssessor`` 注入 pipeline 中的 FileDownloadPipeline。

        ``PipelineFactory.build`` 可能返回 ``FileDownloadPipeline``、
        ``MultiPipeline``（内含多个子管道）或其它单管道。这里用 getattr/hasattr
        健壮地定位 ``FileDownloadPipeline`` 并设置其 ``compliance_assessor`` 属性。
        """
        try:
            # 直接是 FileDownloadPipeline（具备 compliance_assessor 属性）
            if hasattr(pipeline, "compliance_assessor"):
                pipeline.compliance_assessor = assessor
                return
            # MultiPipeline：遍历子管道
            sub_pipelines = getattr(pipeline, "pipelines", None)
            if sub_pipelines:
                for sub in sub_pipelines:
                    if hasattr(sub, "compliance_assessor"):
                        sub.compliance_assessor = assessor
        except Exception:
            pass
