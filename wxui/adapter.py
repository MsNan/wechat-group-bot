# -*- coding: utf-8 -*-
"""
自研微信适配层的对外接口。

  机器人逻辑  →  【这一层】  →  uiautomation  →  微信窗口

对上只暴露业务概念（会话、消息、发送），把控件类名、坐标、颜色这些
微信版本相关的细节全部关在这一层里。微信改版时只改这一层。
"""

import re
import time

import uiautomation as uia

from . import window as W
from . import messages as M
from .errors import (WxError, WeChatNotRunning, AccessibilityUnavailable,
                     ChatNotFound, ChatSwitchFailed, ReadFailed,
                     SendFailed, TargetMismatch)

SESSION_LIST_CLASS = "mmui::ChatSessionList"
SESSION_TABLE_CLASS = "mmui::XTableView"
SESSION_CELL_CLASS = "mmui::ChatSessionCell"
INPUT_CLASS = "mmui::ChatInputField"

# 会话摘要里要跳过的行：未读角标「[3条]」、时间「16:36」「昨天 12:45」「星期五」
_UNREAD_RE = re.compile(r"^\[\d+条\]$")
_TIME_RE = re.compile(r"^(\d{1,2}:\d{2}|昨天\s*\d{1,2}:\d{2}|星期.|\d{4}/\d{1,2}/\d{1,2}|\d{1,2}月\d{1,2}日)")


class SendOutcome(object):
    """
    发送结果。刻意把「调用了」「界面确认了」分开——
    不能把「没抛异常」当成功，否则发失败了还会报成功。
    """

    def __init__(self, called=False, verified=False, method="", detail=""):
        self.called = called
        self.verified = verified
        self.method = method
        self.detail = detail

    @property
    def ok(self):
        return self.called and self.verified

    def describe(self):
        if not self.called:
            return "未发送（%s）" % (self.detail or "原因未知")
        return "已发送(%s)；界面确认=%s%s" % (
            self.method, "是" if self.verified else "否",
            ("；" + self.detail) if self.detail else "")


class WeChatUI(object):
    """
    一个实例对应一个微信主窗口。

    用法：
        ui = WeChatUI(); ui.connect()
        ui.open_chat("测试群")
        for m in ui.poll(): ...
        ui.send_text("你好", expect_chat="测试群")
    """

    def __init__(self, switch_wait=1.2, switch_retries=3):
        self.win = None
        self.switch_wait = switch_wait
        self.switch_retries = switch_retries
        self._target = None          # 当前盯着的会话名
        self._last_seq = None        # 上一轮的消息序列，用于对齐
        self._baseline_size = 0      # 建基线时屏上有多少条（用于如实上报吞了多少）
        self._id_seq = None          # [(key, id, msg)]，用于给消息分配稳定 id
        self._id_counter = 0

    # ---------------- 连接与健康 ----------------

    def connect(self):
        """定位微信主窗口。失败会抛出分档异常，调用方据此决定退避策略。"""
        self.win = W.find_main_window(check_accessibility=True)
        return self

    def ensure_ready(self):
        """
        每轮操作前调用。窗口失效（微信重启过）就重新查找。

        不这么做的话，会抱着失效的引用一直重试，
        微信重启后永远不可能自愈。
        """
        if not W.is_alive(self.win):
            self.win = None
            self.connect()          # 找不到会抛 WeChatNotRunning
            # 窗口重建后，之前的会话状态和消息序列都不作数了
            self._last_seq = None
            return True             # 告诉调用方「窗口重建过」
        return False

    # ---------------- 会话 ----------------

    def _session_cells(self):
        box = W.find_descendant(self.win, SESSION_LIST_CLASS)
        if box is None:
            return []
        table = W.find_descendant(box, SESSION_TABLE_CLASS) or box
        try:
            return [c for c in table.GetChildren()
                    if str(c.ClassName or "") == SESSION_CELL_CLASS]
        except Exception:
            return []

    def sessions(self):
        """
        返回可见会话名列表。

        ChatSessionCell 的 Name 形如「群名\n最后一条摘要\n时间\n」，
        取第一行就是会话名。
        """
        out = []
        for c in self._session_cells():
            try:
                name = str(c.Name or "").split("\n", 1)[0].strip()
            except Exception:
                continue
            if name:
                out.append(name)
        return out

    def current_chat(self):
        """
        当前打开的是哪个会话。

        用输入框的 Name —— 实测 mmui::ChatInputField 的 Name 就是会话名，
        比去解析标题栏可靠。没打开任何会话时返回 None。
        """
        box = W.find_descendant(self.win, INPUT_CLASS)
        if box is None:
            return None
        try:
            name = str(box.Name or "").strip()
        except Exception:
            return None
        return name or None

    def open_chat(self, name):
        """
        切到指定会话，并【复核确认】切换成功。

        不能「调了切换、没抛异常」就当成功：
        实际主窗口可能还停在原地，于是读到 0 条消息、白等一整轮。
        这里每次切换后都用 current_chat() 复核，不一致就重试。
        """
        last = ""
        for attempt in range(1, self.switch_retries + 1):
            cur = self.current_chat()
            if cur == name:
                self._target = name
                return True

            cell = None
            for c in self._session_cells():
                try:
                    if str(c.Name or "").split("\n", 1)[0].strip() == name:
                        cell = c
                        break
                except Exception:
                    continue
            if cell is None:
                raise ChatNotFound(
                    "会话列表里没有「%s」。请确认名称一字不差，"
                    "或该会话已被滚动出可见范围。" % name)
            try:
                cell.Click(simulateMove=False)
            except Exception as e:
                last = "点击会话失败: %s" % e
                time.sleep(self.switch_wait)
                continue

            time.sleep(self.switch_wait)
            cur = self.current_chat()
            if cur == name:
                self._target = name
                self._last_seq = None      # 换会话了，序列重来
                return True
            last = "切换后当前会话是 %r，不是 %r" % (cur, name)

        raise ChatSwitchFailed("切到「%s」失败（试了 %d 次）：%s"
                               % (name, self.switch_retries, last))

    def session_preview(self, name):
        """
        取某个会话在列表里的摘要行。返回 (发送者, 内容摘要) 或 (None, 摘要)。

        ChatSessionCell 的 Name 是多行文本，但【行数不固定】：

            无未读: '产品技术交流群\nRuiyaW: 好\n昨天 12:45\n'
            有未读: '测试群\n[2条] \n🌼: 不在\n16:36\n'
                            ↑ 未读角标会多插一行，把摘要挤到后面

        所以必须按内容找摘要行，不能按下标取 —— 早先写死 parts[1]，
        有未读时就取到了「[2条]」，而"有未读"恰恰是消息刚到时的常态。

        这是不点头像就能拿到发送者的途径。UIA 树里消息节点是叶子、
        头像不是元素，昵称在消息列表那边根本没暴露。
        """
        for c in self._session_cells():
            try:
                raw = str(c.Name or "")
            except Exception:
                continue
            parts = [p.strip() for p in raw.split("\n")]
            if not parts or parts[0] != name:
                continue
            # 跳过会话名(0)、未读角标(形如「[2条]」)、时间行，找带「昵称: 」的那行
            summary = ""
            for seg in parts[1:]:
                if not seg:
                    continue
                if _UNREAD_RE.match(seg):        # 未读角标
                    continue
                if _TIME_RE.match(seg):          # 时间行
                    continue
                summary = seg
                break
            if ": " in summary:
                who, _, body = summary.partition(": ")
                # 昵称不会太长；太长多半是正文里本来就带冒号
                if who and len(who) <= 24:
                    return who, body
            return None, summary
        return None, ""

    def guess_sender(self, message, chat_name=None):
        """
        尽力给一条消息补上发送者昵称。补不到返回 None，【不猜】。

        原理：会话列表摘要里带着最新一条消息的发送者，
        用内容比对确认摘要指的就是这条消息。

        已知局限（必须写进文档，不能假装没有）：
          · 只对【最新一条】有效。两条消息落在同一轮 poll 里时，
            只有最后那条能补出发送者。
          · 摘要会被截断，所以用前缀比对而不是全等。
          · 自己发的消息摘要没有「昵称: 」前缀，本来也不需要。
        """
        if message is None or message.is_self:
            return None
        who, body = self.session_preview(chat_name or self._target or "")
        if not who:
            return None
        content = (message.content or "").strip()
        body = (body or "").strip()
        if not content or not body:
            return None
        # 完全相等直接认（短消息如「在吗」「不在」只有两个字，
        # 之前要求前缀至少 4 字符，把它们全挡掉了）
        if content == body:
            return who
        # 摘要会被截断，所以用前缀比对；但前缀太短容易撞车，至少要 4 个字符
        n = min(len(body), len(content), 12)
        if n >= 4 and content[:n] == body[:n]:
            return who
        return None

    # ---------------- 读消息 ----------------

    def _msg_list(self):
        return W.find_descendant(self.win, M.MSG_LIST_CLASS)

    def read_all(self):
        """读当前会话屏上可见的全部消息（含历史）。"""
        return M.read_visible(self._msg_list())

    def read_all_with_ids(self):
        """
        读当前会话可见消息，并给每条分配【稳定 id】。

        id 由序列对齐产生：本轮与上轮对齐后，老消息沿用原 id，
        新追加的才发新 id。因此
          · 同一条消息在多轮里 id 不变 —— 上层按 id 去重才是对的
          · 「同一个人连发两条一样的话」会拿到两个不同 id —— 不会被误当重复
        这是把「去重」这件事收在适配层做的原因：内容指纹在上层做必然出错。
        """
        cur = M.read_visible(self._msg_list())
        prev = self._id_seq            # [(key, id, msg)]
        if prev is None:
            assigned = []
            for m in cur:
                self._id_counter += 1
                m.id = "m%06d" % self._id_counter
                assigned.append((m.key(), m.id, m))
            self._id_seq = assigned
            return cur

        prev_keys = [k for k, _, _ in prev]
        cur_keys = [m.key() for m in cur]
        overlap = 0
        for k in range(min(len(prev_keys), len(cur_keys)), 0, -1):
            if prev_keys[-k:] == cur_keys[:k]:
                overlap = k
                break

        assigned = []
        for i, m in enumerate(cur):
            if i < overlap:
                old_key, old_id, old_msg = prev[len(prev) - overlap + i]
                m.id = old_id                               # 沿用老 id
                # is_self 粘滞：这条消息之前判出过，本轮因为滚到边缘判不出，
                # 就沿用之前的结论，别退回 None。否则上层会把它当成
                # 「来源不明、不可回复」，而它其实早就判定过了。
                if m.is_self is None and old_msg is not None:
                    m.is_self = old_msg.is_self
            else:
                self._id_counter += 1
                m.id = "m%06d" % self._id_counter
            assigned.append((m.key(), m.id, m))
        self._id_seq = assigned
        return cur

    def poll(self):
        """
        返回自上次 poll 以来的【新消息】。

        首轮只建基线、返回空列表——重启不会把历史重播一遍。
        基线吞掉了多少条记在 self._baseline_size，调用方应当如实打进日志，
        而不是只说一句「已建立基线」。

        返回 (新消息列表, 事件)，事件取值：
            "primed"    首轮建基线
            "ok"        正常增量
            "resync"    序列对不上（切了会话/列表被刷新），已重建基线
        """
        cur = self.read_all()

        if self._last_seq is None:
            self._last_seq = cur
            self._baseline_size = len(cur)
            return [], "primed"

        fresh = M.diff_new(self._last_seq, cur)
        if fresh is None:
            self._last_seq = cur
            self._baseline_size = len(cur)
            return [], "resync"

        self._last_seq = cur
        return fresh, "ok"

    # ---------------- 发消息 ----------------

    def send_text(self, text, expect_chat, verify_timeout=6.0):
        """
        往当前会话发一条文本。

        expect_chat 必填：发送前会复核当前会话是否就是它，
        不一致直接抛 TargetMismatch 放弃发送——宁可不发，绝不发错群。

        中文用剪贴板粘贴而不是逐字符输入：SendKeys 打中文不可靠。
        注意这会覆盖系统剪贴板。
        """
        if not text or not text.strip():
            return SendOutcome(False, False, "", "内容为空")

        cur = self.current_chat()
        if cur != expect_chat:
            raise TargetMismatch(
                "发送前复核不通过：当前会话是 %r，期望 %r。已放弃本次发送。"
                % (cur, expect_chat))

        box = W.find_descendant(self.win, INPUT_CLASS)
        if box is None:
            raise SendFailed("找不到输入框（%s）" % INPUT_CLASS)

        try:
            box.Click(simulateMove=False)
            time.sleep(0.2)
        except Exception as e:
            raise SendFailed("点击输入框失败: %s" % e)

        method = ""
        try:
            uia.SetClipboardText(text)
            box.SendKeys("{Ctrl}a", waitTime=0.05)
            box.SendKeys("{Ctrl}v", waitTime=0.15)
            method = "剪贴板粘贴"
        except Exception:
            try:
                box.SendKeys(text, waitTime=0.05)
                method = "逐字符输入"
            except Exception as e:
                raise SendFailed("写入输入框失败: %s" % e)

        # 发送前再确认一次目标——输入过程中焦点可能被别的窗口抢走
        cur2 = self.current_chat()
        if cur2 != expect_chat:
            raise TargetMismatch(
                "输入完成后复核不通过：当前会话变成了 %r。已放弃发送。" % cur2)

        try:
            box.SendKeys("{Enter}", waitTime=0.1)
        except Exception as e:
            raise SendFailed("回车发送失败: %s" % e)

        verified = self._verify_sent(text, verify_timeout)
        return SendOutcome(True, verified, method,
                           "" if verified else "回读超时，未能在界面上确认")

    def _verify_sent(self, text, timeout):
        """
        回读界面确认真的发出去了。

        判据：最近的自己发的消息里出现了这段内容。
        「调用没报错」不等于发出去了，这一步不能省。
        """
        want = text.strip()[:40]
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                for m in reversed(self.read_all()[-12:]):
                    if m.kind != "text":
                        continue
                    if m.is_self is False:
                        continue
                    got = (m.content or "").strip()
                    if want[:20] and want[:20] in got:
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False
