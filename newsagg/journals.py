"""学术期刊：抓取、噪声过滤、影响力指标。

为什么走 Crossref 而不是逐家爬站：
  这 17 本刊分属 8 家出版商（Springer、AAAS、IEEE、Oxford、Cambridge、
  UChicago、Wiley、APA），各家页面结构与反爬策略都不一样，逐个写解析器既脆弱
  又要逐家查 robots.txt。Crossref 是这些出版商**自己**提交元数据的地方，
  一个接口覆盖全部，免费、无需 key，也就没有合规问题。

为什么论文不进 articles 表：
  见 models.DDL 里 papers 表上方的注释——translate / classify / english /
  琐碎判定这几步取数时都不带 region 过滤，论文一旦混进去就会被整条 AI 流水线
  卷走，既烧 token 又污染新闻的分类和 Top5。

实测数据（2026-08，90 天窗口，用于解释下面几个常量的取值）：
  Nature 约 1050 条、Science 533、TPAMI 326、TNNLS 119，
  而 Demography 28、Social Work 14、管理学会展望 10、世界政治约 10。
  量级差两个数量级：展示不再截断（学科页按刊分块 + 可按刊筛选），
  但 Top5 候选池仍按刊限量，否则 Nature 会把小刊挤出候选。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import db

CONFIG = Path(__file__).resolve().parent.parent / "config" / "journals.yaml"

CROSSREF = "https://api.crossref.org/journals/{issn}/works"
OPENALEX_SRC = "https://api.openalex.org/sources/issn:{issn}"
OPENALEX_WORKS = "https://api.openalex.org/works"

# 窗口内**有多少抓多少**：用游标翻页把 90 天内的全部条目取回来。
# 早先只取最新 200 篇，实测 Nature 90 天有 1065 条却只入库 173 条、
# Science 519 只入 186、TPAMI 242 只入 200——学科页显示的完全不是真实存量。
ROWS_PER_PAGE = 500       # Crossref 单页上限 1000，取 500 兼顾响应体积与翻页次数
MAX_PAGES = 12            # 安全阀：单刊最多翻这么多页（500×12=6000 篇，远超任何刊的 90 天量）
                          # 只为防配置写错时无限翻页，正常绝不会触到
WORKERS = 5               # 并发抓取的刊数。Crossref 礼貌池限速很宽，5 是保守值
FETCH_EVERY_HOURS = 24    # 论文不按小时更新，一天抓一次足够
TIMEOUT = 30


def _mailto() -> str:
    """Crossref 的礼貌池要一个联系邮箱，填了响应更快、限速更宽。

    读环境变量而不是写死在仓库里——那会把作者邮箱公开到 GitHub 上。
    不填也能正常用，只是走公共池。
    """
    return (os.getenv("CROSSREF_MAILTO") or "").strip()


def _ua() -> str:
    m = _mailto()
    return f"BriefMe/1.0 (https://github.com/yudianke/BriefMe{'; mailto:' + m if m else ''})"


def load_journals() -> tuple[list[dict], list[dict]]:
    """返回 (学科列表, 期刊列表)。"""
    with open(CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg["disciplines"], cfg["journals"]


# ---------- 噪声过滤 ----------
#
# 与 fetch.JUNK_TITLE_PATTERNS 同样的取舍：**宁可漏判也不误杀**，
# 每条规则单独编译，命中时能说出是哪一条，被拦下的一律留档可复查。
#
# 实测：对 Nature 保留 84/100（拦掉的都是更正、每日简报、书评），
# 对 TPAMI / TNNLS 保留 100/100（一条没拦，说明规则没有误伤研究论文）。
JUNK_PAPER_PATTERNS = [
    # 更正、撤稿、勘误——是对已有论文的行政记录，不是新研究
    r"^(Author|Publisher) Correction:",
    r"^Correction to:",
    r"^Corrigendum",
    r"^Erratum",
    r"^Retraction Note",
    r"^Editorial Expression of Concern",
    r"^Expression of Concern:",
    r"^Withdrawn:",
    # 期刊自己的栏目与例行件
    r"^Daily briefing:",
    r"^Briefing Chat:",
    r"^Books in brief",
    r"^In Science Journals$",
    r"^Editorial Board",
    r"^Front Matter$",
    r"^Back Matter$",
    r"^Table of Contents",
    # 实际标题形如「JSP volume 55 issue 3 Cover and Front matter」——
    # 中间可能是 Cover / Front / Cover and Front，所以不写死措辞，只锚定前后
    r"volume \d+ issue \d+ .{0,24}\bmatter\b",
    r"^Issue Information",
    r"^Masthead",
    r"^Index to Volume",
    # Science 的书评：标题后面直接拼着整条书目信息，形如
    # 「A harrowing tour of Earth's sacrificial lands <b>The Vanishing Earth</b>
    #   <i>James Crawford</i> Bloomsbury, 2026. 272 pp.」——是书评不是研究论文。
    # 用页数锚定：研究论文标题不会出现「272 pp.」这种写法。
    r"\d{2,4}\s*pp\.\s*$",
]
_JUNK_RES = [(p, re.compile(p, re.I)) for p in JUNK_PAPER_PATTERNS]


def paper_junk_reason(title: str) -> str | None:
    """命中噪声规则则返回**命中的那条规则**，否则 None。"""
    t = (title or "").strip()
    if not t:
        return "<空标题>"
    for pattern, rx in _JUNK_RES:
        if rx.search(t):
            return pattern
    return None


# ---------- Crossref 字段解析 ----------

_JATS = re.compile(r"<[^>]+>")
# 下标/上标要连着前面的字符：`CO <sub>2</sub>` 直接去标签会变成「CO 2」，应是「CO2」
_SUBSUP = re.compile(r"\s*<(sub|sup)>(.*?)</\1>", re.I | re.S)
_ENTITIES = {"&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"',
             "&apos;": "'", "&nbsp;": " ", "&#x2019;": "’", "&#8217;": "’"}


def strip_markup(raw: str) -> str:
    """去掉出版商嵌在标题/摘要里的 HTML/JATS 标签。

    IEEE 会用 <u> 强调缩写字母（`<u>Fed</u>erated`），Science 会用 <i>/<b> 标
    物种名与书名，PNAS 系用 <sub> 标下标。不处理的话这些标签会原样出现在页面上，
    也会被当成标题的一部分送去翻译。
    """
    if not raw:
        return ""
    t = _SUBSUP.sub(lambda m: m.group(2), raw)
    t = _JATS.sub("", t)                      # 用空串而不是空格，避免把单词劈开
    for k, v in _ENTITIES.items():
        t = t.replace(k, v)
    return re.sub(r"\s+", " ", t).strip()


def clean_abstract(raw: str, limit: int = 300) -> str:
    """Crossref 的摘要是 JATS XML，形如
    `<jats:title>Abstract</jats:title><jats:p>We study...</jats:p>`，
    去标签后还要去掉开头那个多余的 "Abstract" 字样。
    """
    t = strip_markup(raw)
    t = re.sub(r"^Abstract[:\s]*", "", t, flags=re.I).strip()
    return t[:limit]


def _parts(parts: list | None) -> list[int]:
    """把 date-parts 拍平成 [y] / [y,m] / [y,m,d]，过滤掉 null。"""
    if not parts:
        return []
    p = parts[0] if isinstance(parts[0], list) else parts
    return [int(x) for x in p if x]


def parse_date(parts: list | None) -> str | None:
    """date-parts 可能是 [2026,8,14] / [2026,9] / [2026]。缺月日按 1 号补。"""
    p = _parts(parts)
    if not p:
        return None
    y = p[0]
    m = p[1] if len(p) > 1 else 1
    d = p[2] if len(p) > 2 else 1
    try:
        return datetime(y, min(max(m, 1), 12), min(max(d, 1), 28),
                        tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def resolve_date(item: dict) -> tuple[str | None, bool]:
    """定出这篇论文该用哪个日期。返回 (ISO 日期, 是否用了 created 兜底)。

    **IEEE 系（TPAMI / TNNLS / T-RO）向 Crossref 提交的 published 只精确到年**，
    实测最新一批全是 `[[2026]]`。按「缺月日补 1 号」处理就会变成 2026-01-01，
    于是这些刊在 90 天窗口里一篇都不剩——T-RO 第一次抓取正是 0 篇。

    Crossref 的 `created`（记录建档时间）总是完整日期，对 IEEE 这种
    「先上线后编期号」的刊，它恰好接近论文实际可读到的时间。
    所以：published 精确到月及以上就用它，只有年时改用 created。
    """
    pub = item.get("published", {}).get("date-parts")
    if len(_parts(pub)) >= 2:
        return parse_date(pub), False
    created = item.get("created", {}).get("date-parts")
    if _parts(created):
        return parse_date(created), True
    return parse_date(pub), False        # 只有年也好过没有


def fmt_authors(authors: list | None, keep: int = 3) -> str:
    if not authors:
        return ""
    names = []
    for a in authors[:keep]:
        fam, giv = (a.get("family") or "").strip(), (a.get("given") or "").strip()
        nm = f"{giv} {fam}".strip() if fam else (a.get("name") or "").strip()
        if nm:
            names.append(nm)
    if not names:
        return ""
    return ", ".join(names) + (" et al." if len(authors) > keep else "")


def _get_json(url: str, tries: int = 3) -> dict | None:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _ua()})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.5 * (i + 1))
    return None


def fetch_journal(j: dict, days: int, stats: dict | None = None) -> list[dict]:
    """抓一本刊在窗口内的论文。返回待入库的 dict 列表。"""
    since_ts = datetime.now(timezone.utc).timestamp() - days * 86400
    since_str = datetime.fromtimestamp(since_ts, timezone.utc).date().isoformat()
    # 按 created（建档时间）而不是 published 过滤与排序：published 的精度因出版商而异，
    # IEEE 系只给到年，用它过滤会把这几本刊整个漏掉（见 resolve_date）。
    # created 各家都是完整日期，口径统一。
    # 用游标深翻，把窗口内的**全部**条目取回来，不再截断。
    # 早先只取最新 200 篇，于是 Nature 90 天有 1065 条却只入库 173 条、
    # 学科页上看到的完全不是真实存量。Crossref 的 cursor 是官方推荐的深翻方式，
    # 比 offset 稳（offset 超过一定深度会被拒），每页返回 next-cursor 供下一页用。
    items: list[dict] = []
    cursor, total = "*", None
    for _ in range(MAX_PAGES):
        params = urllib.parse.urlencode({
            "filter": f"from-created-date:{since_str},type:journal-article",
            "sort": "created", "order": "desc", "rows": ROWS_PER_PAGE,
            "cursor": cursor,
            "select": "title,abstract,author,published,created,DOI,container-title",
        })
        data = _get_json(f"{CROSSREF.format(issn=j['issn'])}?{params}")
        if not data or "message" not in data:
            # 第一页就失败才算这本刊抓取失败；后续页失败就用已拿到的部分，
            # 免得一次网络抖动把整本刊的结果丢掉。
            if not items:
                raise RuntimeError("Crossref 无响应或返回异常")
            break
        msg = data["message"]
        if total is None:
            total = msg.get("total-results")
        page = msg.get("items", [])
        items.extend(page)
        cursor = msg.get("next-cursor") or ""
        # 取满了或没有下一页就停
        if not cursor or not page or (total is not None and len(items) >= total):
            break
    if stats is not None:
        stats["fetched"] = len(items)
        stats["total"] = total if total is not None else len(items)
    data = {"message": {"items": items}}

    now_iso = datetime.now(timezone.utc).isoformat()
    since_iso = datetime.fromtimestamp(since_ts, timezone.utc).isoformat()
    # 迟交的旧论文：出版商偶尔会把几年前的文章补交到 Crossref，created 是今天但
    # published 是 2019 年。按 created 过滤会把它当新论文放进来，这里再挡一道。
    stale_year = datetime.now(timezone.utc).year - 1

    out, dropped = [], []
    for it in data["message"].get("items", []):
        title = strip_markup((it.get("title") or [""])[0] or "")
        doi = (it.get("DOI") or "").strip()
        pub, used_created = resolve_date(it)
        if not doi or not pub or pub < since_iso:
            continue
        pub_year = (_parts(it.get("published", {}).get("date-parts")) or [None])[0]
        if used_created and pub_year and pub_year < stale_year:
            continue
        why = paper_junk_reason(title)
        if why:
            dropped.append({"id": hashlib.sha1(doi.encode()).hexdigest(),
                            "source": j["id"], "source_name": j["name"],
                            "title": title, "url": f"https://doi.org/{doi}",
                            "pattern": why})
            continue
        out.append({
            "id": hashlib.sha1(doi.encode()).hexdigest(),
            "journal_id": j["id"], "journal_name": j["name"],
            "discipline": j["discipline"], "sci": bool(j.get("sci")),
            "title": title, "doi": doi, "url": f"https://doi.org/{doi}",
            "authors": fmt_authors(it.get("author")),
            "abstract": clean_abstract(it.get("abstract") or ""),
            "published_at": pub, "fetched_at": now_iso,
        })
    if dropped:
        # 复用新闻那套留档表：同一个 --dropped 命令就能一起复查，不必再造一套
        db.record_dropped(dropped)
    if stats is not None:
        stats["junk"] = len(dropped)
    return out


# ---------- OpenAlex 补充 ----------

def _invert(idx: dict | None) -> str:
    """OpenAlex 的摘要是倒排索引 {词: [位置...]}，还原成正常语序。"""
    if not idx:
        return ""
    pos: dict[int, str] = {}
    for word, locs in idx.items():
        for p in locs:
            pos[p] = word
    return " ".join(pos[k] for k in sorted(pos))


def backfill_abstracts(papers: list[dict], limit: int = 260,
                       days: int = db.PAPER_PICK_DAYS) -> int:
    """给没有摘要的论文补摘要。

    Crossref 的摘要覆盖率取决于出版商是否提交：实测 Science / 人口学 / 经济学季刊 /
    金融学杂志 覆盖良好，而 Nature / IEEE 三本 / 美国心理学家 / 政治经济学杂志 基本为空
    （整体只有约三成有摘要）。OpenAlex 能补上其中一部分，补不上的就只有标题——
    这一点如实标在页面上，不靠模型脑补。

    **只补最近 days 天的**：一次查询只能查一篇 DOI，全库补要上千次请求，
    而命中率只有约一成。真正需要摘要的是 Top5 的候选池（同样取最近 days 天），
    把请求花在这批上性价比最高；更早的论文页面上只展示标题和链接，够用。
    """
    cutoff = db.paper_cutoff(days)
    todo = [p for p in papers if not p["abstract"] and p["published_at"] >= cutoff][:limit]
    if not todo:
        return 0
    n = 0
    for p in todo:
        d = _get_json(f"{OPENALEX_WORKS}/doi:{urllib.parse.quote(p['doi'])}"
                      f"?select=abstract_inverted_index")
        if d:
            ab = clean_abstract(_invert(d.get("abstract_inverted_index")))
            if ab:
                p["abstract"] = ab
                n += 1
        time.sleep(0.06)
    return n


def refresh_metrics(force: bool = False) -> int:
    """刷新期刊影响力指标。

    **这是 OpenAlex 的两年篇均被引，不是 Clarivate 的影响因子（JIF）。**
    JIF 是专有数据，本项目拿不到，也不应该用别的指标冒充它。
    指标变化很慢，30 天刷一次足够。
    """
    if not force and not db.metrics_stale(30):
        return 0
    _, journals = load_journals()
    rows = []
    for j in journals:
        d = _get_json(OPENALEX_SRC.format(issn=j["issn"]))
        if not d or "id" not in d:
            continue
        s = d.get("summary_stats") or {}
        rows.append({"journal_id": j["id"],
                     "mean_citedness": s.get("2yr_mean_citedness"),
                     "h_index": s.get("h_index"),
                     "works_count": d.get("works_count")})
        time.sleep(0.06)
    if rows:
        db.save_journal_metrics(rows)
    return len(rows)


# ---------- 入口 ----------

def _last_fetch() -> str | None:
    with db.connect() as conn:
        r = conn.execute("SELECT MAX(fetched_at) g FROM papers").fetchone()
    return r["g"] if r else None


def fetch(days: int = db.PAPER_WINDOW_DAYS, force: bool = False) -> int:
    """抓全部期刊。返回新增论文数。距上次抓取不足 FETCH_EVERY_HOURS 则跳过。"""
    db.init()
    last = _last_fetch()
    if not force and last and last > db.window_cutoff(FETCH_EVERY_HOURS):
        print(f"  学术期刊：{FETCH_EVERY_HOURS}h 内已抓过，跳过（上次 {last[:16]}）")
        return 0

    _, journals = load_journals()
    print(f"  抓取 {len(journals)} 本期刊（过去 {days} 天）…")
    results: dict[str, tuple] = {}

    def grab(j):
        st: dict = {}
        return j, fetch_journal(j, days, st), st, None

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(journals))) as ex:
        futs = {ex.submit(grab, j): j for j in journals}
        for f in as_completed(futs):
            j = futs[f]
            try:
                results[j["id"]] = f.result()
            except Exception as exc:       # 单刊失败不影响其余
                results[j["id"]] = (j, [], {}, exc)

    total_new = total_junk = 0
    allp: list[dict] = []
    for j in journals:                      # 按配置顺序输出，便于对照
        _, papers, st, err = results.get(j["id"], (j, [], {}, None))
        if err is not None:
            print(f"    [ERR] {j['id']:<14} {j['name_en']:<34} 失败: {err}")
            continue
        junk = st.get("junk", 0)
        total_junk += junk
        allp.extend(papers)
        # 把 Crossref 侧的总数一并打出来：翻页取回数应当等于它，
        # 不等就说明翻页提前断了（比如撞上 MAX_PAGES），一眼能看出来。
        crossref_total = st.get("total")
        got = st.get("fetched")
        span = (f"  [Crossref {crossref_total} 条"
                + ("，已全部取回]" if got is not None and crossref_total is not None
                   and got >= crossref_total else f"，取回 {got}]")
                ) if crossref_total is not None else ""
        print(f"    [OK ] {j['id']:<14} {j['name_en']:<34} 入库 {len(papers):>4} 篇"
              + (f"，滤除 {junk:>3} 条" if junk else "") + span)

    if allp:
        n_ab = backfill_abstracts(allp)
        total_new = db.upsert_papers(allp)
        if n_ab:
            print(f"    OpenAlex 补充摘要 {n_ab} 篇")
    print(f"  论文新增 {total_new} 篇（窗口内共 {len(allp)} 篇）"
          + (f"，滤除 {total_junk} 条非研究条目" if total_junk else ""))

    n_metric = refresh_metrics()
    if n_metric:
        print(f"  期刊指标已刷新 {n_metric} 本（OpenAlex 两年篇均被引）")
    pruned = db.prune_papers()
    if pruned:
        print(f"  清理 {pruned} 篇超过 {db.PAPER_KEEP_DAYS} 天的论文")
    return total_new


# 注意：这里**没有**一个「抓取 + AI 一条龙」的入口，是刻意的。
# 论文的 AI 环节必须排在全部新闻步骤之后（新闻和论文共用每天 20 万 token 的额度，
# 先跑论文会把额度吃光，新闻的分类纪要和 Top5 就一段都出不来——实测发生过）。
# 由 run.py 分两处调用：抓取放在前面（不花 AI），paperai 那两步放在最后。


def dropped_report() -> str:
    """被噪声规则拦下的论文条目，按规则分组，命中少的排前面（同 fetch.dropped_report）。"""
    rows = db.dropped_by_pattern()
    keep = {p for p in JUNK_PAPER_PATTERNS}
    rows = [r for r in rows if r["pattern"] in keep or r["pattern"] == "<空标题>"]
    if not rows:
        return "暂无学术条目留档。跑一次 python -m newsagg.journals 之后再来看。"
    out, cur = [], None
    for r in rows:
        if r["pattern"] != cur:
            cur = r["pattern"]
            out.append("─" * 76)
            out.append(f"规则  {r['pattern']}")
            out.append(f"      共拦下 {r['n_pattern']} 条"
                       + ("   <- 命中很少，重点核对" if r["n_pattern"] <= 3 else ""))
        out.append(f"    [{r['source_name']}] {r['title']}")
        out.append(f"        {r['url']}")
    out.append("─" * 76)
    return "\n".join(out)


if __name__ == "__main__":
    if "--dropped" in sys.argv:
        print(dropped_report())
    elif "--metrics" in sys.argv:
        print(f"已刷新 {refresh_metrics(force=True)} 本期刊的指标")
    else:
        fetch(force="--force" in sys.argv)
