# -*- coding: utf-8 -*-
"""
wxui —— 自研微信适配层。

只用 uiautomation（Apache-2.0）+ ctypes。

    机器人逻辑  →  wxui  →  uiautomation  →  微信窗口

支持的微信版本：4.1.8.107（实测）。
4.1.13 及以上没有实现无障碍接口，任何界面自动化方案都读不到内容，
连接时会抛 AccessibilityUnavailable 明确告知，而不是给一堆空数据。
"""

from .adapter import WeChatUI, SendOutcome
from .compat import NativeWeChat, WeChat, CompatMessage
from .messages import Message
from .errors import (WxError, WeChatNotRunning, WeChatNotLoggedIn,
                     AccessibilityUnavailable, ChatNotFound, ChatSwitchFailed,
                     ReadFailed, SendFailed, TargetMismatch)

__all__ = [
    "WeChatUI", "SendOutcome", "NativeWeChat", "WeChat", "CompatMessage", "Message",
    "WxError", "WeChatNotRunning", "WeChatNotLoggedIn",
    "AccessibilityUnavailable", "ChatNotFound", "ChatSwitchFailed",
    "ReadFailed", "SendFailed", "TargetMismatch",
]
