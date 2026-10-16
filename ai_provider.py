# -*- coding: utf-8 -*-
"""
大模型接入层 —— 统一走 OpenAI 兼容格式

国内主流大模型（智谱/DeepSeek/通义/Kimi/豆包/百度）都提供 OpenAI 兼容端点，
所以这里不装任何厂商 SDK，直接用 requests 打 /chat/completions。
换模型 = 改 base_url + model + api_key 三个值，代码一行不用动。

支持【主备降级】：主模型挂了/限流了，自动切下一个。
用法：主力用免费的，备用放付费的，既省钱又不断服。
"""

import json
import time
import threading
import logging
from collections import defaultdict, deque

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger("bot")


# ====================================================================
# 各家预设 —— 只填 api_key 就能用
# ====================================================================
# 注意：模型名和价格会变（DeepSeek 就改过模型名）。
# 跑 `python ai_provider.py --test` 可以实测当前 key + 模型能不能通。
PRESETS = {
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.7-flash",
        "note": "永久免费（官方标注免费文本模型），并发约 1 QPS。首选。",
        "console": "https://www.bigmodel.cn/usercenter/proj-mgmt/apikeys",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "note": "付费但很便宜，中文强。注意旧模型名 deepseek-chat 已停用。",
        "console": "https://platform.deepseek.com/api_keys",
    },
    "qwen": {
        "label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-turbo",
        "note": "约 0.3元/百万输入。新用户 100万 token 免费，90天有效。",
        "console": "https://bailian.console.aliyun.com",
    },
    "kimi": {
        "label": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "note": "约 2元/百万输入、10元/百万输出，比上面几家贵。",
        "console": "https://platform.kimi.com/console/api-keys",
    },
    "doubao": {
        "label": "字节豆包",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "",  # 豆包要在控制台建"推理接入点"，model 填 Endpoint ID
        "note": "model 要填控制台创建的接入点 ID（ep-xxxx），不是模型名。",
        "console": "https://console.volcengine.com/ark/region:cn-beijing/apiKey",
    },
    "baidu": {
        "label": "百度文心",
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-speed-8k",
        "note": "ernie-speed / ernie-lite 官方公告长期免费。",
        "console": "https://console.bce.baidu.com/qianfan/",
    },
    "custom": {
        "label": "自定义",
        "base_url": "",
        "model": "",
        "note": "任何 OpenAI 兼容端点都能填，包括本地 Ollama / vLLM。",
        "console": "",
    },
}


# ====================================================================
# 单个模型
# ====================================================================
class Provider:
    def __init__(self, key, api_key, base_url=None, model=None,
                 timeout=20, max_tokens=300, temperature=0.7):
        preset = PRESETS.get(key, PRESETS["custom"])
        self.key = key
        self.label = preset.get("label", key)
        self.base_url = (base_url or preset["base_url"]).rstrip("/")
        self.model = model or preset["model"]
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature

        # 连续失败计数，用于临时熔断
        self._fails = 0
        self._blocked_until = 0

    @property
    def available(self):
        if not self.api_key or not self.base_url or not self.model:
            return False
        return time.time() >= self._blocked_until

    # 哪些模型认 thinking 开关。智谱 GLM-4.5 起支持；
    # 别的厂商传了会 400，所以按厂商 + 模型名双重判定，宁可不传也不要报错。
    THINKING_SWITCH_PRESETS = ("zhipu",)
    THINKING_SWITCH_MODEL_HINTS = ("glm-4.5", "glm-4.6", "glm-4.7", "glm-5")

    def _supports_thinking_switch(self):
        if self.key in self.THINKING_SWITCH_PRESETS:
            return True
        m = (self.model or "").lower()
        return any(h in m for h in self.THINKING_SWITCH_MODEL_HINTS)

    def _endpoint(self):
        # 有的家 base_url 自带 /v1，有的不带，统一兼容
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def chat(self, messages):
        """返回回复文本；失败返回 None（由上层降级到下一个 provider）。"""
        if requests is None:
            log.error("没装 requests: pip install requests")
            return None

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }

        # 推理模型必须关掉「思考」，否则 max_tokens 会被思考过程吃光，
        # content 返回【空字符串】而 HTTP 仍是 200 —— 表现为「AI 调用成功但机器人不吭声」。
        # 2026-09-07 实测 glm-4.7-flash（max_tokens=64）：
        #   默认      -> finish_reason='length', content='', reasoning_tokens=64
        #   thinking  -> finish_reason='stop',   content='在的，我看了一下…', reasoning_tokens=0
        #      disabled                          且总消耗从 64 降到 14
        # 群聊要的是一两句口语化回复，不需要推理，关掉纯赚。
        if self._supports_thinking_switch():
            payload["thinking"] = {"type": "disabled"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(
                self._endpoint(), headers=headers,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=self.timeout,
            )
        except Exception as e:
            self._mark_fail(f"网络异常: {e}")
            return None

        if resp.status_code == 429:
            # 限流：熔断 60 秒，让上层切到备用模型
            self._blocked_until = time.time() + 60
            log.warning(f"[AI/{self.label}] 被限流(429)，熔断 60 秒")
            return None

        if resp.status_code != 200:
            self._mark_fail(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return None

        try:
            data = resp.json()
            choice = data["choices"][0]
            text = choice["message"].get("content")
            # 空 content 必须当失败上报，否则上层会把「空」当正常回复吞掉，
            # 日志里一片正常、群里就是不回。
            if not (text or "").strip():
                fr = choice.get("finish_reason")
                rt = ((data.get("usage") or {}).get("completion_tokens_details")
                      or {}).get("reasoning_tokens")
                self._mark_fail(
                    f"返回内容为空 (finish_reason={fr}, reasoning_tokens={rt})。"
                    f"若 finish_reason=length 且 reasoning_tokens 很大，"
                    f"说明 max_tokens 被思考过程吃光了，调大 max_tokens 或关掉 thinking")
                return None
        except Exception as e:
            self._mark_fail(f"响应解析失败: {e} | 原文: {resp.text[:200]}")
            return None

        self._fails = 0
        return (text or "").strip() or None

    def _mark_fail(self, reason):
        self._fails += 1
        log.warning(f"[AI/{self.label}] 调用失败({self._fails}): {reason}")
        if self._fails >= 3:
            self._blocked_until = time.time() + 120
            self._fails = 0
            log.warning(f"[AI/{self.label}] 连续失败，熔断 120 秒")


# ====================================================================
# 会话记忆 —— 按群隔离，只留最近几轮
# ====================================================================
class Memory:
    def __init__(self, max_turns=6):
        self.max_turns = max_turns
        self._lock = threading.Lock()
        self._store = defaultdict(lambda: deque(maxlen=max_turns * 2))

    def get(self, group):
        with self._lock:
            return list(self._store[group])

    def add(self, group, role, content):
        with self._lock:
            self._store[group].append({"role": role, "content": content})

    def clear(self, group=None):
        with self._lock:
            if group:
                self._store[group].clear()
            else:
                self._store.clear()


# ====================================================================
# 对外入口
# ====================================================================
class AIEngine:
    """
    按顺序尝试多个 provider，第一个成功的就返回。
    典型配置：主力放免费的智谱，备用放 DeepSeek。
    """

    def __init__(self, providers, system_prompt="", memory_turns=6,
                 max_input_chars=1000):
        self.providers = [p for p in providers if p.available or p.api_key]
        self.system_prompt = system_prompt
        self.memory = Memory(memory_turns)
        self.max_input_chars = max_input_chars

    @property
    def enabled(self):
        return bool(self.providers)

    def reply(self, sender, content, group, extra_context=""):
        """
        生成回复。全部 provider 都失败则返回 None，
        上层应降级到关键词规则或干脆不回。
        """
        if not self.providers:
            return None

        content = content[: self.max_input_chars]

        messages = []
        sys_text = self.system_prompt
        if extra_context:
            # 给 AI 喂后端查来的真实数据，避免它瞎编
            sys_text += f"\n\n【以下是系统查询到的真实数据，回答必须基于它，不要编造】\n{extra_context}"
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

        messages.extend(self.memory.get(group))
        messages.append({"role": "user", "content": f"{sender}: {content}"})

        for p in self.providers:
            if not p.available:
                continue
            text = p.chat(messages)
            if text:
                self.memory.add(group, "user", f"{sender}: {content}")
                self.memory.add(group, "assistant", text)
                log.info(f"[AI/{p.label}] 生成回复成功")
                return text

        log.warning("[AI] 所有模型都失败了，降级不回复")
        return None


def build_engine(cfg):
    """从配置字典构造引擎。cfg 结构见 config.py 的 AI 段。"""
    if not cfg.get("enabled"):
        return None

    providers = []
    for item in cfg.get("providers", []):
        if not item.get("api_key"):
            continue
        providers.append(Provider(
            key=item.get("preset", "custom"),
            api_key=item["api_key"],
            base_url=item.get("base_url"),
            model=item.get("model"),
            timeout=cfg.get("timeout", 20),
            max_tokens=cfg.get("max_tokens", 300),
            temperature=cfg.get("temperature", 0.7),
        ))

    if not providers:
        return None

    return AIEngine(
        providers,
        system_prompt=cfg.get("system_prompt", ""),
        memory_turns=cfg.get("memory_turns", 6),
        max_input_chars=cfg.get("max_input_chars", 1000),
    )


# ====================================================================
# 自测：python ai_provider.py
# ====================================================================
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("=" * 62)
    print("  可用的大模型预设")
    print("=" * 62)
    for k, v in PRESETS.items():
        if k == "custom":
            continue
        print(f"\n[{k}] {v['label']}")
        print(f"  模型   : {v['model'] or '(需在控制台创建接入点)'}")
        print(f"  地址   : {v['base_url']}")
        print(f"  说明   : {v['note']}")
        print(f"  拿key  : {v['console']}")

    print("\n" + "=" * 62)
    print("实测某个模型能不能通：")
    print("  python ai_provider.py zhipu 你的APIKEY")
    print("=" * 62)

    if len(sys.argv) >= 3:
        preset, key = sys.argv[1], sys.argv[2]
        model = sys.argv[3] if len(sys.argv) > 3 else None
        p = Provider(preset, key, model=model)
        print(f"\n正在测试 {p.label} / {p.model} ...")
        out = p.chat([{"role": "user", "content": "用一句话自我介绍"}])
        print(f"\n结果: {out if out else '失败，看上面的错误信息'}")
