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
        models={"default": "gemini-2.0-flash"},
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
    wait = min(wait + 0.3, 60)
    if wait <= 0:
        return
    if verbose and wait >= 1.0:
        # 过去这里是静音的：跑得慢时看不出是在等配额，只能盯着总时长猜
        print(f"  [配额] {model} 余量 {int(have)}/{need}，等待 {wait:.1f}s")
    time.sleep(wait)
    _RL.pop(model, None)                         # 等过就作废，等下次响应头刷新


def _is_rate_limit(e) -> bool:
    return getattr(e, "status_code", None) == 429 or "RateLimit" in type(e).__name__


def _retry_after(e, default: float = 20.0) -> float:
    """从 429 里取建议等待秒数：优先 retry-after 头，其次消息里的 'try again in Xs'。"""
    try:
        ra = e.response.headers.get("retry-after")
        if ra:
            return float(ra) + 1.0
    except Exception:
        pass
    m = re.search(r"try again in ([\d.]+)s", str(e))
    if m:
        return float(m.group(1)) + 1.0
    return default


MAX_RL_RETRIES = 6  # 单次调用遇限流最多等待重试次数


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
                if usage is not None and getattr(usage, "total_tokens", None):
                    _LAST_USAGE[mdl] = usage.total_tokens
                return Result(text=text, provider=name, model=mdl, usage=usage)
            except Exception as e:
                # 限流：按建议等待后重试同一个 provider（不消耗 fallback）
                if _is_rate_limit(e) and attempt < MAX_RL_RETRIES - 1:
                    w = _retry_after(e)
                    print(f"  [限流] {name} 触发 TPM 上限，等待 {w:.0f}s 后重试 "
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
    smoke_test()
