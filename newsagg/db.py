"""SQLite 读写、去重，以及**统一的 24 小时滚动窗口**查询层。

规则（全局强制）：UI 与 AI 输入只能看到 `published_at`（媒体发布时间，UTC）
落在「当前时间往前 WINDOW_HOURS 小时」内的新闻。数据库长期保留历史，
但所有对外查询都必须走本模块的 recent_* 函数，不要自己写 SELECT。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import DDL, WINDOW_HOURS, Article

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "news.db"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(DDL)
        # 轻量迁移：老库的 articles 可能没有 title_zh 列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
        if "title_zh" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN title_zh TEXT DEFAULT ''")
        if "trivial" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN trivial INTEGER DEFAULT 0")


def window_cutoff(hours: int = WINDOW_HOURS) -> str:
    """滚动窗口起点（UTC ISO8601）。所有查询共用同一口径。"""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


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
    sql = "SELECT * FROM articles WHERE published_at >= ?"
    params: list = [window_cutoff(hours)]
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
    sql = """SELECT a.* FROM articles a
             JOIN article_categories c ON c.article_id = a.id
             WHERE a.region=? AND c.category=? AND a.published_at>=?
             ORDER BY a.published_at DESC"""
    params: list = [region, category, window_cutoff(hours)]
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def category_counts(region: str, hours: int = WINDOW_HOURS) -> dict[str, int]:
    """窗口内各分类计数（用于分类网格）。"""
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.category cat, COUNT(*) n FROM articles a
               JOIN article_categories c ON c.article_id = a.id
               WHERE a.region=? AND a.published_at>=?
               GROUP BY c.category""",
            (region, window_cutoff(hours)),
        ).fetchall()
    return {r["cat"]: r["n"] for r in rows}


def untranslated(hours: int = WINDOW_HOURS) -> list[sqlite3.Row]:
    """窗口内需要中文译题、且尚未翻译的外文文章。"""
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM articles
               WHERE published_at>=? AND lang!='zh'
                 AND (title_zh IS NULL OR title_zh='')
               ORDER BY published_at DESC""",
            (window_cutoff(hours),),
        ).fetchall()


def set_title_zh(article_id: str, title_zh: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE articles SET title_zh=? WHERE id=?", (title_zh, article_id))


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
        return conn.execute(
            """SELECT a.* FROM articles a
               LEFT JOIN trivial_judged j ON j.article_id = a.id
               WHERE a.published_at >= ? AND j.article_id IS NULL
               ORDER BY a.published_at DESC""",
            (window_cutoff(hours),),
        ).fetchall()


def mark_judged(article_ids: list[str]) -> None:
    """记录这批文章已做过琐碎判定，避免下次重复送审。"""
    if not article_ids:
        return
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS trivial_judged (article_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO trivial_judged(article_id) VALUES(?)",
                         [(i,) for i in article_ids])


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
        out = []
        for e in evs:
            arts = conn.execute(
                """SELECT a.* FROM articles a
                   JOIN event_articles ea ON ea.article_id=a.id
                   WHERE ea.event_id=? AND a.published_at>=?
                   ORDER BY a.published_at DESC""",
                (e["id"], window_cutoff(hours)),
            ).fetchall()
            if arts:
                out.append({"title": e["title"], "summary": e["summary"], "articles": arts})
    return out
