"""HTTP 状态码合规处理器。

根据 spec 的「状态码合规」要求，把每个响应的状态码映射为一个
:class:`StatusDecision`，告诉调度器下一步动作（继续 / 跳过 / 重试 /
降速 / 暂停询问用户）。

设计要点：
    - 纯逻辑模块，无 PySide6 依赖，可被调度器或测试直接复用。
    - 维护按 URL 的连续 429 计数器，实现指数退避降速，并在连续 3 次
      429 后转交用户决策。
    - 非 429 状态码出现时自动清零对应 URL 的计数器。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    """状态码处理动作。"""

    CONTINUE = "continue"
    SKIP = "skip"
    RETRY = "retry"
    SLOW_DOWN = "slow_down"
    PAUSE_ASK_USER = "pause_ask_user"


@dataclass
class StatusDecision:
    """单次状态码判断的结果。

    Attributes:
        action: 调度器应执行的动作。
        message: 给用户 / 日志的可读说明。
        new_interval: 仅当动作为 ``SLOW_DOWN`` 时携带调整后的请求间隔；
            其它动作下为 ``None``。
    """

    action: Action
    message: str = ""
    new_interval: float | None = None


class StatusHandler:
    """根据 HTTP 状态码产生 :class:`StatusDecision`。

    Args:
        base_interval: 基础请求间隔（秒），用于 429 指数退避计算。
        max_interval: 降速间隔上限（秒）。
        logger: 可选日志器；默认使用 ``logging.getLogger("crawler.status_handler")``。
    """

    def __init__(
        self,
        base_interval: float = 0.5,
        max_interval: float = 60.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._base_interval = base_interval
        self._max_interval = max_interval
        self._logger = logger or logging.getLogger("crawler.status_handler")
        # URL -> 连续 429 次数
        self._rate_limit_counts: dict[str, int] = {}

    def handle(self, status_code: int, url: str) -> StatusDecision:
        """根据 ``status_code`` 返回对应的 :class:`StatusDecision`。"""

        # 429：累加计数并指数退避
        if status_code == 429:
            count = self._rate_limit_counts.get(url, 0) + 1
            self._rate_limit_counts[url] = count
            new_interval = min(
                self._base_interval * (2 ** count), self._max_interval
            )
            if count >= 3:
                self._logger.warning(
                    "连续 %d 次收到 429 (%s)，需用户决策", count, url
                )
                return StatusDecision(
                    action=Action.PAUSE_ASK_USER,
                    message=f"连续 {count} 次收到 429，需用户决策",
                )
            self._logger.info(
                "429 降速至 %ss 后重试 (%s)", new_interval, url
            )
            return StatusDecision(
                action=Action.SLOW_DOWN,
                message=f"429 降速至 {new_interval}s 后重试",
                new_interval=new_interval,
            )

        # 非 429：清零该 URL 的计数
        self._rate_limit_counts.pop(url, None)

        if status_code == 200:
            return StatusDecision(action=Action.CONTINUE)
        if status_code in (301, 302, 303, 307, 308):
            return StatusDecision(
                action=Action.CONTINUE,
                message="重定向已由下载器处理",
            )
        if status_code == 403:
            return StatusDecision(
                action=Action.SKIP,
                message="403 Forbidden，跳过该 URL",
            )
        if status_code == 404:
            return StatusDecision(
                action=Action.SKIP,
                message="404 Not Found，跳过该 URL",
            )
        if 500 <= status_code <= 599:
            return StatusDecision(
                action=Action.RETRY,
                message=f"{status_code} 服务端错误，重试",
            )
        return StatusDecision(
            action=Action.PAUSE_ASK_USER,
            message=f"未知状态码 {status_code}，需用户决策",
        )

    def reset(self) -> None:
        """清空所有 429 计数器。"""

        self._rate_limit_counts.clear()
