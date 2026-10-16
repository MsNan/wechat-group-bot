# -*- coding: utf-8 -*-
"""
消息读取与「哪些是新消息」的判定。

两个设计要点：

1. 【self / 别人 用气泡颜色判定，不点头像】
   微信 4.1.8 的 UIA 树里，消息节点 mmui::ChatTextItemView 是【叶子】——
   只有正文，没有子节点，Raw 视图下也没有；头像根本不是 UIA 元素（是画上去的）。
   所以用气泡颜色判定：实测 self=(157,242,159) 绿、别人=(238,238,240) 灰白，
   一次截图就能判完整屏，零点击、不弹窗。

2. 【新消息用序列对齐，不用内容指纹】
   内容指纹会把「同一个人连发两条一样的话」误判成重复。
   这里保留上一轮的完整消息序列，用「最长公共后缀」对齐，
   只有对齐点之后新增的才算新消息。这样：
     · 重复内容        —— 正确识别为两条
     · 向上滚动看历史  —— 历史在前缀，不会被当成新消息
     · 列表虚拟化裁剪  —— 顶部旧消息滚出去了也不影响尾部对齐
"""

import uiautomation as uia

from .errors import ReadFailed

# 消息容器与消息项的控件类名（实测微信 4.1.8.107）
MSG_LIST_CLASS = "mmui::RecyclerListView"
TEXT_ITEM_CLASS = "mmui::ChatTextItemView"
PLAIN_ITEM_CLASS = "mmui::ChatItemView"      # 时间戳、系统提示等

# 气泡颜色特征（实测值，留了容差）
SELF_BUBBLE_RGB = (157, 242, 159)
PEER_BUBBLE_RGB = (238, 238, 240)
COLOR_TOLERANCE = 40


class Message(object):
    """一条消息。sender 可能为 None —— UIA 读不到昵称，需要时再单独取。"""

    __slots__ = ("content", "is_self", "kind", "sender", "rect", "id")

    def __init__(self, content, is_self, kind="text", sender=None, rect=None):
        self.content = content
        self.is_self = is_self
        self.kind = kind            # text / system
        self.sender = sender
        self.rect = rect
        self.id = None          # 由 adapter.read_all_with_ids() 分配

    def key(self):
        """
        用于序列对齐的比较键。

        【只用 kind + content，绝不能包含 is_self】——
        is_self 靠气泡颜色判定，消息滚到视口边缘被裁剪时会判不出（None），
        同一条消息在不同轮次拿到的值可能不一样。把它放进键里，
        键一变对齐就断，整屏消息会被当成新消息重报一遍。
        2026-09-08 实测踩过：一条消息从 self 变 unknown，导致 12 条重复上报。

        时间也不放进来，同理——同一条消息多轮读到必须完全相等。
        """
        return (self.kind, self.content)

    def __repr__(self):
        who = "self" if self.is_self else (self.sender or "peer")
        return "<Msg %s/%s %r>" % (self.kind, who, self.content[:24])


def _rgb_close(a, b, tol=COLOR_TOLERANCE):
    return all(abs(a[i] - b[i]) <= tol for i in range(3))


class _Shot(object):
    """
    消息区的一次截图，用来判气泡颜色。

    一轮 poll 只截一次，然后所有消息共用——避免逐条截图的开销。
    截不到就退化为 None，此时 is_self 交给调用方用别的办法兜底。
    """

    def __init__(self, control):
        self.bmp = None
        self.left = 0
        self.top = 0
        try:
            r = control.BoundingRectangle
            self.left, self.top = r.left, r.top
            self.bmp = uia.Bitmap.FromControl(control)
        except Exception:
            self.bmp = None

    def pixel(self, x, y):
        """传入屏幕坐标，返回 (R,G,B)；失败返回 None。"""
        if self.bmp is None:
            return None
        try:
            argb = self.bmp.GetPixelColor(int(x - self.left), int(y - self.top))
            if argb is None:
                return None
            return ((argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF)
        except Exception:
            return None


def _detect_is_self(shot, rect, list_left, list_right,
                    list_top=None, list_bottom=None):
    """
    判断这条消息是不是自己发的。

    做法：沿消息行中线横扫，找出最宽的一段「非背景色」色块（就是气泡），
    再看它的颜色。颜色比位置更可靠——位置会受窗口宽度、缩放影响。

    取样的 y 会被夹到列表可见范围内：视口顶部/底部的消息行只露出一部分，
    行中线可能落在视口外，那样扫出来是空的。实测就是这么漏掉一条的。

    判不出来返回 None（不猜）。业务层必须把 None 当成「不可回复」处理，
    绝不能默认成「别人发的」——万一那是自己发的就会自问自答死循环。
    """
    if shot is None or shot.bmp is None:
        return None
    y = (rect.top + rect.bottom) // 2
    if list_top is not None and list_bottom is not None:
        # 夹进可见区，并且再往里收 6px 避开边框
        lo, hi = list_top + 6, list_bottom - 6
        if hi > lo:
            y = max(lo, min(hi, y))
    best = None            # (宽度, 颜色)
    cur_start = None
    cur_color = None
    x = list_left + 4
    while x < list_right - 4:
        c = shot.pixel(x, y)
        if c is None:
            x += 6
            continue
        # 背景是近白色 (250,250,250)
        is_bg = _rgb_close(c, (250, 250, 250), 8)
        if not is_bg:
            if cur_start is None:
                cur_start, cur_color = x, c
        else:
            if cur_start is not None:
                width = x - cur_start
                if best is None or width > best[0]:
                    best = (width, cur_color)
                cur_start = None
        x += 6
    if cur_start is not None:
        width = (list_right - 4) - cur_start
        if best is None or width > best[0]:
            best = (width, cur_color)

    if best is None or best[0] < 30:
        return None
    color = best[1]
    if _rgb_close(color, SELF_BUBBLE_RGB):
        return True
    if _rgb_close(color, PEER_BUBBLE_RGB):
        return False
    return None


def read_visible(msg_list):
    """
    读出当前可见的全部消息，按屏幕从上到下排列。

    只读，不点击、不滚动。读取失败抛 ReadFailed —— 注意「读到 0 条」
    不算失败（空会话是合法状态），由上层结合会话校验去判断。
    """
    if msg_list is None:
        raise ReadFailed("消息列表控件不存在")
    try:
        items = msg_list.GetChildren()
        lr = msg_list.BoundingRectangle
    except Exception as e:
        raise ReadFailed("读消息列表失败: %s: %s" % (type(e).__name__, e))

    shot = _Shot(msg_list)
    out = []
    for it in items:
        try:
            cls = str(it.ClassName or "")
            name = str(it.Name or "")
            rect = it.BoundingRectangle
        except Exception:
            continue
        if not name:
            continue
        if cls == TEXT_ITEM_CLASS:
            is_self = _detect_is_self(shot, rect, lr.left, lr.right,
                                      lr.top, lr.bottom)
            out.append(Message(name, is_self, "text", None, rect))
        elif cls == PLAIN_ITEM_CLASS:
            # 时间戳 / 系统提示，一律不回复，但要保留在序列里，
            # 否则它出现时会打乱对齐。
            out.append(Message(name, False, "system", None, rect))
    return out


def diff_new(previous, current):
    """
    对比上一轮与本轮，返回本轮【新增】的消息。

    用最长公共后缀对齐：
        previous:  A B C D
        current:      C D E F   -> 新增 E F
    刻意不用集合去重，所以「D D」这种重复内容能正确识别成两条。

    previous 为 None 表示首轮（建基线），返回空列表 + 全部标记为已见。
    """
    if previous is None:
        return []
    if not current:
        return []

    prev_keys = [m.key() for m in previous]
    cur_keys = [m.key() for m in current]

    # 找最大的 k：previous 的后 k 个 == current 的前 k 个
    max_k = min(len(prev_keys), len(cur_keys))
    for k in range(max_k, 0, -1):
        if prev_keys[-k:] == cur_keys[:k]:
            return current[k:]
    # 完全对不上：可能切了会话或列表被整体刷新。
    # 保守起见全部当新的会造成刷屏，全部当旧的会漏消息；
    # 这里返回 None 交给上层决定（上层应重建基线并记一条 warning）。
    return None
