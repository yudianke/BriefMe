"""抓取：RSS 优先，Google News RSS 兜底。只取 24h 内标题+节选+链接。"""
from __future__ import annotations

import calendar
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from . import db
from .models import SOURCE_WINDOW_HOURS, Article

CONFIG = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"
UA = "Mozilla/5.0 (personal-news-aggregator; RSS reader)"
FETCH_WORKERS = 6   # 并发抓取的源数；纯网络等待，6 足以把 20 个源重叠掉又不显粗暴


def load_sources() -> list[dict]:
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)["sources"]


def build_gnews_url(query: str, locale: dict) -> str:
    q = urllib.parse.quote(query)
    return (f"https://news.google.com/rss/search?q={q}"
            f"&hl={locale['hl']}&gl={locale['gl']}&ceid={locale['ceid']}")


def _norm(s: str) -> str:
    """归一化：去掉空白、连接符与常见的「- 媒体名」尾巴，只留可比对的实体内容。"""
    s = re.sub(r"[\s\-–—|｜·]+", "", s or "")
    return re.sub(r"(新华网|央视网|第一财经|财新|参考消息|南方周末|联合早报|"
                  r"Reuters|CNN|APNews|apnews\.com|BBC|TheGuardian)$", "", s, flags=re.I)


def clean_excerpt(raw: str, limit: int = 200, title: str = "") -> str:
    """清洗摘要；**摘要若只是标题的重复就丢弃**。

    Google News 的 summary 字段常常原样返回标题（实测约占 58%），
    留着它等于把同一句话发给 AI 两遍——分类和汇总都要各付一次 token。

    判定刻意保守：只有当摘要归一化后是标题归一化的前缀（即完全不含新增信息）
    才丢。摘要只要多出一点内容就整段保留，宁可多发也不误删。
    """
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)[:limit].strip()
    if title:
        nt, ne = _norm(title), _norm(text)
        if ne and nt.startswith(ne):
            return ""
    return text


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


# ---- 标题黑名单 ----
# Google News 会把媒体站内的**非新闻页**也当成条目收进来：Reuters 的股票行情页、
# CNN 的栏目/播客/文字稿页、AP 的地区与球队标签页。这些「文章」没有任何内容，
# 却会照常参与分类、汇总和 Top5，把结果稀释掉（实测占抓取量的 12% 左右，
# 且高度集中在 world|金融市场 这类分类里）。这里在入库前直接丢弃。
#
# 取舍：**宁可漏判也不误杀**。每条规则都在真实抓取结果上逐条核对过，
# 改动这里之后建议同样跑一遍全库对比，确认没有真新闻被带走。
JUNK_TITLE_PATTERNS = [
    # 行情、报价与基金说明页
    r"Stock Price & Latest News",
    r"Stock Quote Price and Forecast",
    r"^About .+\([A-Z0-9]+\.[A-Za-z]{1,3}\)",              # About ProShares…(ACSP.OQ)
    r"^\(?[A-Za-z0-9]{1,10}\.[A-Za-z]{1,3}\)?\s*[-–—|]",   # 裸代码开头：PGNY.OQ - / (ROBN.N) |
    # 栏目页、播客推广、文字稿、站点地图
    r"Podcast on CNN Podcasts",
    r"^PODCAST:",
    r"^CNN\.com\s*[-–—]\s*Transcripts",
    r"^CNN Transcripts for ",
    r"^(Content|Videos|Sitemap)\b.*\bCNN$",
    r"^CNN (Newsroom|This Morning|NewsNight|News Central|Live Event|HEALTH)\b",
    r"^CNN's \w",                                          # CNN's Reliable Sources 等节目名
    r"\s[-–—]\s*CNN\s*[-–—]\s*CNN\s*$",                    # sunday spotlight - CNN - CNN
    r"^[\w' ]{1,24}\s>\s",                                 # 站点面包屑：food > restaurants
    # Reuters 的人物/话题标签页：「<人名> News | Today's Latest Stories - reuters.com」。
    # 不能锚 $：后面还跟着 Google News 加的「- reuters.com」后缀。
    r"\|\s*Today'?s Latest Stories\b",
    # CNN 自家的新闻稿（宣传自己要播什么），不是新闻
    r"\s[-–—]\s*CNN Press Room\s*$",
    r"^CNN\.com\s+QuickVote",
    # 财新的站内功能页与作者专栏索引页
    r"^历史搜索\b",
    r"_观点频道_",
    r"^财新一线\s*[-–—]",
    r"^Special Bonus\s*[-–—]\s*Introducing:",
    r"^Introducing:\s",
    # 节目名「The <栏目> with <主持人> - CNN」。两段都必须限长：不限长会撞上正文里的
    # with，把 "The obsession with 'Mamdani marts' isn't about groceries" 这类真新闻误杀。
    r"^The [\w'’ ]{1,20} with [\w'’. ]{1,22}\s*[-–—]\s*CNN$",
    # 聚合页与标签页
    r"\|\s*(Latest|Breaking)\b.{0,30}\bNews\b",
    r"^AP News Search",
    r"^World News: Top & Breaking",
    r"^Reuters\s*[-–—]\s*Reuters$",
    # 共同社英文站会推自动生成的地震速报占位条，以及只剩「- 媒体名」的空标题条
    r"^Earthquake alert \(automated\)",
    r"^\s*[-–—]\s*Japan Wire by Kyodo News\s*$",
    # NHK WORLD 把日语教学栏目也放进新闻流
    r"\bEasy Japanese \(Grammar Lessons\)",
]
# 逐条单独编译（而不是合并成一个大正则）：这样命中时能说出**是哪一条**规则拦的，
# 复查误杀时可以直接定位到要改的那一行。十几条正则的开销可以忽略。
_JUNK_RES = [(p, re.compile(p)) for p in JUNK_TITLE_PATTERNS]

# 媒体自己的栏目页 / 标签页：去掉「- 媒体名」后只剩一个极短的名词短语
# （AP 的 Wildfires、New York Yankees；Politico 的 Media、Foreign Policy、White House）。
#
# 只对列在这里的媒体生效，且**词数阈值一家一定**：这个阈值不是拍脑袋的，
# 是把该源已抓到的全部标题排序后，取「最长的栏目页」与「最短的真新闻」之间的空档。
# 不能一视同仁——CNN 的短标题里有真新闻，例如 "Library devastated by flood - CNN"。
_TAG_SUFFIXES = [
    (re.compile(r"\s[-–—|]\s*(AP News|apnews\.com)\s*$", re.I), 4,
     "<AP 短标签页：去掉「- AP News」后 ≤4 词且无标点>"),
    # Politico 栏目页最长 2 词（Foreign Policy / Health Care / White House），
    # 最短的真新闻是 3 词（Washington’s Mali gamble），中间没有重叠。
    (re.compile(r"\s[-–—|]\s*Politico\s*$", re.I), 2,
     "<Politico 短栏目页：去掉「- Politico」后 ≤2 词且无标点>"),
    # Google News 有时用域名而不是媒体名作后缀，同一批栏目页会以两种形式出现，
    # 只配一种就会漏掉一半（实测漏了 20 条，全都进了翻译与分类队列）。
    # 域名形式的阈值可以放到 3 词：实测 politico.com 后缀下 ≤3 词的全是栏目/话题页
    # （Liquefied natural gas、Critical infrastructure），而同样 3 词的真新闻
    # （Paging Micah Lasher）用的是「- Politico」形式，两者不重叠。
    (re.compile(r"\s[-–—|]\s*politico\.com\s*$", re.I), 3,
     "<Politico 短栏目页（域名后缀）：去掉「- politico.com」后 ≤3 词且无标点>"),
    # CNN 只对**极短**的生效：CNN 的 4 词标题里有真新闻
    # （Library devastated by flood），所以阈值必须压到 1 词。
    (re.compile(r"\s[-–—|]\s*(CNN|cnn\.com)\s*$", re.I), 1,
     "<CNN 单词标签页：去掉「- CNN」后仅 1 词>"),
]


def junk_reason(title: str) -> str | None:
    """命中黑名单则返回**命中的那条规则**，否则返回 None。

    返回规则本身而不是布尔值，是为了让被拦下的条目能连同「因何被拦」一起留档，
    见 db.record_dropped 与 `python -m newsagg.fetch --dropped`。
    """
    t = (title or "").strip()
    for pattern, rx in _JUNK_RES:
        if rx.search(t):
            return pattern
    for suffix, max_words, rule in _TAG_SUFFIXES:
        stem = suffix.sub("", t).strip()
        if stem == t or not stem.isascii():
            continue
        # 真新闻标题几乎不会短到这个程度，且多半带标点
        if len(stem.split()) <= max_words and not re.search(r"[.!?:;,]", stem):
            return rule
    return None


def is_junk_title(title: str) -> bool:
    """标题是否为媒体站内的非新闻页面（行情页、栏目页、标签页等）。"""
    return junk_reason(title) is not None


def clean_title(raw: str, source_name: str) -> str:
    """去掉 Google News 追加的「 - 媒体名」后缀。

    有些条目 Google 抽不出标题，只剩下 '- 联合早报' 这样的空壳，
    去后缀后为空即视为脏数据，由调用方丢弃。
    """
    t = (raw or "").strip()
    t = re.sub(r"\s*[-–—|]\s*[^-–—|]{1,30}$", "", t).strip() if t.startswith("-") else t
    return t.lstrip("-–—|").strip()


def fetch_source(src: dict, hours: int = 24, stats: dict | None = None) -> list[Article]:
    """抓一个源。stats 若给出，会记下本次滤掉的非新闻页条数（供调用方打印）。"""

    def drop_junk(arts: list[Article]) -> list[Article]:
        """滤掉非新闻页。被拦下的一律留档，供事后复查是否误杀。"""
        good, dropped = [], []
        for a in arts:
            why = junk_reason(a.title)
            if why is None:
                good.append(a)
            else:
                dropped.append({"id": a.id, "source": a.source, "source_name": a.source_name,
                                "title": a.title, "url": a.url, "pattern": why})
        if dropped:
            db.record_dropped(dropped)
        if stats is not None:
            stats["junk"] = stats.get("junk", 0) + len(dropped)
        return good

    if src["method"] == "scrape":
        from .scrapers import SCRAPERS
        fn = SCRAPERS.get(src["id"])
        if not fn:
            raise RuntimeError(f"未注册抓取器：{src['id']}（见 newsagg/scrapers.py）")
        return drop_junk(fn(src, hours))

    if src["method"] == "gnews":
        urls = [build_gnews_url(src["query"], src["gnews_locale"])]
    else:
        urls = src["feeds"]

    # 出稿频率低的源用更长的窗口抓（见 models.SOURCE_WINDOW_HOURS）。
    # 必须和展示侧 db._window_sql 用同一张表，否则会出现「抓回来了但页面不显示」
    # 或反过来「页面要显示但根本没抓」的错位。
    src_hours = max(hours, SOURCE_WINDOW_HOURS.get(src["id"], 0))
    cutoff = datetime.now(timezone.utc).timestamp() - src_hours * 3600
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
                excerpt=clean_excerpt(e.get("summary", ""), title=title),
                fetched_at=now_iso,
            ))
    return drop_junk(out)


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

    total_new, total_junk = 0, 0
    print(f"抓取窗口：过去 {hours} 小时\n" + "-" * 48)

    # 并发抓取：这一步是纯网络等待，各源互不相关，串行纯属浪费。
    # 只并发「取」，写库仍在主线程按源顺序进行——SQLite 多写容易撞锁，
    # 而且顺序输出才能让日志保持和 sources.yaml 一致、便于对照。
    # 并发度取 6：足以把 20 个源的等待重叠掉，又不至于对 Google News 造成突发压力。
    def grab(src: dict):
        stats: dict = {}
        return src, fetch_source(src, hours, stats), stats, None

    results: dict[str, tuple] = {}
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(sources) or 1)) as ex:
        futs = {ex.submit(grab, s): s for s in sources}
        for f in as_completed(futs):
            s = futs[f]
            try:
                results[s["id"]] = f.result()
            except Exception as exc:            # 单源失败不阻塞整体
                results[s["id"]] = (s, [], {}, exc)

    for src in sources:                          # 按配置顺序输出与写库
        _, articles, stats, err = results.get(src["id"], (src, [], {}, None))
        if err is not None:
            print(f"[ERR] {src['id']:<9} {src['name']:<8} 失败: {err}")
            continue
        junk = stats.get("junk", 0)
        total_junk += junk
        new = db.upsert_articles(articles)
        total_new += new
        status = "OK " if articles else "空  "
        print(f"[{status}] {src['id']:<9} {src['name']:<8} "
              f"取到 {len(articles):>3} 条，新增 {new:>3}"
              f"{f'，滤除 {junk:>2} 条非新闻页' if junk else ''}  ({src['method']})")
    print("-" * 48)
    print(f"总新增 {total_new} 条 -> {db.DB_PATH}")
    if total_junk:
        print(f"已滤除 {total_junk} 条非新闻页（行情/栏目/标签页，见 fetch.JUNK_TITLE_PATTERNS）")


def dropped_report() -> str:
    """把留档整理成一份可通读的清单，按规则分组。

    分组按「该规则总共拦了多少条」升序：命中数少的排在最前面。
    拦了几十条行情页的规则基本不会错，只命中一两条的那条才最可能是误伤，
    把它放在开头，复查时第一眼就能看到。
    """
    rows = db.dropped_by_pattern()
    if not rows:
        return "暂无留档。跑一次 python run.py 之后再来看。"

    out = [f"被过滤的非新闻页：{len(rows)} 条（按 URL 去重）", ""]
    out.append("按命中规则分组，**命中数少的排在前面——那里最可能藏着误杀**。")
    out.append("发现真新闻被拦，就去 newsagg/fetch.py 的 JUNK_TITLE_PATTERNS 改对应那条规则。")
    out.append("")
    cur = None
    for r in rows:
        if r["pattern"] != cur:
            cur = r["pattern"]
            out.append("─" * 76)
            out.append(f"规则  {r['pattern']}")
            out.append(f"      共拦下 {r['n_pattern']} 条"
                       + ("   <- 命中很少，重点核对" if r["n_pattern"] <= 3 else ""))
        seen = f" ×{r['n_seen']}" if r["n_seen"] > 1 else ""
        out.append(f"    [{r['source_name']}] {r['title']}{seen}")
        # 完整输出 URL：截断的 Google News 跳转链接点不开，而复查时要能直接打开核对
        out.append(f"        {r['url']}")
    out.append("─" * 76)
    out.append(f"\n留档表：dropped_titles（{db.DB_PATH}）")
    return "\n".join(out)


def show_dropped(out_path: str | None = None) -> None:
    text = dropped_report()
    if out_path:
        # 显式写 UTF-8：Windows 控制台默认 cp936，直接用 > 重定向会把中文写乱
        Path(out_path).write_text(text, encoding="utf-8")
        print(f"清单已写入 {out_path}（UTF-8，共 {len(text.splitlines())} 行）")
    else:
        print(text)


def purge_junk(dry_run: bool = True) -> int:
    """拿当前黑名单复查**已入库**的标题，把命中的清出去。

    黑名单只在抓取时生效，所以每次新增规则，之前已经存进库的同类页面还留在里面，
    照样会出现在页面上、进入简报素材。改完 JUNK_TITLE_PATTERNS 就跑一次这个。
    默认只报告不删，确认清单无误后再加 --purge-junk --yes。
    """
    with db.connect() as conn:
        rows = conn.execute("SELECT id, source, source_name, title, url FROM articles").fetchall()
    hits = [dict(r) | {"pattern": w} for r in rows if (w := junk_reason(r["title"]))]
    if not hits:
        print("已入库的标题里没有命中黑名单的，无需清理。")
        return 0

    by_rule: dict[str, list] = {}
    for h in hits:
        by_rule.setdefault(h["pattern"], []).append(h)
    for rule, items in sorted(by_rule.items(), key=lambda x: len(x[1])):
        print(f"【{rule}】{len(items)} 条")
        for h in items[:8]:
            print(f"    [{h['source_name']}] {h['title'][:74]}")
        if len(items) > 8:
            print(f"    …… 另有 {len(items) - 8} 条")

    if dry_run:
        print(f"\n共 {len(hits)} 条命中（仅预览，未删除）。确认后执行："
              "\n  python -m newsagg.fetch --purge-junk --yes")
        return 0

    db.record_dropped(hits)          # 先留档，再删除：清理掉的同样可以事后复查
    ids = [h["id"] for h in hits]
    with db.connect() as conn:
        for table, col in (("article_categories", "article_id"),
                           ("event_articles", "article_id"),
                           ("trivial_judged", "article_id")):
            conn.execute(f"DELETE FROM {table} WHERE {col} IN "
                         f"({','.join('?' * len(ids))})", ids)
        conn.execute(f"DELETE FROM articles WHERE id IN ({','.join('?' * len(ids))})", ids)
        conn.commit()
    print(f"\n已清理 {len(hits)} 条，并留档到 dropped_titles（可用 --dropped 复查）。")
    return len(hits)


if __name__ == "__main__":
    if "--purge-junk" in sys.argv:
        purge_junk(dry_run="--yes" not in sys.argv)
    elif "--dropped" in sys.argv:
        i = sys.argv.index("--out") if "--out" in sys.argv else -1
        show_dropped(sys.argv[i + 1] if i >= 0 and i + 1 < len(sys.argv) else None)
    else:
        hrs = int(sys.argv[1]) if len(sys.argv) > 1 else 24
        run(hrs)
