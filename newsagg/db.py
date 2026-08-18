"""SQLite 读写、去重，以及**统一的滚动窗口**查询层（窗口长度见 models.WINDOW_HOURS）。

规则（全局强制）：UI 与 AI 输入只能看到 `published_at`（媒体发布时间，UTC）
落在「当前时间往前 WINDOW_HOURS 小时」内的新闻。数据库长期保留历史，
但所有对外查询都必须走本模块的 recent_* 函数，不要自己写 SELECT。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import DDL, SOURCE_WINDOW_HOURS, WINDOW_HOURS, Article

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "news.db"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout：抓取阶段是多线程并发的，各线程会各自写 dropped_titles。
    # SQLite 同一时刻只允许一个写者，撞上时默认立刻抛 "database is locked"；
    # 给 10 秒等待窗口即可，这些写入都极小，实际几乎不会等到。
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


# 轻量迁移表：老库缺哪列就补哪列（DDL 里的 CREATE TABLE IF NOT EXISTS 只对新库生效，
# 已存在的表不会自动长出新列，所以每次加列都要在这里登记一条）。
_MIGRATIONS = {
    "articles":  {"title_zh": "TEXT DEFAULT ''", "title_en": "TEXT DEFAULT ''",
                  "trivial": "INTEGER DEFAULT 0"},
    "summaries": {"ai_text_en": "TEXT DEFAULT ''"},
    "events":    {"title_en": "TEXT DEFAULT ''", "summary_en": "TEXT DEFAULT ''"},
}


def init() -> None:
    with connect() as conn:
        conn.executescript(DDL)
        for table, cols in _MIGRATIONS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def window_cutoff(hours: int = WINDOW_HOURS) -> str:
    """滚动窗口起点（UTC ISO8601）。所有查询共用同一口径。"""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _window_sql(hours: int = WINDOW_HOURS, alias: str = "") -> tuple[str, list]:
    """窗口条件片段 + 参数。个别源用更长的窗口，见 models.SOURCE_WINDOW_HOURS。

    抽出来是因为「窗口内」这个条件散落在八处查询里，逐个改必然漏掉一两个，
    而漏掉的那个会让放宽窗口的源在某一环节凭空消失（比如没被分类，于是进不了分类页）。

    两条硬性约束：
      - **只放宽、不收紧**：用 OR 拼接，某源即便配了比默认更短的窗口也不会被截掉；
      - **自带最外层括号**：调用处多为 `WHERE region=? AND <此片段>`，
        少了括号 OR 会吃掉前面的 region 条件，把别的地区漏进来。
    """
    p = f"{alias}." if alias else ""
    sql = f"({p}published_at >= ?"
    params: list = [window_cutoff(hours)]
    for sid, h in SOURCE_WINDOW_HOURS.items():
        if h <= hours:            # 不比默认窗口长就没必要加分支
            continue
        sql += f" OR ({p}source = ? AND {p}published_at >= ?)"
        params += [sid, window_cutoff(h)]
    return sql + ")", params


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def upsert_articles(articles: list[Article]) -> int:
    """按 id(url 哈希) 去重插入，返回新增条数。"""
    init()
    inserted = 0
    with connect() as conn:
        for a in articles:
            cur = conn.execute(
                """INSERT OR IGNORE INTO articles
                   (id, source, source_name, region, title, url, lang, published_at, excerpt, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (a.id, a.source, a.source_name, a.region, a.title, a.url,
                 a.lang, a.published_at, a.excerpt, a.fetched_at),
            )
            inserted += cur.rowcount
    return inserted


# ---------- 统一的窗口内查询 ----------

def recent_articles(hours: int = WINDOW_HOURS, region: str | None = None,
                    limit: int | None = None) -> list[sqlite3.Row]:
    """窗口内文章，按发布时间从新到旧。"""
    wsql, params = _window_sql(hours)
    sql = f"SELECT * FROM articles WHERE {wsql}"
    if region:
        sql += " AND region = ?"
        params.append(region)
    sql += " ORDER BY published_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def recent_by_category(region: str, category: str,
                       hours: int = WINDOW_HOURS, limit: int | None = None) -> list[sqlite3.Row]:
    """窗口内某地区某分类的文章，从新到旧。"""
    wsql, wparams = _window_sql(hours, "a")
    sql = f"""SELECT a.* FROM articles a
             JOIN article_categories c ON c.article_id = a.id
             WHERE a.region=? AND c.category=? AND {wsql}
             ORDER BY a.published_at DESC"""
    params: list = [region, category, *wparams]
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def category_counts(region: str, hours: int = WINDOW_HOURS) -> dict[str, int]:
    """窗口内各分类计数（用于分类网格）。"""
    wsql, wparams = _window_sql(hours, "a")
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT c.category cat, COUNT(*) n FROM articles a
               JOIN article_categories c ON c.article_id = a.id
               WHERE a.region=? AND {wsql}
               GROUP BY c.category""",
            (region, *wparams),
        ).fetchall()
    return {r["cat"]: r["n"] for r in rows}


RESUMMARIZE_RATIO = 0.05   # 新增文章占比低于此值就不重算纪要


def summaries_todo(hours: int = WINDOW_HOURS) -> list[tuple[str, str]]:
    """当天需要生成或**重写**纪要的 (region, category)。

    两种情况要写：
      1. 今天还没写过；
      2. 写过，但之后新抓到的文章已占该分类的 RESUMMARIZE_RATIO 以上。

    第 2 条是必要的：纪要按日期缓存，而文章窗口是滚动的，两者口径不同。
    同一天内文章可以整批换掉——早上的稿子滚出窗口、新稿子进来——若只看
    「今天写过没有」，纪要就会一直停在当天第一次运行时的素材上。
    用 fetched_at 而不是 published_at 比较：判断依据是「这篇有没有被那次汇总
    看到过」，取决于何时入库，而不是媒体何时发稿。

    但也不能一有新稿就重写：汇总是整条流水线里最贵的一步，一天里多跑几次
    会反复烧额度，而多出三五篇通常改变不了纪要的内容。因此加一个比例门槛，
    只有素材确实换掉一部分才值得重写。
    """
    wsql, wparams = _window_sql(hours, "a")
    with connect() as conn:
        # 取每个分类**最近一次**纪要，而不是「今天的」。
        # 纪要的 date 是 UTC 日历日，内容窗口却是滚动的：若按日期查，
        # UTC 零点一过所有纪要就集体「消失」，阈值失去意义、当天第一次运行必然
        # 全量重算。对 UTC+8 用户那是每天早上 8 点。改看 generated_at 之后，
        # 是否重写只取决于素材换了多少，与日历翻页无关。
        done = {(r["region"], r["category"]): r["generated_at"] for r in conn.execute(
            """SELECT region, category, MAX(generated_at) AS generated_at
               FROM summaries GROUP BY region, category""")}
        rows = conn.execute(
            f"""SELECT a.region, c.category, a.fetched_at
               FROM articles a JOIN article_categories c ON c.article_id = a.id
               WHERE {wsql}""",
            wparams,
        ).fetchall()
    total: dict[tuple[str, str], int] = {}
    fresh: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["region"], r["category"])
        total[key] = total.get(key, 0) + 1
        gen = done.get(key)
        if gen is not None and (r["fetched_at"] or "") > gen:
            fresh[key] = fresh.get(key, 0) + 1
    todo = []
    for key, n in total.items():
        if key not in done:
            todo.append(key)                                   # 没写过
        elif fresh.get(key, 0) >= max(1, n * RESUMMARIZE_RATIO):
            todo.append(key)                                   # 素材换掉够多
    return todo


def untranslated(hours: int = WINDOW_HOURS) -> list[sqlite3.Row]:
    """窗口内需要中文译题、且尚未翻译的外文文章。"""
    wsql, wparams = _window_sql(hours)
    with connect() as conn:
        return conn.execute(
            f"""SELECT * FROM articles
               WHERE {wsql} AND lang!='zh'
                 AND (title_zh IS NULL OR title_zh='')
               ORDER BY published_at DESC""",
            wparams,
        ).fetchall()


def set_title_zh(article_id: str, title_zh: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE articles SET title_zh=? WHERE id=?", (title_zh, article_id))


# ---------- 英文界面用的译文（仅 --en 模式生成） ----------

def untranslated_en(hours: int = WINDOW_HOURS) -> list[sqlite3.Row]:
    """窗口内需要英文译题、且尚未翻译的中文文章。

    与 untranslated() 完全对称：那边是「外文原题 -> 中文译题」，
    这边是「中文原题 -> 英文译题」。两边都只新增列，不动 title。
    """
    wsql, wparams = _window_sql(hours)
    with connect() as conn:
        return conn.execute(
            f"""SELECT * FROM articles
               WHERE {wsql} AND lang='zh'
                 AND (title_en IS NULL OR title_en='')
               ORDER BY published_at DESC""",
            wparams,
        ).fetchall()


def set_title_en(article_id: str, title_en: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE articles SET title_en=? WHERE id=?", (title_en, article_id))


def summaries_without_en() -> list[sqlite3.Row]:
    """当天已有中文纪要、但还没有英文版的 (region, category)。"""
    with connect() as conn:
        return conn.execute(
            """SELECT region, category, ai_text FROM summaries
               WHERE date=? AND ai_text!='' AND (ai_text_en IS NULL OR ai_text_en='')""",
            (today_key(),),
        ).fetchall()


def set_summary_en(region: str, category: str, text_en: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE summaries SET ai_text_en=? WHERE region=? AND category=? AND date=?",
            (text_en, region, category, today_key()),
        )


def events_without_en() -> list[sqlite3.Row]:
    """当天已生成、但还没有英文标题/纪要的事件。"""
    with connect() as conn:
        return conn.execute(
            """SELECT id, title, summary FROM events
               WHERE date=? AND (title_en IS NULL OR title_en='')
               ORDER BY region, rank""",
            (today_key(),),
        ).fetchall()


def set_event_en(event_id: str, title_en: str, summary_en: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE events SET title_en=?, summary_en=? WHERE id=?",
                     (title_en, summary_en, event_id))


def set_excerpt(article_id: str, excerpt: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE articles SET excerpt=? WHERE id=?", (excerpt, article_id))


def set_trivial(article_ids: list[str]) -> int:
    """把这批文章标记为琐碎新闻（其余保持不变）。返回更新条数。"""
    if not article_ids:
        return 0
    with connect() as conn:
        cur = conn.executemany("UPDATE articles SET trivial=1 WHERE id=?",
                               [(i,) for i in article_ids])
        return cur.rowcount


def unjudged_trivial(hours: int = WINDOW_HOURS) -> list[sqlite3.Row]:
    """窗口内尚未做过琐碎判定的文章（trivial 仍为默认值 0 且未记录过判定）。

    判定结果只存 1；为区分「判过=不琐碎」和「没判过」，用 trivial_judged 表记录。
    """
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS trivial_judged (article_id TEXT PRIMARY KEY)")
        wsql, wparams = _window_sql(hours, "a")
        return conn.execute(
            f"""SELECT a.* FROM articles a
               LEFT JOIN trivial_judged j ON j.article_id = a.id
               WHERE {wsql} AND j.article_id IS NULL
               ORDER BY a.published_at DESC""",
            wparams,
        ).fetchall()


def mark_judged(article_ids: list[str]) -> None:
    """记录这批文章已做过琐碎判定，避免下次重复送审。"""
    if not article_ids:
        return
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS trivial_judged (article_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO trivial_judged(article_id) VALUES(?)",
                         [(i,) for i in article_ids])


# ---------- 被过滤条目的留档（供复查误杀） ----------

def record_dropped(items: list[dict]) -> None:
    """记下被标题黑名单拦掉的条目。同一 URL 重复出现只累加 n_seen，不新增行。"""
    if not items:
        return
    now = utc_now()
    with connect() as conn:
        for it in items:
            conn.execute(
                """INSERT INTO dropped_titles
                   (id, source, source_name, title, url, pattern, first_seen, last_seen, n_seen)
                   VALUES (?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(id) DO UPDATE SET
                     n_seen = n_seen + 1, last_seen = excluded.last_seen""",
                (it["id"], it["source"], it["source_name"], it["title"],
                 it["url"], it["pattern"], now, now),
            )


def dropped_by_pattern() -> list[sqlite3.Row]:
    """全部留档条目，按「该规则总共拦了多少条」升序排。

    升序是刻意的：拦了几十条行情页的规则基本不会错，而只命中一两条的规则
    才是误杀高发区，排在最前面最先被看到。
    """
    with connect() as conn:
        return conn.execute(
            """SELECT d.*, c.n_pattern FROM dropped_titles d
               JOIN (SELECT pattern, COUNT(*) n_pattern FROM dropped_titles
                     GROUP BY pattern) c ON c.pattern = d.pattern
               ORDER BY c.n_pattern ASC, d.pattern, d.n_seen DESC, d.title"""
        ).fetchall()


# ---------- 事件（Top5） ----------

def save_events(region: str, events: list[dict]) -> None:
    """覆盖写入某地区当天的 Top 事件。events: [{id,title,summary,article_ids}]"""
    date, now = today_key(), utc_now()
    with connect() as conn:
        old = [r[0] for r in conn.execute(
            "SELECT id FROM events WHERE region=? AND date=?", (region, date))]
        for eid in old:
            conn.execute("DELETE FROM event_articles WHERE event_id=?", (eid,))
        conn.execute("DELETE FROM events WHERE region=? AND date=?", (region, date))
        for rank, e in enumerate(events, 1):
            conn.execute(
                """INSERT INTO events(id,region,date,rank,title,summary,generated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (e["id"], region, date, rank, e["title"], e["summary"], now),
            )
            for aid in e["article_ids"]:
                conn.execute(
                    "INSERT OR IGNORE INTO event_articles(event_id,article_id) VALUES(?,?)",
                    (e["id"], aid),
                )


def top_events(region: str, limit: int = 5, hours: int = WINDOW_HOURS) -> list[dict]:
    """当天 Top 事件 + 其成员文章（成员同样受窗口约束）。"""
    with connect() as conn:
        evs = conn.execute(
            "SELECT * FROM events WHERE region=? AND date=? ORDER BY rank LIMIT ?",
            (region, today_key(), limit),
        ).fetchall()
        wsql, wparams = _window_sql(hours, "a")
        out = []
        for e in evs:
            arts = conn.execute(
                f"""SELECT a.* FROM articles a
                   JOIN event_articles ea ON ea.article_id=a.id
                   WHERE ea.event_id=? AND {wsql}
                   ORDER BY a.published_at DESC""",
                (e["id"], *wparams),
            ).fetchall()
            if arts:
                # 按媒体去重，每家只留最新一篇。首页要展示的是「这件事有几家在报」，
                # 而不是「总共几篇稿」——同一家发四篇不等于四家报道，重复徽章也没有信息量。
                by_src: dict[str, sqlite3.Row] = {}
                for a in arts:                      # arts 已按发布时间倒序
                    by_src.setdefault(a["source"], a)
                # 英文缺失（没跑 --en，或那一步被限流打断）时回退到中文，
                # 保证英文界面不会出现空标题。
                out.append({
                    "title": e["title"], "summary": e["summary"],
                    "title_en": e["title_en"] or e["title"],
                    "summary_en": e["summary_en"] or e["summary"],
                    "articles": arts,
                    "sources": list(by_src.values()),
                    "n_sources": len(by_src),
                })
    return out


# ---------- 学术期刊 ----------
#
# 这一节的函数**不被任何 AI 步骤调用**（translate / classify / events / summarize /
# english 全都只查 articles）。论文与新闻的隔离靠的就是这一点：不是靠过滤条件，
# 而是靠根本不在同一张表里。改动这一节时请保持这个性质。

PAPER_WINDOW_DAYS = 90     # 页面展示窗口：季刊也要有内容，见 journals.py 的实测数据
PAPER_PICK_DAYS = 30       # Top5 取材窗口：90 天下榜单几个月不变，失去意义
PAPER_KEEP_DAYS = 365      # 超出则清理，约 9000 行/年


def paper_cutoff(days: int = PAPER_WINDOW_DAYS) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def upsert_papers(papers: list[dict]) -> int:
    """按 id(DOI 哈希) 去重插入，返回新增条数。

    用 INSERT OR IGNORE 而不是 REPLACE：已经翻译好的 title_zh 不能被后续抓取覆盖掉。
    """
    init()
    n = 0
    with connect() as conn:
        for p in papers:
            cur = conn.execute(
                """INSERT OR IGNORE INTO papers
                   (id, journal_id, journal_name, discipline, sci, title, doi, url,
                    authors, abstract, published_at, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p["id"], p["journal_id"], p["journal_name"], p["discipline"],
                 1 if p["sci"] else 0, p["title"], p["doi"], p["url"],
                 p["authors"], p["abstract"], p["published_at"], p["fetched_at"]))
            n += cur.rowcount
    return n


def papers_by_discipline(discipline: str, days: int = PAPER_WINDOW_DAYS,
                         per_journal: int = 60) -> list[sqlite3.Row]:
    """某学科窗口内的论文，按刊分组后每刊只取最新的 per_journal 篇。

    必须按刊截断：Nature 90 天有近千篇，不截断会把同页的人口学 28 篇彻底淹掉。
    """
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM papers WHERE discipline=? AND published_at>=?
               ORDER BY published_at DESC""",
            (discipline, paper_cutoff(days))).fetchall()
    out, seen = [], {}
    for r in rows:
        k = r["journal_id"]
        seen[k] = seen.get(k, 0) + 1
        if seen[k] <= per_journal:
            out.append(r)
    return out


def paper_discipline_counts(days: int = PAPER_WINDOW_DAYS) -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT discipline d, COUNT(*) n FROM papers
               WHERE published_at>=? GROUP BY discipline""",
            (paper_cutoff(days),)).fetchall()
    return {r["d"]: r["n"] for r in rows}


def papers_untranslated(limit: int) -> list[sqlite3.Row]:
    """待翻译的论文标题，最新的优先。limit 是节流用的——见 journals.TRANSLATE_PER_RUN。"""
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM papers
               WHERE (title_zh IS NULL OR title_zh='') AND published_at>=?
               ORDER BY published_at DESC LIMIT ?""",
            (paper_cutoff(), int(limit))).fetchall()


def papers_untranslated_count() -> int:
    with connect() as conn:
        return conn.execute(
            """SELECT COUNT(*) c FROM papers
               WHERE (title_zh IS NULL OR title_zh='') AND published_at>=?""",
            (paper_cutoff(),)).fetchone()["c"]


def set_paper_title_zh(paper_id: str, title_zh: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE papers SET title_zh=? WHERE id=?", (title_zh, paper_id))


def pick_candidates(sci: bool, days: int = PAPER_PICK_DAYS,
                    per_journal: int = 15) -> list[sqlite3.Row]:
    """Top5 的候选池：该池内各刊最近 days 天的最新 per_journal 篇。"""
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM papers WHERE sci=? AND published_at>=?
               ORDER BY published_at DESC""",
            (1 if sci else 0, paper_cutoff(days))).fetchall()
    out, seen = [], {}
    for r in rows:
        k = r["journal_id"]
        seen[k] = seen.get(k, 0) + 1
        if seen[k] <= per_journal:
            out.append(r)
    return out


def save_picks(pool: str, picks: list[dict]) -> None:
    """覆盖写入某池的五篇。picks: [{paper_id, reason}]"""
    now = utc_now()
    with connect() as conn:
        conn.execute("DELETE FROM paper_picks WHERE pool=?", (pool,))
        for i, p in enumerate(picks, 1):
            conn.execute(
                """INSERT INTO paper_picks(pool, rank, paper_id, reason, generated_at)
                   VALUES (?,?,?,?,?)""",
                (pool, i, p["paper_id"], p["reason"], now))


def latest_picks(pool: str) -> list[dict]:
    """某池的五篇 + 论文全文字段。论文已被清理时自动跳过，不会渲染出死链。"""
    with connect() as conn:
        rows = conn.execute(
            """SELECT k.rank, k.reason, k.reason_en, k.generated_at, p.*
               FROM paper_picks k JOIN papers p ON p.id = k.paper_id
               WHERE k.pool=? ORDER BY k.rank""", (pool,)).fetchall()
    return [dict(r) for r in rows]


def picks_stale(pool: str, hours: int = 24) -> bool:
    with connect() as conn:
        r = conn.execute(
            "SELECT MAX(generated_at) g FROM paper_picks WHERE pool=?", (pool,)).fetchone()
    return not r or not r["g"] or r["g"] < window_cutoff(hours)


def journal_metrics() -> dict[str, dict]:
    with connect() as conn:
        return {r["journal_id"]: dict(r)
                for r in conn.execute("SELECT * FROM journal_metrics")}


def save_journal_metrics(rows: list[dict]) -> None:
    now = utc_now()
    with connect() as conn:
        for r in rows:
            conn.execute(
                """INSERT OR REPLACE INTO journal_metrics
                   (journal_id, mean_citedness, h_index, works_count, refreshed_at)
                   VALUES (?,?,?,?,?)""",
                (r["journal_id"], r["mean_citedness"], r["h_index"],
                 r["works_count"], now))


def metrics_stale(days: int = 30) -> bool:
    with connect() as conn:
        r = conn.execute("SELECT MIN(refreshed_at) g FROM journal_metrics").fetchone()
    return not r or not r["g"] or r["g"] < paper_cutoff(days)


def prune_papers(days: int = PAPER_KEEP_DAYS) -> int:
    """清掉过老的论文。被选进 paper_picks 的一律保留，否则页面上的五篇会变成死链。"""
    with connect() as conn:
        cur = conn.execute(
            """DELETE FROM papers WHERE published_at < ?
               AND id NOT IN (SELECT paper_id FROM paper_picks)""",
            (paper_cutoff(days),))
        return cur.rowcount
