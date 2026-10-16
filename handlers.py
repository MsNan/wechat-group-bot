# -*- coding: utf-8 -*-
"""
业务指令处理器 —— 需要查库 / 调后端接口 / 写库的消息走这里

这是整条决策链里【优先级最高】的一层，排在 AI 前面。原因很简单：
订单号、余额、库存这类数据必须来自数据库，不能让 AI 猜。
AI 只负责闲聊兜底，碰不到真实数据。

═══ 一个 handler 长什么样 ═══

    def handle_xxx(ctx):
        # ctx.args    -> 正则捕获组，比如 ("12345",)
        # ctx.sender  -> 发送者昵称
        # ctx.content -> 完整消息原文
        # ctx.group   -> 群名
        # ctx.db      -> 数据库对象（可能是 None）
        # ctx.api     -> 后端接口对象（可能是 None）
        return "要回复的话"        # 直接回复
        return None                # 不回复
        return AIAnswer(data)      # 把真实数据交给 AI 组织语言再回

═══ 加自己的指令 ═══
    1. 写一个 handle_xxx(ctx) 函数
    2. 在最下面的 HANDLERS 列表里加一条 {"name":..., "pattern":..., "func":...}
    pattern 是正则，用 () 捕获参数。
"""

import re
import logging

log = logging.getLogger("bot")


# ====================================================================
# 上下文 & 返回类型
# ====================================================================
class Context:
    def __init__(self, sender, content, group, args, api=None, db=None, store=None):
        self.sender = sender
        self.content = content
        self.group = group
        self.args = args          # 正则捕获组，元组
        self.api = api
        self.db = db
        self.store = store


class AIAnswer:
    """
    handler 返回这个，表示"我查到了真实数据，但让 AI 用人话组织一下再回"。
    AI 会被强制要求只能基于 data 回答，不许编造。
    """

    def __init__(self, data, hint=""):
        self.data = data
        self.hint = hint

    def as_context(self):
        if isinstance(self.data, (list, dict)):
            import json
            body = json.dumps(self.data, ensure_ascii=False, indent=2, default=str)
        else:
            body = str(self.data)
        return f"{self.hint}\n{body}".strip()


# ====================================================================
# 示例 1：查订单 —— 直连数据库
# 群里发：  查订单 202608270001
# ====================================================================
def handle_query_order(ctx):
    order_no = ctx.args[0]

    if not (ctx.db and ctx.db.enabled):
        return "数据库没配好，查不了订单。"

    # 注意：order_no 是【参数】传进去的，不是拼进 SQL 的。防注入。
    rows = ctx.db.query(
        "SELECT order_no, customer_name, amount, status, created_at "
        "FROM orders WHERE order_no = %s LIMIT 1",
        (order_no,),
    )

    if rows is None:
        return "查询出错了，稍后再试。"
    if not rows:
        return f"没找到订单 {order_no}"

    r = rows[0]
    return (
        f"订单 {r['order_no']}\n"
        f"客户：{r.get('customer_name', '-')}\n"
        f"金额：{r.get('amount', '-')}\n"
        f"状态：{r.get('status', '-')}\n"
        f"下单：{r.get('created_at', '-')}"
    )


# ====================================================================
# 示例 2：查库存 —— 走后端 HTTP 接口（推荐方式）
# 群里发：  查库存 A100
# ====================================================================
def handle_query_stock(ctx):
    sku = ctx.args[0]

    if not (ctx.api and ctx.api.enabled):
        return "后端接口没配好。"

    data = ctx.api.get("/api/stock/query", params={"sku": sku})
    if data is None:
        return "后端没响应，稍后再试。"

    # 按你后端的返回结构改这里
    if data.get("code") != 0:
        return f"查询失败：{data.get('msg', '未知错误')}"

    item = data.get("data") or {}
    if not item:
        return f"没查到 {sku} 的库存"

    return f"{sku} 当前库存：{item.get('qty', '-')}（仓库：{item.get('warehouse', '-')}）"


# ====================================================================
# 示例 3：查到数据后交给 AI 用人话说 —— 演示 AIAnswer
# 群里发：  今天销售
# ====================================================================
def handle_today_sales(ctx):
    if not (ctx.db and ctx.db.enabled):
        return None

    rows = ctx.db.query(
        "SELECT COUNT(*) AS cnt, SUM(amount) AS total "
        "FROM orders WHERE DATE(created_at) = CURDATE()"
    )
    if not rows:
        return "今天还没有订单。"

    return AIAnswer(rows[0], hint="这是今天的销售统计数据，用一句话自然地播报给群里：")


# ====================================================================
# 示例 4：写库 —— 群里记一笔
# 群里发：  记一笔 客户张三要加急
# ====================================================================
def handle_note(ctx):
    text = ctx.args[0].strip()
    if not text:
        return None

    # 优先走后端接口，其次直连数据库
    if ctx.api and ctx.api.enabled:
        data = ctx.api.post("/api/note/add", {
            "content": text, "sender": ctx.sender, "group": ctx.group,
        })
        if data and data.get("code") == 0:
            return f"记下了：{text}"
        return "记录失败，后端没收到。"

    if ctx.db and ctx.db.enabled:
        if not ctx.db.allow_write:
            return "没开写库权限，记不了。"
        n = ctx.db.execute(
            "INSERT INTO group_notes (group_name, sender, content) VALUES (%s, %s, %s)",
            (ctx.group, ctx.sender, text),
        )
        return f"记下了：{text}" if n else "写库失败。"

    return "没配后端也没配数据库，记不了。"


# ====================================================================
# 示例 5：指令列表
# ====================================================================
def handle_commands(ctx):
    lines = ["可用指令："]
    for h in HANDLERS:
        if h.get("hidden"):
            continue
        lines.append(f"· {h['usage']}  —— {h['name']}")
    return "\n".join(lines)


# ====================================================================
# 指令注册表  —— 从上往下匹配，命中第一条就停
# ====================================================================
HANDLERS = [
    {
        "name": "查订单",
        "usage": "查订单 <订单号>",
        "pattern": r"^查订单\s*([A-Za-z0-9_-]+)",
        "func": handle_query_order,
    },
    {
        "name": "查库存",
        "usage": "查库存 <编号>",
        "pattern": r"^查库存\s*([A-Za-z0-9_-]+)",
        "func": handle_query_stock,
    },
    {
        "name": "今日销售",
        "usage": "今天销售",
        "pattern": r"^今天?销售",
        "func": handle_today_sales,
    },
    {
        "name": "记录备注",
        "usage": "记一笔 <内容>",
        "pattern": r"^记一笔\s*(.+)",
        "func": handle_note,
    },
    {
        "name": "指令列表",
        "usage": "指令",
        "pattern": r"^(指令|命令列表|帮助菜单)$",
        "func": handle_commands,
        "hidden": True,
    },
]

# 预编译，省得每条消息都编译一遍
_COMPILED = [(re.compile(h["pattern"]), h) for h in HANDLERS]


def route(sender, content, group, api=None, db=None, store=None):
    """
    尝试把消息路由到某个业务 handler。
    返回：
        None                -> 没有指令命中，交给下一层（关键词规则 / AI）
        str                 -> 直接回复这段文本
        AIAnswer            -> 查到了数据，让 AI 组织语言
    """
    text = (content or "").strip()
    if not text:
        return None

    for pattern, h in _COMPILED:
        m = pattern.match(text)
        if not m:
            continue

        log.info(f"  -> 命中业务指令: {h['name']}")
        ctx = Context(sender, content, group, m.groups(), api=api, db=db, store=store)
        try:
            return h["func"](ctx)
        except Exception as e:
            log.exception(f"  -> 指令 {h['name']} 执行出错: {e}")
            return "处理这条指令时出错了，我记到日志里了。"

    return None


# ====================================================================
# 自测：python handlers.py
# ====================================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("=" * 62)
    print("  已注册的业务指令")
    print("=" * 62)
    for h in HANDLERS:
        print(f"\n{h['name']}")
        print(f"  用法  : {h['usage']}")
        print(f"  正则  : {h['pattern']}")

    print("\n" + "=" * 62)
    print("  正则匹配测试（不连数据库，只验证能不能路由到）")
    print("=" * 62)
    for t in ["查订单 202608270001", "查库存 A100", "今天销售",
              "记一笔 客户张三要加急", "指令", "随便聊两句"]:
        hit = None
        for pattern, h in _COMPILED:
            m = pattern.match(t)
            if m:
                hit = f"{h['name']}  参数={m.groups()}"
                break
        print(f"  {t!r:28s} -> {hit or '无命中（会交给 AI）'}")
