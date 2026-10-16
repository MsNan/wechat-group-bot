# -*- coding: utf-8 -*-
"""
兼容层：把 wxui 包装成 bot.py 已经在用的那套接口。

bot.py 的业务逻辑（三层决策链、防封号节流、发送多路降级、回读确认）
只认这套接口，怎么读写微信全部收在 wxui 里。

对齐的接口（bot.py 实际用到的全集）：
    WeChat()            -> NativeWeChat
      .nickname
      .GetSession()     -> [obj with .name]
      .ChatWith(name)
      .ChatInfo()       -> {'chat_name': ..., 'chat_type': ...}
      .GetAllMessage()  -> [CompatMessage]
      .SendMsg(text, at=None)
    CompatMessage: .attr / .type / .sender / .content / .id

刻意【不提供】独立聊天子窗口相关的接口：
bot.py 只走「ChatWith + 主窗口轮询」这一条路。
"""

import time

from .adapter import WeChatUI
from .errors import SendFailed, TargetMismatch


class _Session(object):
    """GetSession() 的返回元素，bot.py 只读 .name。"""
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name


class CompatMessage(object):
    """
    把 wxui 的 Message 翻译成 bot.py 认识的 attr/type 语义。

    attr: self / friend / system      —— 谁发的
    type: text / time / other         —— 内容类型

    注意 is_self 为 None（判不出来）时，attr 给 'unknown'。
    bot.py 的 should_handle 只放行 attr == 'friend'，
    所以判不出来的消息会被跳过——这是【故意的】：
    万一那条其实是机器人自己发的，回复它就会自问自答死循环。
    """
    __slots__ = ("attr", "type", "sender", "content", "id", "_raw")

    _PLACEHOLDERS = {"[图片]", "[视频]", "[语音]", "[文件]", "[动画表情]",
                     "[表情]", "[链接]", "[位置]", "[聊天记录]", "[小程序]",
                     "[音乐]", "[名片]", "[转账]", "[红包]"}

    def __init__(self, m, sender=None):
        self._raw = m
        self.id = m.id
        self.content = m.content or ""
        if m.kind == "system":
            self.attr, self.type, self.sender = "system", "time", "system"
            return
        if m.is_self is True:
            self.attr, self.sender = "self", "self"
        elif m.is_self is False:
            self.attr, self.sender = "friend", (sender or "群成员")
        else:
            self.attr, self.sender = "unknown", (sender or "未知")
        self.type = "other" if self.content.strip() in self._PLACEHOLDERS else "text"


class NativeWeChat(object):
    """bot.py 使用的微信对象，底下是 wxui 的 WeChatUI。"""

    def __init__(self, ui=None):
        self._ui = ui or WeChatUI().connect()
        self._chat = None
        # 发送者昵称按消息 id 缓存。
        # 昵称只能从会话列表摘要里读，而摘要只反映【最新一条】消息——
        # 消息刚到那一刻能读到，晚几秒摘要就被更新的消息顶掉了。
        # bot.py 一轮里会多次调 GetAllMessage（轮询 + 会话校验 + 发送回读），
        # 不缓存的话第一次读到了，后面几次又变回未知。
        self._sender_by_id = {}
        self._sender_tries = {}
        self._SENDER_CACHE_MAX = 400
        # 会话列表的摘要更新【比消息本身慢】。轮询 1.5s 一次，消息刚到就被读走，
        # 那一刻摘要还停在上一条上，昵称自然读不到；等摘要刷新了，
        # 消息早已被业务层消费掉。所以新消息第一次读到时给一次短重试。
        self._SENDER_RETRY_WAIT = 0.4
        self._SENDER_MAX_TRIES = 3

    def supports_at(self):
        """
        这一层【不支持】@ 某人。

        微信的 @ 要从候选列表里挑人，挑错就会 @ 到别人身上——
        实测出现过「本想 @郭涛，实际 @ 到了 tao（机器人自己）」。
        在拿不到群成员列表、无法在发送前确认命中的前提下，
        正确的降级是【根本不 @】，而不是先错发一次再拉黑。
        上层据此不要设置 at 参数，也不要做 @ 落点校验。
        """
        return False

    # --- bot.py 会读的属性 ---
    @property
    def nickname(self):
        # UIA 里没有直接暴露当前登录账号昵称。返回 None 让 bot.py 走它的
        # 「读不出昵称」分支，而不是编一个假的出来。
        return None

    # --- 会话 ---
    def GetSession(self):
        return [_Session(n) for n in self._ui.sessions()]

    def ChatWith(self, who, **kw):
        self._ui.ensure_ready()
        self._ui.open_chat(who)
        self._chat = who
        return who

    def ChatInfo(self):
        name = self._ui.current_chat()
        return {"chat_name": name, "chat_type": "group"}

    # --- 消息 ---
    def GetAllMessage(self, **kw):
        self._ui.ensure_ready()
        msgs = self._ui.read_all_with_ids()
        chat = self._chat or self._ui.current_chat()
        # 发送者只对最新一条补得到（摘要只反映最新消息），
        # 所以只给最后一条非自己的消息补，其余保持默认。
        # 只对「最新一条别人发的」尝试补昵称——摘要只反映最新消息。
        # 补到了就按 id 缓存，之后同一条消息再读到就直接用缓存。
        last_peer = None
        for m in reversed(msgs):
            if m.kind == "text" and m.is_self is False:
                last_peer = m
                break
        if last_peer is not None and last_peer.id not in self._sender_by_id:
            got = self._ui.guess_sender(last_peer, chat)
            if not got:
                # 摘要可能还没刷新，等一下再试一次
                time.sleep(self._SENDER_RETRY_WAIT)
                got = self._ui.guess_sender(last_peer, chat)
            if got:
                self._sender_by_id[last_peer.id] = got
                self._sender_tries.pop(last_peer.id, None)
                if len(self._sender_by_id) > self._SENDER_CACHE_MAX:
                    for k in list(self._sender_by_id)[:100]:
                        self._sender_by_id.pop(k, None)
            else:
                # 连续几轮都读不到就固化成占位符，别每轮都白等 0.4 秒
                n = self._sender_tries.get(last_peer.id, 0) + 1
                self._sender_tries[last_peer.id] = n
                if n >= self._SENDER_MAX_TRIES:
                    self._sender_by_id[last_peer.id] = None

        return [CompatMessage(m, self._sender_by_id.get(m.id)) for m in msgs]

    # --- 发送 ---
    def SendMsg(self, msg, who=None, at=None, **kw):
        """
        发送。at 参数【被忽略】。

        原因：微信的 @ 需要从候选列表里选人，选错了就会 @ 到别人身上——
        实测出现过「本想 @郭涛，实际 @ 到了 tao（机器人自己）」。
        在拿不到群成员列表、无法在发送前确认命中的前提下，
        正确的降级是【不 @】而不是「先错发一次再拉黑」。
        称呼放在正文里由上层拼，效果一样且不会出错。
        """
        target = who or self._chat or self._ui.current_chat()
        out = self._ui.send_text(msg, expect_chat=target)
        if not out.called:
            raise SendFailed(out.describe())
        return {"status": "成功" if out.verified else "已发送但未确认",
                "is_success": True,
                "message": out.describe()}


def WeChat(*a, **kw):
    """bot.py 通过它拿到微信对象。"""
    return NativeWeChat()
