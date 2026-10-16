# -*- coding: utf-8 -*-
"""
微信主窗口定位与健康检查。

只依赖 uiautomation（Apache-2.0）+ ctypes。

这一层要处理的两个坑：
  1. 窗口类名不止一种 —— 实测同一台机器上出现过 'Qt51514QWindowIcon'
     和 'mmui::MainWindow' 两种，微信重启后会变；只认一种的话，重启后就再也找不到窗口。
     这里两种都认，且按特征而非单一类名匹配。
  2. 微信重启后旧的 UIA 元素会失效 —— 所以对外提供 revalidate()，
     发现失效就重新查找，而不是抱着死引用一直重试。
  3. 「窗口没了」和「会话被切走」是两回事 —— 前者重试无意义，必须退避 + 告警。
"""

import ctypes
from ctypes import wintypes

import uiautomation as uia

from .errors import WeChatNotRunning, AccessibilityUnavailable

# 已知的微信主窗口类名。之所以是列表：实测微信在不同启动/登录状态下
# 顶层窗口的 ClassName 会不同，写死一个必然翻车。
MAIN_WINDOW_CLASSES = ("mmui::MainWindow", "Qt51514QWindowIcon")

# 主窗口的判定特征：类名命中之一，且标题是「微信」，且尺寸够大
# （登录窗口/托盘小窗也可能同类名，用尺寸区分）
MIN_MAIN_WIDTH = 500
MIN_MAIN_HEIGHT = 400

_OBJID_CLIENT = 0xFFFFFFFC
_WM_GETOBJECT = 0x003D


def _has_accessibility(hwnd):
    """
    这个窗口有没有实现无障碍接口。

    返回 0 表示完全没有 —— 实测微信 4.1.13.12 就是 0，
    此时任何基于 UI Automation 的方案都读不到内容，换库无用。
    4.1.8.107 返回非 0（实测 49275 / 49280）。
    """
    try:
        return int(ctypes.windll.user32.SendMessageW(
            hwnd, _WM_GETOBJECT, 0, ctypes.c_long(_OBJID_CLIENT).value))
    except Exception:
        return 0


def find_main_window(check_accessibility=True):
    """
    找到微信主窗口，返回 uiautomation 的 Control。

    找不到抛 WeChatNotRunning；找到了但没有无障碍接口抛 AccessibilityUnavailable。
    这两种异常调用方要区别对待：前者可能是微信还没起来（可等），
    后者是微信版本不对（等多久都没用）。
    """
    root = uia.GetRootControl()
    best = None
    best_area = 0
    for child in root.GetChildren():
        try:
            cls = str(child.ClassName or "")
            name = str(child.Name or "")
            rect = child.BoundingRectangle
        except Exception:
            continue
        if cls not in MAIN_WINDOW_CLASSES:
            continue
        if name != "微信":
            continue
        w, h = rect.width(), rect.height()
        if w < MIN_MAIN_WIDTH or h < MIN_MAIN_HEIGHT:
            continue
        if w * h > best_area:
            best, best_area = child, w * h

    if best is None:
        raise WeChatNotRunning(
            "没有找到微信主窗口。请确认：微信在运行、已登录、窗口没有最小化到托盘。")

    if check_accessibility:
        try:
            hwnd = best.NativeWindowHandle
        except Exception:
            hwnd = 0
        if hwnd:
            code = _has_accessibility(hwnd)
            if code == 0:
                raise AccessibilityUnavailable(
                    "微信窗口没有实现无障碍接口（WM_GETOBJECT 返回 0）。"
                    "实测微信 4.1.13 及以上是这种情况，界面自动化完全读不到内容，"
                    "只能改用受支持的微信版本。")
    return best


def is_alive(control):
    """
    这个窗口引用还有效吗。

    微信重启后旧引用会失效，但访问它不一定抛异常，可能只是返回空——
    所以这里主动读一个属性来验活，别等到读消息读出一堆空列表才发现。
    """
    if control is None:
        return False
    try:
        return bool(control.Exists(0, 0)) and str(control.Name or "") == "微信"
    except Exception:
        return False


def find_descendant(node, class_name, max_depth=26):
    """
    在子树里找第一个指定 ClassName 的控件。

    微信的控件层级很深（会话列表在 14 层左右），默认深度给足。
    找不到返回 None —— 由调用方决定这算不算错误。
    """
    if node is None:
        return None
    stack = [(node, 0)]
    while stack:
        cur, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            children = cur.GetChildren()
        except Exception:
            continue
        for ch in children:
            try:
                if str(ch.ClassName or "") == class_name:
                    return ch
            except Exception:
                continue
            stack.append((ch, depth + 1))
    return None
