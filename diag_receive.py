# -*- coding: utf-8 -*-
"""
收消息链路分段自检 —— 每一段单独判定 通过 / 失败 / 未验证

对应排查顺序：
    1. 能否连接并识别当前微信
    2. 能否定位目标测试会话
    3. 能否捕获一条实际新消息
    4. 消息解析和过滤是否正确

【这个脚本一条消息都不会发。】第 5~8 段（AI / 发送 / 持续监听）要用 bot.py 验，
因为那几步必然涉及真实发送。

用法：
    python diag_receive.py                # 用 config.LISTEN_GROUPS[0]
    python diag_receive.py "群名"          # 指定会话
    python diag_receive.py "群名" 120      # 第 3 段等 120 秒（默认 90）
"""

import sys
import time
import traceback

SEP = "=" * 66
RESULTS = []


def sec(title):
    print(f"\n{SEP}\n  {title}\n{SEP}")


def verdict(step, status, detail=""):
    """status: 通过 / 失败 / 未验证"""
    RESULTS.append((step, status, detail))
    mark = {"通过": "[通过]", "失败": "[失败]", "未验证": "[未验证]"}[status]
    print(f"\n  {mark} {step}")
    if detail:
        for line in str(detail).split("\n"):
            print(f"         {line}")


# ------------------------------------------------------------------
sec("0. 环境")

try:
    import config
except Exception:
    print("导入 config 失败：\n" + traceback.format_exc())
    sys.exit(1)

GROUP = sys.argv[1] if len(sys.argv) > 1 else (
    config.LISTEN_GROUPS[0] if config.LISTEN_GROUPS else "测试群")
WAIT = int(sys.argv[2]) if len(sys.argv) > 2 else 90

print(f"  目标会话 : {GROUP!r}")
print(f"  Python   : {sys.version.split()[0]}")
print(f"  可执行文件: {sys.executable}")

try:
    import wxui
except Exception as e:
    verdict("0. 适配层可用", "失败",
            f"导入 wxui 失败: {type(e).__name__}: {e}\n"
            f"先跑 python check_env.py --install 装依赖")
    sys.exit(1)

try:
    from importlib.metadata import version
    UIAVER = version("uiautomation")
except Exception:
    UIAVER = "?"
print(f"  适配层   : wxui（uiautomation {UIAVER}）")

# 微信版本 vs 适配上限
try:
    sys.path.insert(0, ".")
    import bot as botmod
    wv = botmod._detect_wechat_version()
    print(f"\n  微信客户端: {wv}")
    ceil = botmod.SUPPORTED_CEILING
    if wv and botmod._ver_tuple(wv) > ceil:
        top = ".".join(str(x) for x in ceil)
        print(f"  ！微信 {wv} 高于适配上限 {top}，这个版本没有无障碍接口，读不到内容。")
except Exception as e:
    print(f"\n  （版本探测跳过：{type(e).__name__}: {e}）")


# ------------------------------------------------------------------
sec("1. 能否连接并识别当前微信")
wx = None
try:
    wx = wxui.WeChat()
    nick = getattr(wx, "nickname", None)
    online = None
    try:
        online = wx.IsOnline()
    except Exception:
        pass
    verdict("1. 连接微信", "通过", f"昵称={nick!r}  在线={online}")
    if config.MY_NICKNAME and nick and nick != config.MY_NICKNAME:
        print(f"         ！config.MY_NICKNAME={config.MY_NICKNAME!r} 与实际昵称对不上，"
              f"@我 的判断会失效")
except Exception as e:
    verdict("1. 连接微信", "失败",
            f"{type(e).__name__}: {e}\n"
            f"依次查：微信登录了吗 / 主窗口开着吗（不能缩托盘）/ "
            f"Windows 是不是停在登录界面 / 安全软件拦没拦 UI 自动化 / 管理员权限")
    for s in ("2. 定位目标会话", "3. 捕获一条真实新消息", "4. 解析与过滤"):
        verdict(s, "未验证", "前一步失败，无法继续")
    print(f"\n{SEP}\n  自检结束（卡在第 1 步）\n{SEP}")
    sys.exit(1)

# 会话列表 —— 顺便给出一字不差的群名
try:
    sessions = wx.GetSession()
    names = [getattr(s, "name", None) for s in sessions]
    print(f"\n  当前会话列表（{len(names)} 个），群名要从这里一字不差地复制：")
    for n in names[:25]:
        flag = "   <== 目标" if n == GROUP else ""
        print(f"    {n!r}{flag}")
    if GROUP not in names:
        print(f"\n  ！{GROUP!r} 不在会话列表里。可能是名字不一字不差，"
              f"或者这个会话没在最近列表中（先在微信里点开它一次）。")
except Exception as e:
    print(f"\n  读会话列表失败: {type(e).__name__}: {e}")


# ------------------------------------------------------------------
sec("2. 能否定位目标测试会话")

# 直接用 bot.py 里的 MessageStream，不再另写一套 ——
# 自检必须走被检对象的真实路径，否则两份逻辑会一直漂移，diag 的结论就代表不了机器人的行为。
chat = None
mode = "?"
stream = None
try:
    import bot as botmod
    stream = botmod.MessageStream(wx, GROUP)
    ok, why = stream.acquire()
    chat, mode = stream.chat, stream.mode
    if ok and chat is not None:
        verdict("2. 定位目标会话", "通过", f"模式={mode}\n{why}")
    else:
        verdict("2. 定位目标会话", "失败", why or "acquire() 返回失败")
except Exception as e:
    verdict("2. 定位目标会话", "失败", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

if chat is None:
    for st in ("3. 捕获一条真实新消息", "4. 解析与过滤"):
        verdict(st, "未验证", "前一步失败，无法继续")
    print(f"\n{SEP}\n  自检结束（卡在第 2 步）\n{SEP}")
    sys.exit(1)


# ------------------------------------------------------------------
sec("3. 能否捕获一条实际新消息")

# 同样走 bot.py 的 poll()：首次 poll 只建基线（屏上历史全标已读，
# 所以重启不会重放历史），之后每次返回【自上次以来的新消息】。
# 这样连去重键 _msg_key 和基线逻辑都一并验到了。
try:
    primed = stream.poll()          # 第一次 = 建基线，正常返回空
except Exception as e:
    verdict("3. 捕获一条真实新消息", "失败",
            f"首次 poll() 抛异常: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    verdict("4. 解析与过滤", "未验证", "前一步失败")
    sys.exit(1)

print(f"  基线已建立（首次 poll 返回 {len(primed)} 条，应为 0）")
print(f"\n  >>> 现在请在「{GROUP}」里发一条消息 <<<")
print(f"      · 用【别的号】发才能验证完整链路（自己发的 attr=self，会被正确过滤）")
print(f"      · 自己发也行，能验证到「读得到 + 认得出是自己发的」")
print(f"  等待 {WAIT} 秒 ... (Ctrl+C 提前结束)\n")

captured = []
t0 = time.time()
try:
    while time.time() - t0 < WAIT:
        for m in stream.poll():
            captured.append(m)
            a, t = botmod.norm_msg(m)
            print(f"  [{int(time.time()-t0):>3}s] 捕获: "
                  f"原始 attr={getattr(m,'attr',None)!r} type={getattr(m,'type',None)!r} | "
                  f"归一化 ({a!r},{t!r}) | "
                  f"sender={getattr(m,'sender',None)!r} "
                  f"content={str(getattr(m,'content',''))[:60]!r}")
        time.sleep(getattr(config, "POLL_INTERVAL", 1.5))
except KeyboardInterrupt:
    print("\n  （手动结束等待）")

errors = getattr(stream, "consec_errors", 0)
if captured:
    verdict("3. 捕获一条真实新消息", "通过",
            f"{WAIT}s 内捕获 {len(captured)} 条（轮询 {stream.poll_count} 次，"
            f"当前连续读取错误 {errors} 次）")
else:
    verdict("3. 捕获一条真实新消息", "失败",
            f"{WAIT}s 内一条新消息都没读到"
            f"（轮询 {stream.poll_count} 次，连续读取错误 {errors} 次）。\n"
            f"确认：这段时间里确实有人在「{GROUP}」发言了；\n"
            f"      期间不要在微信里手动切换聊天；\n"
            f"      微信窗口没有被完全遮挡。")


# ------------------------------------------------------------------
sec("4. 消息解析和过滤是否正确")

if not captured:
    verdict("4. 解析与过滤", "未验证", "第 3 步没捕获到消息")
else:
    try:
        sys.path.insert(0, ".")
        import bot as botmod
        print("  对每条捕获到的消息跑一遍真实的 should_handle()：\n")
        n_ok = 0
        for m in captured:
            ok, why = botmod.should_handle(m, GROUP)
            n_ok += 1 if ok else 0
            # 原始字段 vs 归一化后的字段都打出来，方便核对。
            na, nt = botmod.norm_msg(m)
            print(f"    原始 attr={getattr(m,'attr',None)!r:>10} "
                  f"type={getattr(m,'type',None)!r:>10} | "
                  f"归一化 ({na!r},{nt!r}) | "
                  f"sender={getattr(m,'sender',None)!r:>12} -> "
                  f"{'【会回复】' if ok else '【跳过】' + why}")
        detail = (f"{len(captured)} 条里 {n_ok} 条会触发回复。\n"
                  f"判定标准（作用在【归一化后】的值上）：attr 必须是 friend"
                  f"（self=自己发的，一律不回，这条保证了机器人不会自己回自己）；"
                  f"type 必须是 text/quote。")
        verdict("4. 解析与过滤", "通过", detail)
    except Exception as e:
        verdict("4. 解析与过滤", "失败",
                f"{type(e).__name__}: {e}\n{traceback.format_exc()}")


# ------------------------------------------------------------------
sec("汇总")
for step, status, _ in RESULTS:
    print(f"  {status:<5} {step}")
print(f"\n  5. AI 请求  / 6. 进入发送逻辑 / 7. 发到正确会话 / 8. 持续监听")
print(f"     -> 这四段必然涉及真实发送，用 bot.py 验（先 DRY_RUN=True 看第 5~6 段，")
print(f"        确认无误再把 DRY_RUN 改 False 验第 7~8 段）")
print(f"\n{SEP}")
try:
    input("按回车退出...")
except (EOFError, KeyboardInterrupt):
    pass
