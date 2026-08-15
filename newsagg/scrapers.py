"""逐站抓取器（Phase 7）：给 RSS 坏掉/没有/被 Google News 漏掉的站点用。

每个源注册一个函数，签名 (src: dict, hours: int) -> list[Article]。
在 sources.yaml 里把 method 设为 `scrape`，fetch.py 就会来这里找同名抓取器。
只取标题 + 链接 + 发布时间（+ 可得的简介），不抓全文。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
from dateutil import parser as dateparser

from .models import Article

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

SCRAPERS: dict = {}


def register(source_id: str):
    def deco(fn):
        SCRAPERS[source_id] = fn
        return fn
    return deco


def _client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True)


# ---- robots.txt 遵从 ----
# 逐站抓取前先看 robots.txt 是否允许。取不到 robots.txt 时按「允许」处理
# （站点没提供规则），但明确 Disallow 的路径一律不抓。
_ROBOTS: dict[str, object] = {}


def robots_allows(url: str, ua: str = UA) -> bool:
    from urllib.parse import urlsplit
    from urllib.robotparser import RobotFileParser

    parts = urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    rp = _ROBOTS.get(root)
    if rp is None:
        rp = RobotFileParser()
        try:
            with httpx.Client(headers=HEADERS, timeout=10.0,
                              follow_redirects=True) as c:
                r = c.get(root + "/robots.txt")
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])          # 拿不到就视为无限制
        _ROBOTS[root] = rp
    try:
        return rp.can_fetch(ua, url)
    except Exception:
        return True


def _get(c: httpx.Client, url: str, tries: int = 3, **kw):
    """带重试的 GET。全部失败则抛出，由调用方决定是否记录。

    抓取前先查 robots.txt；被明确禁止的路径直接放弃，不做重试。
    """
    if not robots_allows(url):
        raise PermissionError(f"robots.txt 不允许抓取：{url}")
    last = None
    for i in range(tries):
        try:
            r = c.get(url, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
    raise last


def _mk(src: dict, title: str, url: str, dt: datetime, excerpt: str = "") -> Article:
    return Article(
        source=src["id"], source_name=src["name"], region=src["region"],
        title=title.strip(), url=url, lang=src["lang"],
        published_at=dt.astimezone(timezone.utc).isoformat(),
        excerpt=excerpt.strip()[:300],
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------- 参考消息
# 站点是 SPA，但有公开的频道 JSON 接口：/json/channel/{频道}/list.json
CKXX_CHANNELS = ["yaowen", "gj", "zhongguo", "junshi", "kejiyy", "guandian"]
CKXX_BASE = "https://www.cankaoxiaoxi.com/json/channel/{}/list.json"


@register("cankaoxiaoxi")
def cankaoxiaoxi(src: dict, hours: int) -> list[Article]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out, seen, failed = [], set(), []

    # 该站单次请求要 30~50 秒，串行 6 个频道会长达数分钟，故并发拉取。
    def grab(ch: str):
        try:
            with _client(timeout=60.0) as c:
                r = _get(c, CKXX_BASE.format(ch), tries=2,
                         params={"_t": int(datetime.now().timestamp() * 1000)})
            return ch, r.json().get("list", [])
        except Exception as e:
            return ch, e

    with ThreadPoolExecutor(max_workers=len(CKXX_CHANNELS)) as ex:
        results = list(ex.map(grab, CKXX_CHANNELS))

    for ch, items in results:
        if isinstance(items, Exception):
            failed.append(f"{ch}({type(items).__name__})")
        else:
            for raw in items:
                # 结构为 {"list":[{"data":{...}}]}，文章字段在内层 data 里
                it = raw.get("data") if isinstance(raw.get("data"), dict) else raw
                url, title = (it.get("url") or "").strip(), (it.get("title") or "").strip()
                ts = it.get("publishTime") or it.get("lastpublishTime") or ""
                if not (url and title and ts) or url in seen:
                    continue
                try:  # 接口给的是北京时间，无时区标记
                    dt = dateparser.parse(ts).replace(tzinfo=timezone(timedelta(hours=8)))
                except (ValueError, OverflowError):
                    continue
                if dt < cutoff:
                    continue
                seen.add(url)
                out.append(_mk(src, title, url, dt, it.get("description") or ""))
    if failed:
        print(f"    [参考消息] {len(failed)}/{len(CKXX_CHANNELS)} 个频道拉取失败：{', '.join(failed)}")
    return out


# ---------------------------------------------------------------- 联合早报
# 列表页是服务端渲染的：标题在 <a aria-label="...">，链接形如 /news/china/story20260815-xxxx。
# 列表页不带具体时刻，故对候选文章各取一次页面里的 datePublished。
ZAOBAO_LISTS = [
    "https://www.zaobao.com.sg/realtime/china",
    "https://www.zaobao.com.sg/realtime/world",
    "https://www.zaobao.com.sg/realtime/finance",
]
ZB_LINK = re.compile(r'aria-label="([^"]{6,120})"[^>]*href="(/news/[a-z]+/story(\d{8})-\d+)"')
ZB_TIME = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')
ZB_MAX = 60  # 单次最多解析的文章数，避免请求过多


@register("zaobao")
def zaobao(src: dict, hours: int) -> list[Article]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    # 先按 URL 里的日期粗筛（窗口最多跨两天）
    valid_days = {(now - timedelta(days=d)).strftime("%Y%m%d") for d in range(0, 3)}

    cand: dict[str, str] = {}   # url -> title
    with _client() as c:
        for lu in ZAOBAO_LISTS:
            try:
                html = c.get(lu).text
            except Exception:
                continue
            for title, path, day in ZB_LINK.findall(html):
                if day in valid_days:
                    cand.setdefault("https://www.zaobao.com.sg" + path, title)

    errors: list[str] = []

    def one(item):
        url, title = item
        try:
            with _client() as c:
                m = ZB_TIME.search(c.get(url).text)
            if not m:
                errors.append("无 datePublished")
                return None
            dt = dateparser.parse(m.group(1))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return _mk(src, title, url, dt) if dt >= cutoff else None
        except Exception as e:
            errors.append(type(e).__name__)
            return None

    # 并发别开太大，避免被站点限流
    with ThreadPoolExecutor(max_workers=4) as ex:
        out = [a for a in ex.map(one, list(cand.items())[:ZB_MAX]) if a]
    if not out and errors:
        from collections import Counter
        print(f"    [zaobao] {len(cand)} 个候选全部失败：{dict(Counter(errors))}")
    return out


# ---------------------------------------------------------------- 南方周末
# 无 RSS，Google News 也只收到「关于我们」这类栏目页，故直接解析列表页。
# 列表页不带时间，时间在文章页的 <span class="nfzm-content__publish" data-time="...">。
NFZM_BASE = "https://www.infzm.com"
NFZM_LISTS = [
    "https://www.infzm.com/",
    "https://www.infzm.com/topics/t995.html",   # 快讯
    "https://www.infzm.com/topics/t998.html",   # 推荐
    "https://www.infzm.com/topics/t1012.html",  # 动静
    "https://www.infzm.com/topics/t202.html",   # 深度
]
NFZM_TIME = re.compile(r'class="nfzm-content__publish"\s+data-time="([^"]+)"')
NFZM_MAX = 80          # 单次最多核对的文章数
CN_TZ = timezone(timedelta(hours=8))


@register("nanfangzhoumo")
def nanfangzhoumo(src: dict, hours: int) -> list[Article]:
    from bs4 import BeautifulSoup

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cand: dict[str, str] = {}   # url -> title

    with _client() as c:
        for lu in NFZM_LISTS:
            try:
                soup = BeautifulSoup(_get(c, lu).text, "html.parser")
            except Exception:
                continue
            for a in soup.select('a[href*="/contents/"]'):
                href = a.get("href", "")
                m = re.match(r"^(/contents/\d+)", href)
                if not m:
                    continue
                # 标题在 *__title 子元素里；整段 a.get_text() 会把摘要也粘进来
                el = a.select_one('[class*="__title"]') or a.find(["h1", "h2", "h3", "h4"])
                title = re.sub(r"\s+", " ", (el or a).get_text(strip=True)).strip()
                if len(title) < 6:
                    continue
                cand.setdefault(NFZM_BASE + m.group(1), title)

    errors: list[str] = []

    def one(item):
        url, title = item
        try:
            with _client() as c:
                m = NFZM_TIME.search(_get(c, url).text)
            if not m:
                errors.append("无 data-time")
                return None
            dt = dateparser.parse(m.group(1))
            if dt.tzinfo is None:       # 站点给的是北京时间
                dt = dt.replace(tzinfo=CN_TZ)
            return _mk(src, title, url, dt) if dt >= cutoff else None
        except Exception as e:
            errors.append(type(e).__name__)
            return None

    with ThreadPoolExecutor(max_workers=5) as ex:
        out = [a for a in ex.map(one, list(cand.items())[:NFZM_MAX]) if a]
    if not out and errors:
        from collections import Counter
        print(f"    [nanfangzhoumo] {len(cand)} 个候选全部失败：{dict(Counter(errors))}")
    return out
