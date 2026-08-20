"""AI 层：provider-agnostic（多后端可插拔），当前默认走 Groq。

设计：所有后端都用 OpenAI 兼容接口（openai SDK）。新增一个后端只需在 PROVIDERS
里加一条（base_url / key 环境变量 / 模型名 / 鉴权方式），无需改业务代码。
调用方只用 complete(role, ...)，由本模块按 provider_order() 依次尝试（可做 fallback）。

已内置：groq（默认）、portkey（NYU 网关，需 VPN）。
待加：gemini / cerebras / siliconflow —— 都是 OpenAI 兼容，照抄一条即可。

.env 里放对应的 key（如 GROQ_API_KEY）。绝不打印 key。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_ENV = Path(__file__).resolve().parent.parent / ".env"


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key_env: str
    # role -> 模型名。role 见 ROLES；缺失的 role 会回退到 "default"。
    models: dict
    auth: str = "bearer"    # "bearer"(Authorization) | "portkey"(x-portkey-api-key)
    direct: bool = False    # True=绕过系统代理直连（用于校内网关）；False=尊重 HTTPS_PROXY
    # 透传给 create() 的 extra_body，**按模型名分别配置**。
    # 必须按模型分：同一家的不同模型对参数的支持不一样，比如 Groq 上
    # gpt-oss 接受 reasoning_effort，llama-3.3-70b 收到它会直接返回 400。
    extra: dict = field(default_factory=dict)   # 模型名 -> extra_body
    sdk: str = "openai"     # "openai"=OpenAI 兼容接口 | "anthropic"=Anthropic 原生接口
    cn: bool = False        # True=中国大陆可直连，无需代理
    # 「当日额度耗尽」的识别模式，**按 provider 分别配**。
    # 各家 429 的措辞完全不同，用一条正则通吃必然出错：要么漏判（该 break 时
    # 不 break，几十批各重试三次白等），要么误判（把每分钟限流当成日额度耗尽，
    # 直接放弃剩下的批次）。两种都实测踩过，见 _DAILY_RE 的注释。
    # None 表示沿用默认的 Groq 模式（_DAILY_RE）。
    daily_re: object = None


# 业务角色（各 provider 可为不同角色配不同模型；缺失则回退 "default"）
ROLES = ("classify", "translate", "summarize", "events", "default")

# —— 后端注册表：加新 provider 只改这里 ——
PROVIDERS: dict[str, Provider] = {
    "groq": Provider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        # 全部任务统一用 gpt-oss-120b：口径一致、译文风格统一。
        # 曾试过把 translate/classify 分给 llama-3.3-70b（TPM 12K 更宽、更省
        # token），但译文风格不合用，遂放弃。若日后想再分，只需在这里按 role
        # 加键，并在下面 extra 里为新模型配好它自己支持的参数。
        models={"default": "openai/gpt-oss-120b"},
        auth="bearer",
        direct=False,                       # 公网 API，尊重系统代理
        # extra 按模型名配：同一 provider 下不同模型支持的参数不同，
        # 例如 reasoning_effort 只有 gpt-oss 接受，llama 收到会直接 400。
        extra={"openai/gpt-oss-120b": {"reasoning_effort": "low"}},
    ),
    "portkey": Provider(
        name="portkey",
        base_url="https://ai-gateway.apps.cloud.rt.nyu.edu/v1",
        api_key_env="PORTKEY_API_KEY",
        models={
            "default": "@vertexai/anthropic.claude-haiku-4-5@20251001",
            "summarize": "@vertexai/anthropic.claude-sonnet-4-6",
            "events": "@vertexai/anthropic.claude-sonnet-4-6",
        },
        auth="portkey",
        direct=True,                        # 校内网关，必须绕过代理直连
    ),
    # ---------------- 中国大陆可直连 ----------------
    "deepseek": Provider(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        models={"default": "deepseek-chat"},
        cn=True,
    ),
    "glm": Provider(   # 智谱 GLM
        name="glm",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_env="GLM_API_KEY",
        models={"default": "glm-4-flash", "summarize": "glm-4-plus", "events": "glm-4-plus"},
        cn=True,
    ),
    "qwen": Provider(  # 阿里通义千问（DashScope 的 OpenAI 兼容模式）
        name="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="QWEN_API_KEY",
        models={"default": "qwen-turbo", "summarize": "qwen-plus", "events": "qwen-plus"},
        cn=True,
    ),
    "minimax": Provider(
        name="minimax",
        base_url="https://api.minimaxi.com/v1",
        api_key_env="MINIMAX_API_KEY",
        models={"default": "MiniMax-Text-01"},
        cn=True,
    ),

    # ---------------- 境外（大陆通常需代理） ----------------
    "openai": Provider(   # ChatGPT
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        models={"default": "gpt-4o-mini", "summarize": "gpt-4o", "events": "gpt-4o"},
    ),
    "gemini": Provider(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        # 上下文 1,048,576 in / 65,536 out，远超本项目所需；flash-lite 档最省。
        models={"default": "gemini-3.5-flash-lite"},
        # Gemini 的配额报错措辞与 Groq 完全不同：429 RESOURCE_EXHAUSTED，
        # 配额种类写在 quota_id 里（…PerDayPerProjectPerModel… / …PerMinute…）。
        # 用默认那条 Groq 模式会漏判，于是各批处理循环不再 break，而是每批重试三次白等。
        # 只认 PerDay/per day，**不认** RESOURCE_EXHAUSTED——后者在每分钟限流里
        # 同样出现，认了会把瞬时限流当成当日耗尽而放弃整轮（_MINUTE_RE 虽已兜底，
        # 但模式本身也不该依赖兜底）。漏判的代价只是多重试几次，方向是安全的。
        daily_re=re.compile(r"PerDay|per day", re.I),
    ),
    "grok": Provider(     # xAI
        name="grok",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        models={"default": "grok-2-latest"},
    ),
    "claude": Provider(   # Anthropic 原生接口（非 OpenAI 兼容）
        name="claude",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        models={"default": "claude-haiku-4-5",
                "summarize": "claude-sonnet-5", "events": "claude-sonnet-5"},
        sdk="anthropic",
    ),
}

# 默认尝试顺序：第一个「.env 里有 key」的即为主用，其余作 fallback。
# 可用环境变量 AI_PROVIDERS 覆盖，例如 AI_PROVIDERS=deepseek,qwen
DEFAULT_ORDER = ["groq", "deepseek", "glm", "qwen", "minimax",
                 "openai", "gemini", "grok", "claude", "portkey"]


@dataclass
class Result:
    text: str
    provider: str
    model: str
    usage: object = None  # openai 的 usage 对象（prompt_tokens/completion_tokens/total_tokens）


def load_env() -> None:
    """把 .env 里的 KEY=VALUE 读进 os.environ。兼容 UTF-16(BOM)/UTF-8。"""
    if not _ENV.exists():
        return
    raw = _ENV.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig")
    else:
        text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def provider_order() -> list[str]:
    env = os.environ.get("AI_PROVIDERS")
    order = [p.strip() for p in env.split(",")] if env else DEFAULT_ORDER
    return [p for p in order if p in PROVIDERS]


def available_providers() -> list[str]:
    """按顺序返回「.env 里有对应 key」的 provider。"""
    load_env()
    return [p for p in provider_order() if os.environ.get(PROVIDERS[p].api_key_env)]


def _client(p: Provider):
    import httpx
    key = os.environ[p.api_key_env]
    # direct=True 时 trust_env=False，绕过系统代理（校内网关）；否则尊重代理（公网 API）。
    http_client = httpx.Client(trust_env=not p.direct,
                               timeout=httpx.Timeout(90.0, connect=30.0))
    if p.sdk == "anthropic":
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise RuntimeError("使用 claude 需要先装 anthropic：pip install anthropic") from e
        return Anthropic(api_key=key, base_url=p.base_url,
                         max_retries=0, http_client=http_client)
    from openai import OpenAI
    headers = {"x-portkey-api-key": key} if p.auth == "portkey" else None
    # max_retries=0：限流(429)由我们自己按 retry-after 精确等待，避免 SDK 盲目退避。
    return OpenAI(api_key=key, base_url=p.base_url, max_retries=0,
                  default_headers=headers, http_client=http_client)


def _call(client, p: Provider, role: str, system: str, user: str,
          max_tokens: int, temperature: float):
    """按 provider 的 SDK 类型发一次请求，统一返回 (文本, usage)。"""
    model = model_for(p, role)
    if p.sdk == "anthropic":
        # Anthropic 把 system 放顶层参数，不放进 messages
        r = client.messages.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            system=system, messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        return text, getattr(r, "usage", None)
    # extra_body 按模型取：同一 provider 下不同模型支持的参数不同（见 Provider.extra）
    raw = client.chat.completions.with_raw_response.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        extra_body=p.extra.get(model) or None)
    _note_headers(model, raw.headers)   # 记录该模型余量，供下次调用前动态节流
    r = raw.parse()
    return (r.choices[0].message.content or ""), getattr(r, "usage", None)


# ---------------- 动态节流 ----------------
# 过去用固定 time.sleep(1)：有余量时白等，没余量时又不够，只能靠 429 兜底。
# 改为读 OpenAI 兼容接口通用的 x-ratelimit-* 响应头：只有在本分钟剩余 token
# 确实不够下一次调用时才等，且只等到配额重置那一刻。拿不到头就不等（退化成原来
# 的行为 + 429 重试），不会因为某个 provider 不返回这些头而卡住。
# 按模型记账：不同模型的配额上限与余量是分别返回的（Groq 上 gpt-oss 是 8K TPM、
# llama-3.3-70b 是 12K），混用时若共用一份状态，阶段切换处就会拿上一个模型的
# 余量去判断下一个模型该不该等，白等或漏等。
_RL: dict[str, dict] = {}          # 模型 -> {"remaining": int, "reset": 时间戳}
_LAST_USAGE: dict[str, int] = {}   # 模型 -> 上次实际用量，作为下次的预算估计

# ---------------- 当日用量本地台账 ----------------
# Groq 免费档每个模型每天有 token 上限（gpt-oss-120b 是 20 万），但这个数字
# **不在响应头里**，只有耗尽时的 429 文案才会提到。也就是说跑之前无从判断
# 今天还剩多少。既然每次调用都会返回 usage，就在本地按天累加，做个近似台账。
# 只是近似：换机器、或在别处用同一个 key，这边都统计不到。
_USAGE_LOG = Path(__file__).resolve().parent.parent / "data" / "usage.json"


def _total_tokens(usage) -> int:
    """从各家形状不同的 usage 对象里取总 token 数。

    OpenAI 兼容接口给 total_tokens；Anthropic 给的是 input_tokens/output_tokens，
    没有 total_tokens——只认前者的话，用 Claude 的人台账会永远是空的。
    """
    if usage is None:
        return 0
    tot = getattr(usage, "total_tokens", None)
    if tot:
        return int(tot)
    a = getattr(usage, "input_tokens", 0) or 0
    b = getattr(usage, "output_tokens", 0) or 0
    return int(a) + int(b)


def _record_usage(model: str, tokens: int) -> None:
    if not tokens:
        return
    try:
        import datetime
        day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        data = {}
        if _USAGE_LOG.exists():
            data = json.loads(_USAGE_LOG.read_text(encoding="utf-8"))
        today = data.setdefault(day, {})
        today[model] = today.get(model, 0) + tokens
        # 只留最近 7 天，避免无限增长
        for old in sorted(data)[:-7]:
            data.pop(old, None)
        _USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        _USAGE_LOG.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception:
        pass                            # 台账失败绝不能影响正常调用


def usage_today() -> dict[str, int]:
    """今日（UTC）各模型的累计 token 用量。"""
    try:
        import datetime
        day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        return json.loads(_USAGE_LOG.read_text(encoding="utf-8")).get(day, {})
    except Exception:
        return {}


_DUR_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_DUR_RE = re.compile(r"([\d.]+)\s*(ms|s|m|h|d)", re.I)


def _parse_reset(v: str | None) -> float:
    """把 '690ms' / '7.66s' / '7m12s' / '1h2m' 这类时长解析成秒。

    注意 ms 必须先于 m 匹配：Groq 的 TPM 重置常常是 '690ms' 这种亚秒值，
    若按单字符扫描会把它读成 690 分钟，从而把节流变成漫长的空等。
    """
    if not v:
        return 0.0
    total = 0.0
    for num, unit in _DUR_RE.findall(str(v)):
        total += float(num) * _DUR_UNITS[unit.lower()]
    return total


def _note_headers(model: str, h) -> None:
    try:
        rem = h.get("x-ratelimit-remaining-tokens")
        if rem is None:
            return
        limit = h.get("x-ratelimit-limit-tokens")
        _RL[model] = {
            "remaining": int(rem),
            "limit": int(limit) if limit else None,
            "reset": time.time() + _parse_reset(h.get("x-ratelimit-reset-tokens")),
            "at": time.time(),
        }
    except Exception:
        pass                            # 头缺失或格式意外：当作没有余量信息


def pace(model: str, verbose: bool = True) -> None:
    """调用前按该模型的剩余配额等待。余量够就立即返回，不浪费时间。

    只等「攒够这次要用的量」，而不是等配额回满。TPM 是持续回填的令牌桶：
    reset 头给的是回满所需时间，若照它睡，缺 1000 token 也会按缺满桶来等，
    实测能白等好几倍。按 limit/60 的回填速率折算所需秒数才是对的。
    """
    st = _RL.get(model)
    if not st:
        return                                   # 还没见过这个模型，或没有余量头
    need = int(_LAST_USAGE.get(model, 1200) * 1.15)   # 留 15% 余地，避免刚好卡线
    # 桶自上次响应以来已经回填了一部分，先把这部分算进来
    limit = st["limit"]
    rate = (limit / 60.0) if limit else 0.0      # token/秒
    have = st["remaining"] + rate * (time.time() - st["at"])
    if have >= need:
        return
    wait = (need - have) / rate if rate else max(0.0, st["reset"] - time.time())
    if wait <= 0:
        # 没有 limit 也没有有效 reset（有些 provider 只回 remaining 一项）时算不出
        # 该等多久，这时**一秒都不该等**——余量的事交给 429 重试兜底。
        # 注意先判后加余量：反过来会让每次调用都白睡那点余量。
        return
    wait = min(wait + 0.3, MAX_RL_WAIT)
    if verbose and wait >= 1.0:
        # 过去这里是静音的：跑得慢时看不出是在等配额，只能盯着总时长猜
        print(f"  [配额] {model} 余量 {int(have)}/{need}，等待 {wait:.1f}s")
    time.sleep(wait)
    _RL.pop(model, None)                         # 等过就作废，等下次响应头刷新


def _is_rate_limit(e) -> bool:
    return getattr(e, "status_code", None) == 429 or "RateLimit" in type(e).__name__


# 「当日额度耗尽」与「这一分钟发太快」是两回事，必须分开处理：
# 前者要等到明天（Groq 免费档每模型每天 20 万 token），重试多少次都没用，
# 干等只会让程序卡住半小时还什么都没做；后者等几秒就恢复。
# 只认「限流类型」那一段，不能宽松地搜 per day：Groq 的 429 正文末尾常带一句
# 升级推广（…14,400 requests per day on the Dev Tier），宽松匹配会把**每分钟**
# 限流误判成当日额度耗尽，调用方于是直接放弃剩下的批次。
# 实测踩过：论文翻译因此在还剩 50 条时中止并打出「额度耗尽」，
# 但当时本机日用量才 14.6 万 / 20 万，立刻重跑就顺利跑完。
_DAILY_RE = re.compile(r"on (?:tokens|requests) per day|\((?:TPD|RPD)\)", re.I)
_USAGE_RE = re.compile(r"Limit (\d+), Used (\d+)", re.I)


# 「每分钟限流」的标记。这是**负向守卫**，优先级高于下面任何一家的日额度模式。
# 为什么需要它：两家的每分钟报错里都可能同时出现 "per day" 字样——
# Groq 尾部带升级推广语（…14,400 requests per day on the Dev Tier），
# Gemini 的 RESOURCE_EXHAUSTED 正文会列出多条配额。只按「有没有 per day」判，
# 就会把每分钟限流误判成当日耗尽，于是调用方放弃剩下所有批次。
# 反过来先认「每分钟」则不会错：带 per minute / (TPM) / PerMinute 的一定不是日额度。
_MINUTE_RE = re.compile(r"per minute|\((?:TPM|RPM)\)|PerMinute", re.I)


def _daily_re_for(provider: str | None) -> "re.Pattern":
    """某个 provider 的日额度识别模式；没单独配就用默认的 Groq 模式。"""
    p = PROVIDERS.get(provider or "")
    if p is None:
        return _DAILY_RE
    return p.daily_re or _DAILY_RE


def _is_daily_limit(e, provider: str | None = None) -> bool:
    """按 provider 的模式判断。provider=None 表示拿不到上下文，见 is_daily_limit。"""
    text = str(e)
    if _MINUTE_RE.search(text):
        return False                      # 每分钟限流：等几秒就好，绝不是日额度
    if provider is not None:
        return bool(_daily_re_for(provider).search(text))
    # 无上下文时：**任一已启用的 provider** 命中即算。
    # 只对已启用的取并集，而不是对全部 10 家取并集——后者会把没在用的家的
    # 模式也引进来，平白扩大误判面。
    return any(_daily_re_for(n).search(text)
               for n in (available_providers() or [None]))


def is_daily_limit(e) -> bool:
    """这个异常是不是「当日额度用尽」。

    给分批循环的调用方用：日额度耗尽时后面每一批都会以同样的理由失败，
    继续循环只是在刷屏和浪费时间（实测一次跑会连撞十几次）。
    与每分钟限流不同——那个等几秒就好，值得重试。

    调用方（translate/classify/summarize/paperai 的批循环）拿不到是哪个
    provider 抛的异常，所以这里对**已启用的** provider 取并集判断。
    """
    return _is_rate_limit(e) and _is_daily_limit(e)


def _daily_usage(e) -> str:
    """从 429 文案里提取「已用/总额」，让用户看得见自己烧到哪了。"""
    m = _USAGE_RE.search(str(e))
    if not m:
        return ""
    limit, used = int(m.group(1)), int(m.group(2))
    return f"（已用 {used:,}/{limit:,}，剩 {limit - used:,}）"


def _retry_after(e, default: float = 20.0) -> float:
    """从 429 里取建议等待秒数：优先 retry-after 头，其次消息里的 'try again in …'。

    时长要整段解析，不能只认「秒」：日额度耗尽时 Groq 给的是
    'try again in 14m16.656s'，若用 `([\\d.]+)s` 去匹配会直接落空，
    结果就是明明知道 14 分钟后恢复，却报不出这个数。
    """
    try:
        ra = e.response.headers.get("retry-after")
        if ra:
            return float(ra) + 1.0
    except Exception:
        pass
    m = re.search(r"try again in ([\dhms.]+)", str(e))
    if m:
        secs = _parse_reset(m.group(1))
        if secs > 0:
            return secs + 1.0
    return default


MAX_RL_RETRIES = 6   # 单次调用遇「每分钟限流」最多等待重试次数
MAX_RL_WAIT = 75.0   # 单次等待上限（秒）。每分钟配额最多 60 秒就回满，
                     # 建议值若远大于此，说明撞的其实不是分钟级限额，别傻等。


def model_for(p: Provider, role: str) -> str:
    """按角色取模型；未单独配置的角色回退到 default（再回退到任意一个）。"""
    return p.models.get(role) or p.models.get("default") or next(iter(p.models.values()))


def complete(role: str, system: str, user: str, max_tokens: int = 1024,
             temperature: float = 0.3) -> Result:
    """按 provider 顺序尝试，返回第一个成功的结果；全失败则抛最后一个异常。"""
    provs = available_providers()
    if not provs:
        raise RuntimeError(
            "没有可用的 AI provider。请在 pachong/.env 填入某个后端的 key，例如：\n"
            "GROQ_API_KEY=gsk_...\n"
            f"（当前尝试顺序 {provider_order()}，各自需要的环境变量见 ai.PROVIDERS）"
        )
    last = None
    for name in provs:
        p = PROVIDERS[name]
        client = _client(p)
        for attempt in range(MAX_RL_RETRIES):
            try:
                mdl = model_for(p, role)
                pace(mdl)   # 余量不足时等到配额重置，够就立即发
                text, usage = _call(client, p, role, system, user,
                                    max_tokens, temperature)
                tok = _total_tokens(usage)
                if tok:
                    _LAST_USAGE[mdl] = tok
                    _record_usage(mdl, tok)
                return Result(text=text, provider=name, model=mdl, usage=usage)
            except Exception as e:
                # 当日额度耗尽：等下去也不会恢复，直接换 provider 或放弃，
                # 让已完成的部分照常渲染，而不是把用户晾在那儿等半小时。
                if _is_rate_limit(e) and _is_daily_limit(e, name):
                    w = _retry_after(e, default=0.0)
                    hint = f"，约 {w / 60:.0f} 分钟后恢复" if w else ""
                    print(f"  [当日额度用尽] {name} 今日 token 配额已耗尽"
                          f"{_daily_usage(e)}{hint}。")
                    print(f"      本轮跳过该 provider；已完成的部分会照常渲染。")
                    last = e
                    break
                # 每分钟限流：等几秒就恢复，按建议等待后重试同一个 provider
                if _is_rate_limit(e) and attempt < MAX_RL_RETRIES - 1:
                    w = min(_retry_after(e), MAX_RL_WAIT)
                    print(f"  [限流] {name} 触发每分钟上限，等待 {w:.0f}s 后重试 "
                          f"({attempt + 1}/{MAX_RL_RETRIES})…")
                    time.sleep(w)
                    continue
                last = e
                print(f"[provider {name} 失败] {type(e).__name__}: {e}")
                break  # 换下一个 provider
    raise last


def parse_json(text: str):
    """从可能带 ```json 围栏或多余文字的回复里抽出 JSON 对象/数组。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.IGNORECASE).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if 0 <= i < j:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


def quota_report() -> None:
    """查当前配额余量：python -m newsagg.ai --quota

    发一次最小请求（几十 token），把 x-ratelimit-* 头打出来。
    免费档除了每分钟 TPM，还有**每天** TPD（Groq 上 gpt-oss-120b 是 20 万），
    日额度耗尽时只能等到次日，跑之前先看一眼能少走弯路。
    """
    provs = available_providers()
    if not provs:
        print("没有可用 provider（检查 .env）")
        return
    for name in provs:
        p = PROVIDERS[name]
        if p.sdk != "openai":
            print(f"  {name}: 非 OpenAI 兼容接口，跳过")
            continue
        try:
            raw = _client(p).chat.completions.with_raw_response.create(
                model=model_for(p, "default"), max_tokens=1,
                messages=[{"role": "user", "content": "."}],
                extra_body=p.extra.get(model_for(p, "default")) or None)
            h = raw.headers
            print(f"  [{name}] {model_for(p, 'default')}")
            print(f"    每分钟 token 余量 {h.get('x-ratelimit-remaining-tokens')}"
                  f"/{h.get('x-ratelimit-limit-tokens')}"
                  f"（{h.get('x-ratelimit-reset-tokens')} 后回满）")
            print(f"    请求数余量     {h.get('x-ratelimit-remaining-requests')}"
                  f"/{h.get('x-ratelimit-limit-requests')}"
                  f"（{h.get('x-ratelimit-reset-requests')} 后重置）")
        except Exception as e:
            if _is_daily_limit(e, name):
                w = _retry_after(e, default=0.0)
                print(f"  [{name}] 当日额度已耗尽{_daily_usage(e)}"
                      f"{f'，约 {w / 60:.0f} 分钟后恢复' if w else ''}")
            else:
                print(f"  [{name}] 查询失败：{type(e).__name__}: {str(e)[:100]}")

    today = usage_today()
    if today:
        print("\n  本机今日累计用量（UTC 计日，仅统计本机调用）：")
        for m, n in sorted(today.items(), key=lambda x: -x[1]):
            print(f"    {m:<26} {n:>7,} tokens")
        print("    注：每日上限不在响应头里，Groq 免费档 gpt-oss-120b 约 20 万/天，")
        print("        耗尽时才会在报错里告知。此处为本机累计的近似值。")


def smoke_test() -> None:
    """连通性自测：python -m newsagg.ai —— 用当前主用 provider 发一句最小请求。"""
    provs = available_providers()
    print("可用 provider（按顺序）:", provs or "（无——检查 .env 是否有对应 key）")
    if not provs:
        return
    try:
        r = complete("classify",
                     "You are a helpful assistant. Answer in one short word only.",
                     "Reply with exactly: ok", max_tokens=200)
        print(f"✅ 成功  provider={r.provider}  model={r.model}")
        print(f"回复: {r.text.strip()!r}")
        u = r.usage
        if u is not None:
            print(f"token 用量: prompt={getattr(u,'prompt_tokens',None)} "
                  f"completion={getattr(u,'completion_tokens',None)} "
                  f"total={getattr(u,'total_tokens',None)}")
    except Exception as e:
        print(f"❌ 失败: {type(e).__name__}: {e}")


if __name__ == "__main__":
    import sys
    if "--quota" in sys.argv:
        quota_report()
    else:
        smoke_test()
