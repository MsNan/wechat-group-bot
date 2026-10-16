# -*- coding: utf-8 -*-
"""
微信群机器人 —— 配置

两种改配置的方式，二选一：

  【方式一】直接改这个文件（源码运行时用这个，最方便）

  【方式二】改同目录下的 settings.json（打包成 exe 后用这个）
            settings.json 里写了什么，就覆盖这里的什么，没写的沿用这里的默认值。
            模板见 settings.example.json，复制成 settings.json 再改。

打包成 exe 之后代码就改不动了，所以所有需要现场调的东西都必须能从
settings.json 覆盖 —— 这就是方式二存在的原因。
"""

import os
import sys
import json


# ====================================================================
# 一、安全开关（第一次跑请务必保持 True）
# ====================================================================

# True  = 演习模式：只把"我打算回复什么"打到日志，【一条都不真发】
# False = 真回复
DRY_RUN = True


# ====================================================================
# 二、监听哪些群
# ====================================================================

# 群名必须【一字不差】，包括空格和 emoji。
# 精确写法从 check_env.py 输出的"会话列表"里复制。
LISTEN_GROUPS = [
    "测试群",
]

# 你自己的微信昵称（用于判断有没有人 @ 我）。不用可留空。
# 从 bot.py 启动日志「已连接微信: xxx」那行能看到。
MY_NICKNAME = "tao"


# ====================================================================
# 三、什么情况下才回复
# ====================================================================

ONLY_REPLY_WHEN_AT_ME = False    # True = 只有被 @ 才回
SENDER_WHITELIST = []            # 只回这些人，留空=不限
SENDER_BLACKLIST = []            # 这些人一律不回，优先级最高
REPLY_WITH_AT = True             # 回复时 @ 回发送者
REPLY_AS_QUOTE = True            # 用引用回复


# ====================================================================
# 四、回复决策链  —— 从上往下，命中即停
# ====================================================================
#
#   1. 业务指令 (handlers.py)  查库 / 调后端接口 / 写库    ← 优先级最高
#   2. 关键词规则 (下面 RULES)  固定话术，零成本零延迟
#   3. AI 兜底   (ai_provider)  前两层都没命中才轮到它
#
# 为什么业务指令必须排 AI 前面：订单号、库存、余额这类数据只能来自数据库，
# 让 AI 去答它就会编。AI 只管闲聊。

# --- 第 2 层：关键词规则 ---
# keywords: 命中任一即可（不区分大小写）
# reply   : 支持 {sender} {group} 占位符
# mode    : contains(包含) / exact(整条完全相等)
RULES = [
    {
        "keywords": ["在吗", "在不在", "有人吗"],
        "reply": "在的 {sender}，有什么事你说～",
        "mode": "contains",
    },
    {
        "keywords": ["帮助", "help", "菜单"],
        "reply": "发「指令」可以看我能干什么。",
        "mode": "exact",
    },
]

# 三层全都没命中时回什么。None = 不回（推荐）
FALLBACK_REPLY = None


# ====================================================================
# 五、AI 智能回复（第 3 层）
# ====================================================================
#
# 国内大模型基本都兼容 OpenAI 的接口格式，所以换厂商只改三个值：
# preset / model / api_key。
#
# providers 是【按顺序降级】的：第一个失败或被限流，自动切第二个。
# 推荐配法：主力放免费的智谱，备用放付费的 DeepSeek，既省钱又不断服。
#
# 各家对比跑：python ai_provider.py
# 实测能不能通：python ai_provider.py zhipu 你的KEY

# 【密钥怎么放】不要把 api_key 写进这个文件 —— 它是要进版本库的。
# 复制 settings.example.json 成 settings.json（已在 .gitignore 里），在那边写：
#     {"AI": {"enabled": true,
#             "providers": [{"preset": "zhipu", "api_key": "你的key",
#                            "model": "glm-4-flash-250414"}]}}
# settings.json 里写了什么就覆盖这里的什么，没写的沿用默认值。
AI = {
    "enabled": False,              # 填好 key 后改 True；【key 请写 settings.json，别写这个文件】

    "providers": [
        {
            # 【主力】智谱 GLM-4-Flash：永久免费，非推理模型，群聊短回复够用。
            # 2026-09-07 实测：HTTP 200 / 0.24s / 4 个 completion token 出结果。
            # 为什么不用更新的 glm-4.7-flash 当主力 —— 见下面那条。
            "preset": "zhipu",
            "api_key": "",   # ← 不要写在这里！放 settings.json，见下方说明
            "model": "glm-4-flash-250414",
        },
        {
            # 【备用】智谱 GLM-4.7-Flash：同一把 key，更强，但 2026-09-07 实测
            # 持续返回 429 / code 1305「该模型当前访问量过大」——
            # 注意这【不是】我们账号超速（那个是 1302），是免费池被全网挤爆，
            # 跟调用频率无关，requests 和 curl 结果一致。所以降级为备用，
            # 主力挂了由 AIEngine 自动顶上（它按顺序试，第一个成功就返回）。
            # 它是推理模型，必须带 thinking:disabled，否则 max_tokens 会被
            # 思考过程吃光、content 返回空 —— ai_provider 已按厂商自动加。
            "preset": "zhipu",
            "api_key": "",   # ← 不要写在这里！放 settings.json，见下方说明
            "model": "glm-4.7-flash",
        },
        # 还能加 qwen / kimi / doubao / baidu / custom，见 ai_provider.PRESETS
    ],

    # AI 的人设。这段直接决定回复风格，值得好好写。
    "system_prompt": (
        "你是一个微信群里的助手机器人。回复要求：\n"
        "1. 简短，一般不超过两句话，群聊里没人看长篇大论\n"
        "2. 语气自然口语化，像群友说话，不要用书面语\n"
        "3. 不知道的事直接说不知道，绝对不要编造\n"
        "4. 不要用 markdown 格式，微信里显示不出来\n"
        "5. 涉及订单号、金额、库存这类具体数据，如果没有系统给你的真实数据，"
        "就让对方发对应的查询指令，不要自己猜"
    ),

    "memory_turns": 6,        # 每个群记住最近几轮对话（按群隔离）
    "max_tokens": 300,        # 回复长度上限，也是省钱的关键
    "temperature": 0.7,
    "timeout": 20,            # 秒。群聊场景超过这个就没意义了
    "max_input_chars": 1000,  # 单条消息截断长度，防超长文本烧 token

    # 什么消息才交给 AI（省钱 + 防刷）
    "only_when_at_me": True,  # 只有 @ 我才用 AI（强烈建议 True，不然群里每句话都烧 token）
    "min_length": 2,          # 消息太短不值得调 AI
}


# ====================================================================
# 六、后端 / 数据库对接
# ====================================================================
#
# 两种模式，可以只开一个，也可以都开：
#
#   http  —— 机器人调你后端的 REST 接口，后端去查库。【推荐】
#            好处：数据库密码不落在这台 Windows 上，业务逻辑改起来方便
#
#   mysql —— 机器人直连数据库
#            好处：不用后端配合。坏处：密码要存在这台机器上
#
# 具体查什么、怎么查，写在 handlers.py 里（已有 4 个示例照着改）
# 连通性自测：python backend.py

BACKEND = {
    "http": {
        "enabled": False,
        "base_url": "https://your-backend.com",   # 不要带结尾斜杠
        "token": "",                              # 接口鉴权 token
        "token_header": "Authorization",          # 有的后端用 X-Token
        "token_prefix": "Bearer ",                # 有的后端不要前缀，填 ""
        "headers": {},                            # 额外的固定请求头
        "timeout": 10,
        "retries": 2,
        "health_path": "/api/health",             # 自测用的探活接口
    },

    "mysql": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "",
        "database": "",
        "timeout": 10,

        # 写操作开关。默认关闭。
        # 想清楚再开：这个机器人是被群消息触发的，开了写权限
        # 就意味着群里的人能通过发消息改你的数据库。
        # （DELETE / DROP / TRUNCATE / ALTER 代码里已硬性禁止，开了也不行）
        "allow_write": False,
    },

    # 把收到的群消息存下来
    "save_messages": {
        "enabled": False,
        "api_path": "/api/message/save",   # 走 http 模式时用这个接口
        # 走 mysql 模式会自动建表，表结构见 backend.py 的 CREATE_TABLE_SQL
    },
}


# ====================================================================
# 七、防封号 / 防刷屏  —— 别删这一段
# ====================================================================
# 微信风控最敏感的就是「秒回 + 高频 + 复读机」。下面几项是为了让机器人
# 看起来像个人。数值能调，但不建议全关。

REPLY_DELAY_RANGE = (2.0, 5.0)   # 回复前随机等几秒。秒回是最明显的机器特征
PER_SENDER_COOLDOWN = 30         # 同一个人多少秒内不重复回
PER_GROUP_COOLDOWN = 5           # 同一个群的回复最小间隔
MAX_REPLIES_PER_HOUR = 40        # 每小时全局上限，超了停到下小时
MAX_REPLY_LENGTH = 500           # 单条回复长度上限，防规则写错刷屏

# 消息去重的有效期（秒）。
# 主去重是按【消息 id】做的（见 bot.py 的 MessageStream），这一条是第二道保险，
# 防的是同一条消息被读到两次却拿到不同 id。
# 有效期不能设成永久 —— 否则同一个人今天说「在吗」、明天再说「在吗」，
# 第二次就永远收不到回复了。
# 调试期可以设成 5，方便反复发同一句话测试。
# 内容指纹去重的有效期（秒）。0 = 关闭。
#
# 【应当保持 0】：适配层已按序列对齐给每条消息
# 分配稳定 id，同一条消息多轮读到 id 不变，而同一个人连发两条一样的话
# 会拿到两个不同 id —— 已经是正确的去重。再叠一层内容指纹只会误杀
# 合法的重复发言（实测：连发两条「在吗」，第二条被吞）。
DEDUP_WINDOW = 0


# ====================================================================
# 七之二、收消息与发送确认
# ====================================================================
#
# 每 POLL_INTERVAL 秒读一次当前会话的可见消息，和上一轮对齐算出新增。
# 详见 bot.py 顶部注释。

POLL_INTERVAL = 1.5        # 轮询间隔（秒）。太小会增加 UI 读取负担，1~3 合适

# 发完之后回读聊天窗口，确认这条消息真的出现在界面里。
# 关掉的话，日志就只能报告「已调用发送方法」，不能报告「界面确认已发送」——
# 这两件事不是一回事，不能因为没抛异常就当成功。
VERIFY_SENT_IN_UI = True
VERIFY_SENT_TIMEOUT = 6    # 回读确认最多等几秒

# 连续发送失败保护：不做无限重试。
# 连续失败到阈值就进冷静期，期间照收照记但不发，避免在微信异常/被风控时反复戳。
MAX_CONSECUTIVE_SEND_FAILURES = 5
SEND_FAIL_COOLDOWN = 300   # 冷静期（秒）

HEARTBEAT_INTERVAL = 300   # 每隔多久打一条心跳（轮询次数/收到条数/错误数）


# ====================================================================
# 八、日志
# ====================================================================

MESSAGE_LOG = "logs/messages.log"   # 收到的每条消息
RUN_LOG = "logs/bot.log"            # 运行日志
PRINT_TO_CONSOLE = True


# ====================================================================
# ↓↓↓ 以下是加载逻辑，一般不用动 ↓↓↓
# ====================================================================

def app_dir():
    """
    程序所在目录。
    打包成 exe 后 __file__ 指向临时解压目录，必须用 sys.executable 的目录，
    否则找不到用户放在 exe 旁边的 settings.json。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


SETTINGS_FILE = os.path.join(app_dir(), "settings.json")


def _deep_merge(base, override):
    """把 override 合并进 base，嵌套 dict 逐层合并而不是整个替换。"""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_settings():
    """
    读 settings.json 覆盖上面的默认值。
    文件不存在就静默跳过（源码运行时的正常情况）。
    """
    if not os.path.exists(SETTINGS_FILE):
        return False

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[!] settings.json 解析失败，将使用 config.py 的默认值: {e}")
        return False

    module = sys.modules[__name__]
    for key, value in data.items():
        if key.startswith("_"):
            continue
        current = getattr(module, key, None)
        if isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(current, value)
        else:
            # JSON 没有元组，REPLY_DELAY_RANGE 这种要转回去
            if isinstance(current, tuple) and isinstance(value, list):
                value = tuple(value)
            setattr(module, key, value)

    return True


LOADED_FROM_JSON = load_settings()


def log_path(name):
    """日志用绝对路径，避免双击 exe 时工作目录乱跑导致日志写到别处。"""
    if os.path.isabs(name):
        return name
    return os.path.join(app_dir(), name)
