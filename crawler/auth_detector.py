"""登录态 / 认证需求探测器。

根据 spec 的「登录认证探测」要求，判断响应是否需要登录、用户是否已提供
认证信息，以及是否应弹出认证提示。

设计要点：
    - 纯逻辑模块，无 PySide6 依赖。
    - 仅依赖 :mod:`crawler.models` 中的数据类做类型提示，models.py 本身
      UI-free，可直接 import。
    - ``needs_auth`` 综合状态码 (401/407) 与登录页关键词命中判断。
    - ``has_user_auth`` 检查 cookies 与 Authorization / Cookie 请求头。
    - ``should_prompt_auth`` = 需要认证 且 用户未提供认证。
"""

from __future__ import annotations

from crawler.models import CrawlConfig, Response

# 登录页关键词（已小写；中文不受 lower() 影响）
_LOGIN_KEYWORDS = (
    "登录",
    "login",
    "sign in",
    "请登录后查看",
    "请先登录",
)

# 视为携带认证信息的请求头名（小写）
_AUTH_HEADERS = ("authorization", "cookie")


def needs_auth(response: Response, config: CrawlConfig) -> bool:
    """判断响应是否表明需要登录 / 认证。

    Args:
        response: :class:`crawler.models.Response`。
        config: :class:`crawler.models.CrawlConfig`（保留参数以备扩展）。

    Returns:
        状态码为 401/407，或响应文本命中登录页关键词时返回 ``True``。
    """

    status = getattr(response, "status", 0) or 0
    if status in (401, 407):
        return True
    text = (getattr(response, "text", "") or "").lower()
    return any(keyword in text for keyword in _LOGIN_KEYWORDS)


def has_user_auth(config: CrawlConfig) -> bool:
    """判断用户配置是否已携带认证信息。

    Returns:
        ``config.cookies`` 非空，或 ``config.headers`` 中包含
        ``Authorization`` / ``Cookie``（大小写不敏感）头时返回 ``True``。
    """

    if getattr(config, "cookies", None):
        return True
    headers = getattr(config, "headers", None) or {}
    for key in headers:
        if key.lower() in _AUTH_HEADERS:
            return True
    return False


def should_prompt_auth(response: Response, config: CrawlConfig) -> bool:
    """判断是否应向用户提示需要认证。

    Returns:
        ``needs_auth(response, config) and not has_user_auth(config)``。
    """

    return needs_auth(response, config) and not has_user_auth(config)
