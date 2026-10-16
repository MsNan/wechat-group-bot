# -*- coding: utf-8 -*-
"""
自研微信适配层的异常类型。

分成三档，调用方据此决定「重试 / 退避 / 放弃」，
而不是像以前那样把所有失败都当成同一种、然后每 1.5 秒疯狂重试。
"""


class WxError(Exception):
    """本层所有异常的基类。"""


# ---------- 第一档：环境级，重试无意义，必须等外部条件恢复 ----------

class WeChatNotRunning(WxError):
    """微信进程/主窗口不存在。重试再多次也没用，应当退避并明确告警。"""


class WeChatNotLoggedIn(WxError):
    """微信在跑但没登录（停在扫码页）。同样需要人工介入。"""


class AccessibilityUnavailable(WxError):
    """
    窗口在，但没有无障碍接口（WM_GETOBJECT 返回 0）。
    实测微信 4.1.13.12 就是这种情况——换库也读不到，只能换微信版本。
    """


# ---------- 第二档：会话级，可以重试或切换 ----------

class ChatNotFound(WxError):
    """会话列表里找不到指定会话（可能没在列表中，需要搜索）。"""


class ChatSwitchFailed(WxError):
    """切换会话失败，或切换后复核发现当前会话不是目标。"""


# ---------- 第三档：操作级，通常可重试 ----------

class ReadFailed(WxError):
    """读消息失败（控件树临时不可用等）。"""


class SendFailed(WxError):
    """发送失败。message 里应说明是哪一步失败：定位输入框 / 输入 / 回读确认。"""


class TargetMismatch(SendFailed):
    """
    发送前复核发现当前会话不是目标会话——直接放弃发送。
    这是「宁可不发，也绝不发错群」的兜底。
    """
