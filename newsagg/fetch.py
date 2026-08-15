"""抓取：RSS 优先，Google News RSS 兜底。只取 24h 内标题+节选+链接。"""
from __future__ import annotations

import calendar
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from . import db
from .models import Article

CONFIG = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"
UA = "Mozilla/5.0 (personal-news-aggregator; RSS reader)"


def load_sources() -> list[dict]:
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)["sources"]


def build_gnews_url(query: str, locale: dict) -> str:
    q = urllib.parse.quote(query)
    return (f"https://news.google.com/rss/search?q={q}"
            f"&hl={locale['hl']}&gl={locale['gl']}&ceid={locale['ceid']}")


def clean_excerpt(raw: str, limit: int = 200) -> str:
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return text[:limit].strip()


def entry_datetime(entry) -> datetime | None:
    """返回 UTC datetime；解析失败返回 None。"""
    if getattr(entry, "published_parsed", None):
        return datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=timezone.utc)
    for key in ("published", "updated", "pubDate"):
        val = entry.get(key) if hasattr(entry, "get") else None
        if val:
            try:
                dt = dateparser.parse(val)
                return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                continue
    return None


def clean_title(raw: str, source_name: str) -> str:
    """去掉 Google News 追加的「 - 媒体名」后缀。

    有些条目 Google 抽不出标题，只剩下 '- 联合早报' 这样的空壳，
    去后缀后为空即视为脏数据，由调用方丢弃。
    """
    t = (raw or "").strip()
    t = re.sub(r"\s*[-–—|]\s*[^-–—|]{1,30}$", "", t).strip() if t.startswith("-") else t
    return t.lstrip("-–—|").strip()


def fetch_source(src: dict, hours: int = 24) -> list[Article]:
    if src["method"] == "scrape":
        from .scrapers import SCRAPERS
        fn = SCRAPERS.get(src["id"])
        if not fn:
            raise RuntimeError(f"未注册抓取器：{src['id']}（见 newsagg/scrapers.py）")
        return fn(src, hours)

    if src["method"] == "gnews":
        urls = [build_gnews_url(src["query"], src["gnews_locale"])]
    else:
        urls = src["feeds"]

    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    now_iso = datetime.now(timezone.utc).isoformat()
    out: list[Article] = []
    seen: set[str] = set()

    for url in urls:
        feed = feedparser.parse(url, agent=UA)
        for e in feed.entries:
            link = e.get("link", "").strip()
            title = clean_title(e.get("title", ""), src["name"])
            # 标题为空的条目是 Google News 的脏数据，丢弃
            if not link or not title or link in seen:
                continue
            dt = entry_datetime(e)
            if dt is None or dt.timestamp() < cutoff:
                continue
            seen.add(link)
            out.append(Article(
                source=src["id"],
                source_name=src["name"],
                region=src["region"],
                title=title,
                url=link,
                lang=src["lang"],
                published_at=dt.isoformat(),
                excerpt=clean_excerpt(e.get("summary", "")),
                fetched_at=now_iso,
            ))
    return out


# 境外源多数托管在这些域名上；用它们探测「境外网络是否可达」
PROBE_URLS = ["https://news.google.com/", "https://feeds.bbci.co.uk/news/world/rss.xml"]


def probe_overseas(timeout: float = 10.0) -> bool:
    """探测能否连到境外站点。

    用途是**优雅降级**：中国大陆用户不挂代理时，境外源会长时间超时，
    与其让整轮抓取卡住几分钟再报一堆错，不如提前跳过、只抓国内源。
    这里不做任何绕过，只是判断「连得上吗」。

    必须用 httpx（trust_env=True）而不是裸 socket：抓取本身走的就是
    httpx/feedparser，会遵循系统代理；裸 socket 不走代理，对已挂代理的
    用户会误判成「没有外网」，把境外源全跳过。

    trust_env 经 urllib.getproxies() 解析，Windows 上会回落读注册表里的
    系统代理，因此 Clash/v2rayN 这类「设置系统代理」的客户端也能被识别。
    超时给到 10 秒是留给高峰期较慢的代理；真的连不上时才会等满。
    误判仍有可能（例如代理极慢），此时用 run.py --all 强制抓取。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import httpx

    def hit(url: str) -> bool:
        try:
            with httpx.Client(trust_env=True, timeout=timeout,
                              follow_redirects=True,
                              headers={"User-Agent": UA}) as c:
                return c.get(url).status_code < 500
        except Exception:
            return False

    # 并发探测：连得上时任一成功即返回；连不上时总耗时也只是单个超时，
    # 而不是各 URL 超时之和——这段等待正好落在「没有代理的国内用户」身上，
    # 串行会让他们每次都白等一倍时间。
    with ThreadPoolExecutor(max_workers=len(PROBE_URLS)) as ex:
        futs = [ex.submit(hit, u) for u in PROBE_URLS]
        for f in as_completed(futs):
            if f.result():
                return True
    return False


def run(hours: int = 24, cn_only: bool = False, skip_probe: bool = False) -> None:
    db.init()
    sources = load_sources()

    # 决定是否抓境外源
    if cn_only:
        overseas_ok = False
        print("[--cn-only] 只抓国内源")
    elif skip_probe:
        overseas_ok = True
    else:
        overseas_ok = probe_overseas()
        if not overseas_ok:
            print("[网络] 连不上境外站点，本轮只抓国内源。")
            print("       若你在中国大陆且未使用代理，这是预期行为；"
                  "境外媒体需要能访问外网才能抓取。")
            print("       如果你确定自己能上外网（探测可能因代理较慢而误判），")
            print("       用 python run.py --all 跳过探测、强制抓取全部源。")

    if not overseas_ok:
        skipped = [s for s in sources if s["region"] != "china"]
        sources = [s for s in sources if s["region"] == "china"]
        if skipped:
            print(f"       跳过 {len(skipped)} 个境外源："
                  f"{'、'.join(s['name'] for s in skipped[:6])}"
                  f"{' 等' if len(skipped) > 6 else ''}")

    total_new = 0
    print(f"抓取窗口：过去 {hours} 小时\n" + "-" * 48)
    for src in sources:
        try:
            articles = fetch_source(src, hours)
            new = db.upsert_articles(articles)
            total_new += new
            status = "OK " if articles else "空  "
            print(f"[{status}] {src['id']:<9} {src['name']:<8} "
                  f"取到 {len(articles):>3} 条，新增 {new:>3}  ({src['method']})")
        except Exception as ex:  # 单源失败不阻塞整体
            print(f"[ERR] {src['id']:<9} {src['name']:<8} 失败: {ex}")
    print("-" * 48)
    print(f"总新增 {total_new} 条 -> {db.DB_PATH}")


if __name__ == "__main__":
    hrs = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    run(hrs)
