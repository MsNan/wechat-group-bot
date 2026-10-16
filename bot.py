# -*- coding: utf-8 -*-
"""
微信群消息自动读取 + 自动回复

回复决策链（从上往下，命中即停）：
    1. 业务指令  handlers.py    查库 / 调后端接口 / 写库   ← 优先级最高
    2. 关键词规则 config.RULES   固定话术，零成本
    3. AI 兜底   ai_provider.py  前两层没命中才轮到它
    4. FALLBACK_REPLY

为什么业务指令必须排在 AI 前面：订单号、库存、金额这类数据只能来自数据库，
交给 AI 它就会编。AI 只负责闲聊。

═══════════════════════════════════════════════════════════════════
收消息的方式：自己轮询
═══════════════════════════════════════════════════════════════════
每隔 POLL_INTERVAL 秒读一次当前会话的可见消息，和上一轮对齐算出新增。
好处：
  · 所有异常都落在我们自己的 bot.log 里
  · 收 / 过滤 / AI / 发 全流程可以用同一个 trace id 串起来
  · 轮询间隔、失败退避、重启不重放历史 全部可控

运行环境：Windows + 微信 PC 客户端（已登录，主窗口打开，别缩托盘）
跑之前先跑 check_env.py。停止：Ctrl+C
"""

import os
import sys
import time
import random
import logging
import threading
import itertools
from collections import deque

import config
import handlers
import ai_provider
import backend


# ====================================================================
# 打包兼容：必须在导入 wxui 之前执行，晚了就没用了
# ====================================================================
def _fix_comtypes_for_frozen():
    """
    wxui 底层是 uiautomation -> comtypes。comtypes 默认会把动态生成的
    COM 接口包装代码写到 site-packages/comtypes/gen/ 这个磁盘缓存目录。
    打包成 exe 后那个目录不存在或只读，会炸成：
        FileNotFoundError: ...comtypes\\gen\\_944DE083_..._0_1_0.py

    gen_dir = None 让它只在内存里生成，完全不碰文件系统。
    只在打包运行时生效；源码运行时保持默认（用磁盘缓存，启动更快）。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        import comtypes.client
        comtypes.client.gen_dir = None
    except Exception:
        pass


_fix_comtypes_for_frozen()


# ====================================================================
# 微信版本探测
# ====================================================================
def _exe_version(path):
    """读 exe 的文件版本号，返回 '4.1.13.12' 这种字符串。"""
    try:
        import ctypes
        from ctypes import wintypes
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf)
        r = ctypes.c_void_p()
        ln = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
                buf, "\\", ctypes.byref(r), ctypes.byref(ln)):
            return None
        d = ctypes.string_at(r.value, ln.value)
        ms = int.from_bytes(d[8:12], "little")
        ls = int.from_bytes(d[12:16], "little")
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


def _detect_wechat_version():
    """
    拿微信客户端的完整版本号，比如 '4.1.13.12' / '3.9.12.17'。取不到返回 None。

    为什么不能只读老那几个注册表键：这个键【不保证存在】，且各版本行为不一致。
    2026-09-07 在虚拟机（微信 4.1.13.12）上复测：
        HKCU\\Software\\Tencent\\Weixin 是【存在】的，
        值为 Version=4065598732, InstallPath='C:\\Program Files\\Tencent\\Weixin'
    —— 早先记的「4.1.13 把 InstallPath 挪走了、键根本不存在」是错的，已更正。
    但卸载重装后这个键会短暂缺失，所以仍然不能只靠它。
    所以按「卸载信息表 -> 老注册表键 -> 固定安装目录」三条路依次找。
    """
    if sys.platform != "win32":
        return None

    candidates = []

    # --- 路径 1：已安装程序列表（4.1.13 实测走得通的就是这条）---
    try:
        import winreg
        for hive, sub in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]:
            try:
                with winreg.OpenKey(hive, sub) as root:
                    for i in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                                name = winreg.QueryValueEx(k, "DisplayName")[0]
                                if name not in ("微信", "WeChat", "Weixin"):
                                    continue
                                try:
                                    ver = winreg.QueryValueEx(k, "DisplayVersion")[0]
                                    if ver:
                                        candidates.append(ver)
                                except OSError:
                                    pass
                                try:
                                    loc = winreg.QueryValueEx(k, "InstallLocation")[0]
                                    for exe in ("Weixin.exe", "WeChat.exe"):
                                        p = os.path.join(loc, exe)
                                        if os.path.exists(p):
                                            v = _exe_version(p)
                                            if v:
                                                candidates.append(v)
                                except OSError:
                                    pass
                        except OSError:
                            continue
            except OSError:
                continue
    except Exception:
        pass

    # --- 路径 2：老注册表键（3.9.x 和早期 4.x 走这条）---
    try:
        import winreg
        for hive, path in [
            (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\WeChat"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\WeChat"),
            (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\Weixin"),
        ]:
            try:
                with winreg.OpenKey(hive, path) as k:
                    ip = winreg.QueryValueEx(k, "InstallPath")[0]
            except OSError:
                continue
            if not ip or not os.path.isdir(ip):
                continue
            # 3.9.x 的安装目录里有 [3.9.12.17] 这种版本号文件夹
            for name in sorted(os.listdir(ip), reverse=True):
                if name.startswith("[") and name.endswith("]"):
                    candidates.append(name.strip("[]"))
            for exe in ("Weixin.exe", "WeChat.exe"):
                p = os.path.join(ip, exe)
                if os.path.exists(p):
                    v = _exe_version(p)
                    if v:
                        candidates.append(v)
    except Exception:
        pass

    # --- 路径 3：固定安装目录兜底 ---
    for d in (r"C:\Program Files\Tencent\Weixin",
              r"C:\Program Files\Tencent\WeChat",
              r"C:\Program Files (x86)\Tencent\WeChat"):
        for exe in ("Weixin.exe", "WeChat.exe"):
            p = os.path.join(d, exe)
            if os.path.exists(p):
                v = _exe_version(p)
                if v:
                    candidates.append(v)

    for v in candidates:
        if v and v[0].isdigit():
            return v
    return None


def _ver_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:4])
    except Exception:
        return ()


import wxui  # noqa: E402  必须在 _fix_comtypes_for_frozen() 之后导入

WeChat = wxui.WeChat


# 实测通过的微信版本上限。
# 更高版本（4.1.13+）没有实现无障碍接口，界面自动化整条路都不通，
# wxui 会在连接时抛 AccessibilityUnavailable 明说。
# 微信会静默自动升级 —— 这台机器就是这么坏的：9/4 还是 4.1.8.107，之后自动升到了 4.1.13.12。
SUPPORTED_CEILING = (4, 1, 8, 107)


def _check_version_compat():
    """开机就把「微信版本 vs 适配上限」讲清楚，别等连不上了才猜。"""
    wv = _detect_wechat_version()
    if not wv:
        log.warning("没读出微信客户端版本（几条探测路径都没命中）。"
                    "手动核对：微信 -> 设置 -> 关于微信")
        return
    log.info(f"微信客户端: {wv}")
    if _ver_tuple(wv) > SUPPORTED_CEILING:
        top = ".".join(str(x) for x in SUPPORTED_CEILING)
        log.warning("=" * 58)
        log.warning(f"微信 {wv} 高于适配上限 {top}")
        log.warning("这个版本没有无障碍接口，连接时会直接报 AccessibilityUnavailable。")
        log.warning(f"出路：把微信降回 {top}，并【关掉自动更新】——")
        log.warning("      不关的话过几天会自动升回去，同样的故障会原样复现。")
        log.warning("=" * 58)


def _uia_version():
    try:
        from importlib.metadata import version
        return version("uiautomation")
    except Exception:
        return "?"


# ====================================================================
# 日志
# ====================================================================
def _setup_logging():
    run_log = config.log_path(config.RUN_LOG)
    msg_log_path = config.log_path(config.MESSAGE_LOG)

    for path in (run_log, msg_log_path):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    # encoding 必须显式 utf-8，否则 Windows 默认 GBK 会在 emoji 上炸
    logger = logging.getLogger("bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fh = logging.FileHandler(run_log, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    if getattr(config, "PRINT_TO_CONSOLE", True):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        logger.addHandler(sh)

    ml = logging.getLogger("messages")
    ml.setLevel(logging.INFO)
    ml.handlers.clear()
    ml.propagate = False
    mh = logging.FileHandler(msg_log_path, encoding="utf-8")
    mh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    ml.addHandler(mh)

    return logger, ml


log, msg_log = _setup_logging()

# 每条消息一个 trace id，收 / 过滤 / AI / 发 全用它串起来
_trace_seq = itertools.count(1)


def new_trace():
    return f"m{next(_trace_seq):05d}"


# ====================================================================
# 全局组件（main 里初始化）
# ====================================================================
AI = None        # ai_provider.AIEngine
API = None       # backend.BackendAPI
DB = None        # backend.Database
STORE = None     # backend.MessageStore


# ====================================================================
# 限流 / 去重
# ====================================================================
class Throttle:
    def __init__(self):
        self._lock = threading.Lock()
        self._sender_last = {}
        self._group_last = {}
        self._hour_window = deque()
        self._seen = deque(maxlen=500)
        self._seen_set = set()

    def is_duplicate(self, fingerprint):
        """
        内容指纹去重，【只在 DEDUP_WINDOW 秒内有效】。

        这是第二道保险：第一道是 MessageStream 按消息 id 去重（更准）。
        这一道防的是同一条消息被读到两次却拿到了不同 id 的情况。
        有效期不能设成永久 —— 否则同一个人今天说「在吗」、明天再说「在吗」，
        第二次就永远收不到回复了。
        """
        now = time.time()
        ttl = getattr(config, "DEDUP_WINDOW", 120)
        # ttl <= 0 表示关掉这道去重。
        # 用 wxui 适配层时应当关掉：它按序列对齐分配稳定 id，同一条消息
        # 多轮读到 id 不变，而「同一个人连发两条一样的话」会拿到两个不同 id。
        # 这时再叠一层内容指纹，只会把合法的重复发言误杀。
        # 2026-09-08 实测：用户故意连发两条「在吗」，第二条被这里吞掉了。
        if ttl <= 0:
            return False
        with self._lock:
            while self._seen and now - self._seen[0][1] > ttl:
                old_fp, _ = self._seen.popleft()
                self._seen_set.discard(old_fp)

            if fingerprint in self._seen_set:
                return True

            if len(self._seen) == self._seen.maxlen and self._seen:
                old_fp, _ = self._seen[0]
                self._seen_set.discard(old_fp)

            self._seen.append((fingerprint, now))
            self._seen_set.add(fingerprint)
            return False

    def check(self, sender, group):
        now = time.time()
        with self._lock:
            while self._hour_window and now - self._hour_window[0] > 3600:
                self._hour_window.popleft()

            if len(self._hour_window) >= config.MAX_REPLIES_PER_HOUR:
                return False, f"已达每小时上限 {config.MAX_REPLIES_PER_HOUR} 条"

            last = self._sender_last.get(sender, 0)
            if now - last < config.PER_SENDER_COOLDOWN:
                left = int(config.PER_SENDER_COOLDOWN - (now - last))
                return False, f"对 {sender} 冷却中（还剩 {left}s）"

            if now - self._group_last.get(group, 0) < config.PER_GROUP_COOLDOWN:
                return False, f"群 {group} 冷却中"

            return True, ""

    def commit(self, sender, group):
        now = time.time()
        with self._lock:
            self._sender_last[sender] = now
            self._group_last[group] = now
            self._hour_window.append(now)


throttle = Throttle()


# ====================================================================
# 消息流 —— 自己轮询
# ====================================================================
def norm_msg(msg):
    """
    返回 (attr, mtype)。全项目只认这个函数的输出，不要再在别处直接读 msg.attr / msg.type。

    取值见 wxui/compat.py 的 CompatMessage：
        attr  ∈ friend / self / system / unknown   —— 谁发的（unknown = 判不出来）
        mtype ∈ text / time / other                —— 内容类型
    """
    attr = getattr(msg, "attr", "") or ""
    mtype = getattr(msg, "type", "") or "text"
    # 取不到 attr 就如实返回 "?"，让日志能看出来，而不是假装成 friend
    return (attr or "?"), mtype


def _msg_key(msg):
    """
    一条消息的唯一键。

    优先用适配层分配的 id。取不到就退回
    「类型+发送者+内容+hash」组合。绝不能只用内容，否则同一个人
    连发两条一样的话，第二条会被误判成重复。
    """
    mid = getattr(msg, "id", None)
    if mid:
        return f"id:{mid}"
    h = getattr(msg, "hash", None)
    a, t = norm_msg(msg)
    return "fb:%s|%s|%s|%s" % (
        a, t,
        getattr(msg, "sender", "?"),
        h or (getattr(msg, "content", "") or "")[:200],
    )


class MessageStream:
    """
    盯住一个会话，每次 poll() 返回【自上次以来的新消息】。

    首次 poll 只做基线（把当前屏上的历史全部标记为已读）并返回空 ——
    这样重启机器人不会把历史消息重新回一遍。
    """

    MAX_SEEN = 3000
    SWITCH_RETRIES = 3      # 主窗口模式下切会话的重试次数
    SWITCH_WAIT = 1.5       # 每次切换后等界面刷新的秒数

    def __init__(self, wx, group):
        self.wx = wx
        self.group = group
        self.chat = None
        self.mode = "?"
        self._seen = set()
        self._seen_order = deque()
        self._primed = False
        self.poll_count = 0
        self.new_count = 0
        self.consec_errors = 0
        self.drift_count = 0

    # ---------- 拿到能读消息的 Chat 对象 ----------
    def acquire(self):
        """
        切到目标会话并校验，返回 (ok, 说明)。

        【必须校验切换结果】，不能「ChatWith 没抛异常就当成功」——
        2026-09-07 实测过一次：切换调用正常返回、没抛异常，但主窗口右侧
        依然是空白默认页，读消息恒返回 0 条。原因没有定位到，所以每次切完
        都用 ChatInfo() 核对会话名，不一致就重试。
        """
        last = ""
        for attempt in range(1, self.SWITCH_RETRIES + 1):
            try:
                self.wx.ChatWith(self.group)
            except Exception as e:
                last = f"ChatWith 抛 {type(e).__name__}: {e}"
                log.error(f"ChatWith({self.group}) 第 {attempt} 次失败: {last}")
                time.sleep(self.SWITCH_WAIT)
                continue

            time.sleep(self.SWITCH_WAIT)
            ok, why = self._on_target_chat()
            if ok:
                self.chat = self.wx
                self.mode = "主窗口"
                return True, (f"已校验当前会话确为「{self.group}」"
                              f"（第 {attempt} 次切换成功）。"
                              f"轮询时会持续校验，被切走会自动切回。")
            last = why
            log.warning(f"ChatWith({self.group}) 第 {attempt} 次【没切过去】: {why}")

        return False, f"切到「{self.group}」失败（试了 {self.SWITCH_RETRIES} 次）：{last}"

    # ---------- 当前会话校验 ----------
    def _on_target_chat(self):
        """
        主窗口现在是不是停在目标会话上。返回 (是否, 说明)。

        优先用 ChatInfo() 取当前会话名。
        取不到就退而用「能不能读到消息」做弱判定（读到 0 条基本就是没切过去，
        因为主窗口没打开会话时 GetAllMessage() 恒为 0）。
        """
        info_fn = getattr(self.wx, "ChatInfo", None)
        if callable(info_fn):
            try:
                info = info_fn() or {}
                name = info.get("chat_name") if isinstance(info, dict) else None
                if name is not None:
                    if name == self.group:
                        return True, f"ChatInfo 确认当前会话={name!r}"
                    return False, f"当前会话是 {name!r}，不是 {self.group!r}"
            except Exception as e:
                log.debug(f"ChatInfo() 取失败，退回弱判定: {type(e).__name__}: {e}")

        try:
            n = len(self.wx.GetAllMessage())
        except Exception as e:
            return False, f"GetAllMessage 抛 {type(e).__name__}: {e}"
        if n > 0:
            return True, f"弱判定：读到 {n} 条消息"
        return False, "弱判定：读到 0 条（主窗口很可能没打开会话）"

    # ---------- 轮询 ----------
    def poll(self):
        """返回新消息列表。异常不外抛，但会记进我们自己的日志。"""
        self.poll_count += 1

        # 主窗口模式下，读之前先确认还停在目标会话上。
        # 原来这里只在文档里写「期间不要手动切换聊天」，靠人自觉 ——
        # 一旦被切走（人手动点了别的会话、或微信自己弹窗抢焦点），
        # 就会安静地去读【别人的聊天】，既漏消息又可能回错群。
        if self.mode.startswith("主窗口"):
            ok, why = self._on_target_chat()
            if not ok:
                self.drift_count += 1
                log.warning(f"[{self.group}] 会话被切走了（{why}），第 "
                            f"{self.drift_count} 次，正在切回")
                try:
                    self.wx.ChatWith(self.group)
                    time.sleep(self.SWITCH_WAIT)
                    ok2, why2 = self._on_target_chat()
                    if not ok2:
                        log.error(f"[{self.group}] 切回失败：{why2}，本轮跳过")
                        return []
                    log.info(f"[{self.group}] 已切回")
                except Exception as e:
                    log.error(f"[{self.group}] 切回时抛异常: "
                              f"{type(e).__name__}: {e}")
                    return []

        try:
            msgs = self.chat.GetAllMessage()
            self.consec_errors = 0
        except Exception as e:
            self.consec_errors += 1
            # 前 3 次和之后每 20 次记一条，避免刷屏但绝不静默
            if self.consec_errors <= 3 or self.consec_errors % 20 == 0:
                log.exception(
                    f"[{self.group}] 读消息失败（连续第 {self.consec_errors} 次）: "
                    f"{type(e).__name__}: {e}")
            return []

        if not self._primed:
            for m in msgs:
                self._remember(_msg_key(m))
            self._primed = True
            log.info(f"[{self.group}] 已建立基线：屏上 {len(msgs)} 条历史消息标记为已读"
                     f"（重启不会重复回复历史）")
            return []

        fresh = []
        for m in msgs:
            k = _msg_key(m)
            if k in self._seen:
                continue
            self._remember(k)
            fresh.append(m)

        self.new_count += len(fresh)
        return fresh

    def _remember(self, key):
        if key in self._seen:
            return
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > self.MAX_SEEN:
            self._seen.discard(self._seen_order.popleft())


def _wx_ok(res):
    """
    判断发送接口的返回值是成功还是失败。

    返回 (True/False/None, 说明)。None = 没有返回值，无从判断。
    """
    if res is None:
        return None, ""
    succ = getattr(res, "is_success", None)
    if succ is not None:
        try:
            return bool(succ), str(res.get("message", "") if hasattr(res, "get") else "")
        except Exception:
            return bool(succ), ""
    if isinstance(res, dict) and "status" in res:
        return res.get("status") == "成功", str(res.get("message", ""))
    if isinstance(res, bool):
        return res, ""
    return None, ""


# ====================================================================
# 第 2 层：关键词规则
# ====================================================================
def match_rules(sender, content, group):
    text = content.strip()
    if not text:
        return None
    lowered = text.lower()

    for rule in config.RULES:
        mode = rule.get("mode", "contains")
        keywords = rule.get("keywords", [])
        if mode == "exact":
            hit = any(lowered == kw.lower() for kw in keywords)
        else:
            hit = any(kw.lower() in lowered for kw in keywords)
        if hit:
            return rule["reply"].format(sender=sender, group=group)
    return None


# ====================================================================
# 决策链总入口
# ====================================================================
def build_reply(trace, sender, content, group, at_me):
    """返回 (回复文本 或 None, 命中的层)。"""

    # --- 第 1 层：业务指令（查库 / 调后端 / 写库）---
    result = handlers.route(sender, content, group, api=API, db=DB, store=STORE)

    if isinstance(result, handlers.AIAnswer):
        # 查到真实数据了，让 AI 用人话组织一下
        if AI and AI.enabled:
            log.info(f"[{trace}] AI 请求：把业务数据组织成人话")
            text = _ai_reply(trace, sender, content, group,
                             extra_context=result.as_context())
            if text:
                return text, "业务指令+AI播报"
        # AI 不可用就把原始数据直接甩出去，总比不回强
        return result.as_context(), "业务指令(AI不可用,直出数据)"

    if isinstance(result, str) and result:
        return result, "业务指令"

    # --- 第 2 层：关键词规则 ---
    text = match_rules(sender, content, group)
    if text:
        return text, "关键词规则"

    # --- 第 3 层：AI 兜底 ---
    if AI and AI.enabled:
        ai_cfg = config.AI
        if ai_cfg.get("only_when_at_me", True) and not at_me:
            log.info(f"[{trace}] 跳过 AI：没 @ 我（only_when_at_me=True）")
        elif len(content.strip()) < ai_cfg.get("min_length", 2):
            log.info(f"[{trace}] 跳过 AI：消息太短")
        else:
            log.info(f"[{trace}] AI 请求：发出")
            text = _ai_reply(trace, sender, content, group)
            if text:
                return text, "AI"
    elif not AI:
        pass  # AI 整体关闭，正常情况，不刷日志

    # --- 第 4 层 ---
    if config.FALLBACK_REPLY:
        return config.FALLBACK_REPLY, "兜底话术"
    return None, "无命中"


def _ai_reply(trace, sender, content, group, extra_context=""):
    """包一层，把 AI 是否真的返回内容记进同一条 trace。"""
    t0 = time.time()
    try:
        text = AI.reply(sender, content, group, extra_context=extra_context)
    except Exception as e:
        log.exception(f"[{trace}] AI 调用抛异常: {type(e).__name__}: {e}")
        return None
    cost = time.time() - t0
    if text:
        log.info(f"[{trace}] AI 返回有效内容（{len(text)} 字，耗时 {cost:.1f}s）")
    else:
        log.warning(f"[{trace}] AI 没返回有效内容（耗时 {cost:.1f}s），本条不回")
    return text


# ====================================================================
# 消息过滤
# ====================================================================
def _is_at_me(content):
    """微信的 @ 后面跟的是特殊空格 U+2005，不是普通空格，所以只匹配前缀。"""
    if not config.MY_NICKNAME:
        return False
    return f"@{config.MY_NICKNAME}" in content


def should_handle(msg, group):
    """
    返回 (是否处理, 原因)。原因一定要写清楚，跳过的每一条都要能在日志里查到为什么。

    attr / type 的取值见 wxui/compat.py 的 CompatMessage：
        attr ∈ friend / self / system / unknown
        type ∈ text / time / other
    """
    attr, mtype = norm_msg(msg)

    # 最重要的一条：绝不处理自己发的，否则机器人会自己跟自己聊到天荒地老。
    # 机器人自己发出去的回复，下一轮轮询会被读到，attr 就是 self，在这里被挡掉。
    if attr == "self":
        return False, "自己发的（含机器人自己的回复）"
    if attr in ("system", "time", "tickle"):
        return False, f"非用户消息(attr={attr})"
    if attr != "friend":
        return False, f"来源类型不是 friend(attr={attr})"
    if mtype not in ("text", "quote"):
        return False, f"非文本消息(type={mtype})"

    sender = getattr(msg, "sender", "") or ""
    if config.MY_NICKNAME and sender == config.MY_NICKNAME:
        return False, "发送者是我自己（昵称相同）"
    if sender in config.SENDER_BLACKLIST:
        return False, "发送者在黑名单"
    if config.SENDER_WHITELIST and sender not in config.SENDER_WHITELIST:
        return False, "发送者不在白名单"

    content = getattr(msg, "content", "") or ""
    if not content.strip():
        return False, "内容为空"
    if config.ONLY_REPLY_WHEN_AT_ME and not _is_at_me(content):
        return False, "没有 @ 我（ONLY_REPLY_WHEN_AT_ME=True）"

    return True, ""


# ====================================================================
# 发送
# ====================================================================
class SendResult:
    """
    把「调用了发送方法」和「确认发出去了」分开表达 —— 这两件事不是一回事，
    不能因为没抛异常就报告成功。

    called   : 发送方法被成功调用（没抛异常）
    api_ok   : 库的返回值说成功。None = 该版本没有返回值，无从判断
    ui_ok    : 回读聊天窗口，在里面看到了这条自己发的消息。None = 没做这个校验
    """

    def __init__(self, called=False, api_ok=None, ui_ok=None, method="", err=None):
        self.called = called
        self.api_ok = api_ok
        self.ui_ok = ui_ok
        self.method = method
        self.err = err

    @property
    def confirmed(self):
        """只有界面里真看到了，才算「确认已发送」。"""
        return self.ui_ok is True

    def describe(self):
        if not self.called:
            return f"发送失败：{self.err}"
        parts = [f"已调用 {self.method}"]
        if self.api_ok is True:
            parts.append("接口返回成功")
        elif self.api_ok is False:
            parts.append("接口返回失败")
        else:
            parts.append("该版本无返回值·接口层无法判断")
        if self.ui_ok is True:
            parts.append("界面已确认")
        elif self.ui_ok is False:
            parts.append("界面未确认（没在聊天里回读到）")
        return "；".join(parts)


def send_reply(trace, chat, msg, reply, sender):
    """
    发送并尽量确认。返回 SendResult。

    按「最想要 -> 最保底」的顺序依次试：引用回复 -> 带 @ -> 纯文本，
    前一种底层不支持就换下一种。
    """
    reply = reply[: config.MAX_REPLY_LENGTH]

    # 模拟人的反应时间。秒回是风控最明显的特征之一。
    delay = random.uniform(*config.REPLY_DELAY_RANGE)
    log.info(f"[{trace}] 准备发送，先等 {delay:.1f}s（防秒回特征）")
    time.sleep(delay)

    at = sender if (config.REPLY_WITH_AT and sender) else None
    # 后端明确声明不支持 @ 时（自研 wxui 就是），直接不带 @。
    # 它改用正文里带称呼，效果一样且不会 @ 错人。
    # 不加这个判断的话，下面的落点校验会因为「开头没有 @」而误报「@ 落错人」。
    if at is not None:
        _supports = getattr(chat, "supports_at", None)
        if callable(_supports) and not _supports():
            at = None
    if at and at in _AT_BLACKLIST:
        # 之前对这个人 @ 错过（落到了别人头上），不再尝试，改用纯文本
        log.info(f"[{trace}] {at!r} 在不@名单里（之前 @ 落错人），本次不带 @")
        at = None

    attempts = []
    if config.REPLY_AS_QUOTE and hasattr(msg, "quote"):
        if at:
            attempts.append(("msg.quote(带@)", lambda: msg.quote(reply, at=at)))
        attempts.append(("msg.quote", lambda: msg.quote(reply)))
    if at:
        attempts.append(("chat.SendMsg(带@)", lambda: chat.SendMsg(reply, at=at)))
    attempts.append(("chat.SendMsg", lambda: chat.SendMsg(reply)))

    last_err = None
    for name, fn in attempts:
        try:
            res = fn()
        except TypeError as e:
            last_err = e
            log.info(f"[{trace}]   {name} 签名不匹配，换下一种: {e}")
            continue
        except AttributeError as e:
            last_err = e
            log.info(f"[{trace}]   {name} 方法不存在，换下一种: {e}")
            continue
        except Exception as e:
            # 其它异常是真错误，不该当成「换一种就好」而吞掉
            last_err = e
            log.exception(f"[{trace}]   {name} 抛异常: {type(e).__name__}: {e}")
            continue

        api_ok, why = _wx_ok(res)
        if api_ok is False:
            log.warning(f"[{trace}]   {name} 接口返回失败: {why}，换下一种")
            last_err = RuntimeError(f"{name} 返回失败: {why}")
            continue

        ui_ok, actual = _verify_in_ui(trace, chat, reply)
        if at and actual:
            _check_at_landed(trace, at, actual)
        return SendResult(called=True, api_ok=api_ok, ui_ok=ui_ok, method=name)

    return SendResult(called=False, err=f"所有发送方式都失败，最后一个错误: {last_err}")


# @ 落错人的人名黑名单（进程内）。
#
# 背景（2026-09-07 实测）：回复 郭涛 时 at='郭涛' 传得没错，但微信里实际发出的是
# 「@tao 在的 郭涛，…」—— @ 落到了机器人自己头上。微信的 @ 要在群成员候选列表里
# 挑人，匹配不上就选了第一项，而发送前拿不到群成员表，无法校验。
# wxui 声明不支持 @（supports_at() 为 False），所以这段只在底层支持 @ 时才起作用：
# 发完回读实际文本，比对开头是不是「@目标名」，错了就把这个人拉进名单，之后改用纯文本。
_AT_BLACKLIST = set()


def _check_at_landed(trace, at, actual):
    """校验 @ 有没有落到正确的人身上。actual 是回读到的实际发出文本。"""
    head = (actual or "").lstrip()
    if head.startswith(f"@{at}"):
        return True
    # 取实际 @ 到的那个名字，方便排查
    got = ""
    if head.startswith("@"):
        got = head[1:].split()[0] if len(head) > 1 else ""
    _AT_BLACKLIST.add(at)
    log.warning(
        f"[{trace}] @ 落错人了：本想 @{at}，实际发出的是 {head[:40]!r}"
        + (f"（@ 到了 {got!r}）" if got else "（开头没有 @）")
        + f"。已把 {at!r} 加入不@名单，之后对他改用纯文本发送。"
        f"根因：微信的 @ 靠候选列表挑人，发送前无法校验。")
    return False


def _verify_in_ui(trace, chat, reply):
    """
    回读聊天窗口，确认这条消息真的出现在界面里。

    这是「界面确认已发送」和「只是调用了发送方法」的区别所在。
    关掉：config.VERIFY_SENT_IN_UI = False（省一次 UI 读取，但就只能报告"已调用"）
    """
    if not getattr(config, "VERIFY_SENT_IN_UI", True):
        return None, None

    want = reply.strip()[:60]
    deadline = time.time() + getattr(config, "VERIFY_SENT_TIMEOUT", 6)
    while time.time() < deadline:
        try:
            for m in reversed(chat.GetAllMessage()[-12:]):
                if norm_msg(m)[0] != "self":
                    continue
                got = (getattr(m, "content", "") or "").strip()
                if got.startswith(want[:30]) or want[:30] in got:
                    # 连实际发出的原文一起返回 —— @ 校验要用它
                    return True, got
        except Exception as e:
            log.debug(f"[{trace}] 回读校验读取失败: {e}")
        time.sleep(0.6)
    return False, None


# ====================================================================
# 单条消息处理
# ====================================================================
def handle_message(msg, chat, group):
    """
    处理一条消息。绝不向外抛异常 —— 抛了会打断整个轮询循环。
    """
    trace = new_trace()
    try:
        _handle(trace, msg, chat, group)
    except Exception as e:
        log.exception(f"[{trace}] 处理消息出错（已兜住，轮询继续）: "
                      f"{type(e).__name__}: {e}")


def _handle(trace, msg, chat, group):
    sender = getattr(msg, "sender", "") or "未知"
    content = getattr(msg, "content", "") or ""
    attr, mtype = norm_msg(msg)

    msg_log.info(f"[{trace}] [{group}] <{sender}> ({attr}/{mtype}) {content}")
    log.info(f"[{trace}] 收到 [{group}] {sender}({attr}/{mtype}): {content[:80]}")

    ok, reason = should_handle(msg, group)
    if not ok:
        log.info(f"[{trace}] 跳过：{reason}")
        _save(group, sender, content, mtype, attr, False, "")
        return

    if throttle.is_duplicate(f"{group}|{sender}|{content}"):
        log.info(f"[{trace}] 跳过：{config.DEDUP_WINDOW}s 内的重复内容")
        return

    at_me = _is_at_me(content)
    reply, layer = build_reply(trace, sender, content, group, at_me)

    if not reply:
        log.info(f"[{trace}] 不回复：决策链没产出内容（{layer}）")
        _save(group, sender, content, mtype, attr, False, "")
        return

    log.info(f"[{trace}] 决定回复（命中层：{layer}）: {reply[:80]}")

    allowed, why = throttle.check(sender, group)
    if not allowed:
        log.info(f"[{trace}] 有回复内容但不发：{why}")
        _save(group, sender, content, mtype, attr, False, "")
        return

    if config.DRY_RUN:
        log.info(f"[{trace}] [演习] 本来会回复: {reply}")
        _save(group, sender, content, mtype, attr, False, reply)
        return

    result = send_reply(trace, chat, msg, reply, sender)

    if result.called and result.api_ok is not False:
        throttle.commit(sender, group)
        level = log.info if result.confirmed else log.warning
        level(f"[{trace}] 发送结果：{result.describe()}")
        _save(group, sender, content, mtype, attr, result.confirmed, reply)
        SEND_FAILS.reset()
    else:
        log.error(f"[{trace}] 发送结果：{result.describe()}")
        _save(group, sender, content, mtype, attr, False, reply)
        SEND_FAILS.hit(trace)


class SendFailGuard:
    """
    连续发送失败保护：不做无限重试。

    连续失败到阈值就进入冷静期，冷静期内不再尝试发送（但仍继续收消息、记日志），
    避免在微信异常/被限制时反复戳它。
    """

    def __init__(self):
        self._n = 0
        self._quiet_until = 0

    def hit(self, trace):
        self._n += 1
        limit = getattr(config, "MAX_CONSECUTIVE_SEND_FAILURES", 5)
        if self._n >= limit:
            cool = getattr(config, "SEND_FAIL_COOLDOWN", 300)
            self._quiet_until = time.time() + cool
            self._n = 0
            log.error(f"[{trace}] 连续发送失败 {limit} 次，进入 {cool}s 冷静期，"
                      f"期间只收不发。请检查微信窗口是否被关掉/被风控。")

    def reset(self):
        self._n = 0

    @property
    def muted(self):
        return time.time() < self._quiet_until


SEND_FAILS = SendFailGuard()


def _save(group, sender, content, mtype, attr, replied, reply_text):
    if not (STORE and STORE.enabled):
        return
    try:
        STORE.save(group, sender, content, msg_type=mtype, msg_attr=attr,
                   replied=replied, reply_text=reply_text)
    except Exception as e:
        log.warning(f"  消息入库失败: {e}")


# ====================================================================
# 启动
# ====================================================================
def _init_components():
    global AI, API, DB, STORE

    API, DB, STORE = backend.build(config.BACKEND)
    AI = ai_provider.build_engine(config.AI)

    log.info("-" * 60)
    log.info(f"微信适配层: wxui（uiautomation {_uia_version()}）")
    log.info(f"Python    : {sys.version.split()[0]} @ {sys.executable}")
    log.info(f"代码文件  : {os.path.abspath(__file__)}")
    log.info(f"配置来源  : {'settings.json + config.py' if config.LOADED_FROM_JSON else 'config.py'}")
    log.info(f"业务指令  : 已注册 {len(handlers.HANDLERS)} 条")

    if AI and AI.enabled:
        names = " -> ".join(p.label for p in AI.providers)
        scope = "仅 @我 时" if config.AI.get("only_when_at_me") else "所有消息"
        log.info(f"AI 回复   : 开启（{names}），触发条件：{scope}")
    else:
        log.info("AI 回复   : 关闭")

    log.info(f"后端接口  : {'开启 ' + API.base_url if (API and API.enabled) else '关闭'}")
    if DB and DB.enabled:
        log.info(f"数据库    : 开启 {DB.cfg['host']}/{DB.cfg['database']} "
                 f"(写权限:{'开' if DB.allow_write else '关'})")
    else:
        log.info("数据库    : 关闭")
    log.info(f"消息入库  : {'开启' if (STORE and STORE.enabled) else '关闭'}")
    log.info(f"轮询间隔  : {getattr(config, 'POLL_INTERVAL', 1.5)}s")
    log.info(f"发送校验  : {'开（回读界面确认）' if getattr(config, 'VERIFY_SENT_IN_UI', True) else '关'}")
    log.info("-" * 60)


def main():
    print("=" * 62)
    print("  微信群自动回复机器人")
    print("=" * 62)

    _init_components()

    if config.DRY_RUN:
        log.info(">>> 演习模式 DRY_RUN=True：只记录，不会真发消息 <<<")
    else:
        log.info(">>> 实发模式：会真的往群里发消息 <<<")

    if not config.LISTEN_GROUPS:
        log.error("LISTEN_GROUPS 是空的，没群可监听。")
        input("按回车退出...")
        return

    # 目前只支持单群：多个会话共用同一个微信主窗口，轮询时会互相把对方切走。
    if len(config.LISTEN_GROUPS) > 1:
        log.warning(f"LISTEN_GROUPS 配了 {len(config.LISTEN_GROUPS)} 个，"
                    f"目前只支持单群，只会盯第一个：{config.LISTEN_GROUPS[0]}")

    _check_version_compat()

    try:
        wx = WeChat()
    except Exception as e:
        log.error(f"连不上微信: {type(e).__name__}: {e}")
        log.error("排查顺序：1) 微信登录了吗 2) 主窗口开着吗（不能缩托盘）"
                  " 3) 腾讯电脑管家之类的安全软件在不在拦 UI 自动化"
                  " 4) 试试管理员权限运行")
        input("按回车退出...")
        return

    log.info(f"已连接微信: {getattr(wx, 'nickname', '?')}")
    if config.MY_NICKNAME and getattr(wx, "nickname", None) \
            and wx.nickname != config.MY_NICKNAME:
        log.warning(f"config.MY_NICKNAME={config.MY_NICKNAME!r} 跟实际登录昵称 "
                    f"{wx.nickname!r} 对不上，@我 的判断会失效。建议改成实际昵称。")

    streams = []
    for group in config.LISTEN_GROUPS[:1]:
        st = MessageStream(wx, group)
        ok, why = st.acquire()
        if ok:
            log.info(f"已盯上: {group}（{st.mode}）—— {why}")
            streams.append(st)
        else:
            log.error(f"盯 {group} 失败: {why}")
            log.error("  群名必须一字不差（空格、emoji 都算）。"
                      "跑 check_env.py 从会话列表里复制精确写法。")

    if not streams:
        log.error("一个会话都没盯上，退出。")
        input("按回车退出...")
        return

    interval = getattr(config, "POLL_INTERVAL", 1.5)
    heartbeat = getattr(config, "HEARTBEAT_INTERVAL", 300)

    log.info(f"共盯 {len(streams)} 个会话，每 {interval}s 轮询一次... (Ctrl+C 停止)")
    if config.DRY_RUN:
        log.info("提示：现在是演习模式，收到消息只会打印不会真发。")
    log.info("提示：你自己发的消息不会触发回复（attr=self 会被过滤），"
             "要让【别的号】发。")

    last_beat = time.time()
    try:
        while True:
            for st in streams:
                for m in st.poll():
                    if SEND_FAILS.muted and not config.DRY_RUN:
                        # 冷静期：照收照记，只是不发
                        t = new_trace()
                        log.warning(f"[{t}] 处于发送冷静期，本条只记录不回复: "
                                    f"{getattr(m, 'sender', '?')}: "
                                    f"{(getattr(m, 'content', '') or '')[:60]}")
                        continue
                    handle_message(m, st.chat, st.group)

            now = time.time()
            if now - last_beat >= heartbeat:
                last_beat = now
                for st in streams:
                    log.info(f"[心跳] {st.group}: 轮询 {st.poll_count} 次，"
                             f"新消息 {st.new_count} 条，"
                             f"连续读取错误 {st.consec_errors} 次，模式 {st.mode}")
                    if st.poll_count > 60 and st.new_count == 0:
                        log.warning(f"[心跳] {st.group} 一直没收到任何新消息。"
                                    f"确认：微信主窗口没被关、独立聊天窗口还在、"
                                    f"确实有【别人】在群里发言。")
            time.sleep(interval)

    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，正在停止...")
    finally:
        for st in streams:
            log.info(f"统计 {st.group}: 轮询 {st.poll_count} 次，"
                     f"处理新消息 {st.new_count} 条")
        try:
            stop = getattr(wx, "StopListening", None)
            if callable(stop):
                stop()
        except Exception:
            pass
        if DB:
            DB.close()
        log.info("已停止。")


if __name__ == "__main__":
    main()
