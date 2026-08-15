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
    extra: dict = field(default_factory=dict)  # 透传给 create() 的 extra_body（如 reasoning_effort）
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
        models={"default": "openai/gpt-oss-120b"},
        auth="bearer",
        direct=False,                       # 公网 API，尊重系统代理
        extra={"reasoning_effort": "low"},  # gpt-oss 是推理模型，低档更快省
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
    r = client.chat.completions.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        extra_body=p.extra or None)
    return (r.choices[0].message.content or ""), getattr(r, "usage", None)


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
                text, usage = _call(client, p, role, system, user,
                                    max_tokens, temperature)
                return Result(text=text, provider=name,
                              model=model_for(p, role), usage=usage)
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
