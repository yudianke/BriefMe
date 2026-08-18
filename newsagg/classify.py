"""AI 分类：分级策略。

第一级：仅凭标题分类。若标题含义模糊 / 无法可靠判断，模型返回 uncertain，
        **不允许强行分类**。
第二级：对 uncertain 的文章，补充其公开 preview / 正文开头，再判一次。
仍判不出的，才落到兜底分类。
"""
from __future__ import annotations


from . import db, extract
from .ai import complete, is_daily_limit, parse_json
from .models import CATEGORIES, FALLBACK_CATEGORY

BATCH = 30        # 第一级每批标题数
BATCH2 = 10       # 第二级带正文，单批更小
MAX_TOK = 1800

_SYSTEM1 = (
    "你是新闻分类助手。仅根据标题，把每篇新闻归入 1-2 个最贴切的主题分类。"
    "只能使用给定分类，不要新增。"
    "如果标题过于模糊、缺乏信息，或你无法可靠判断，请把该篇标记为 uncertain=true "
    "并让 categories 为空——不要猜测、不要强行分类。"
    '严格只输出 JSON：{"assignments":[{"id":"短ID","categories":["分类"],"uncertain":false}]}'
)

_SYSTEM2 = (
    "你是新闻分类助手。下面每篇给出了标题和正文开头，请据此归入 1-2 个最贴切的分类。"
    "只能使用给定分类。现在信息更充分，请尽量给出判断。"
    '严格只输出 JSON：{"assignments":[{"id":"短ID","categories":["分类"]}]}'
)


def _unclassified(hours: int = 24) -> list:
    rows = db.recent_articles(hours)
    with db.connect() as conn:
        done = {r[0] for r in conn.execute("SELECT DISTINCT article_id FROM article_categories")}
    return [r for r in rows if r["id"] not in done]


def _save(article_id: str, cats: list[str]) -> int:
    n = 0
    with db.connect() as conn:
        for c in cats:
            if c in CATEGORIES:
                conn.execute(
                    "INSERT OR IGNORE INTO article_categories(article_id,category) VALUES(?,?)",
                    (article_id, c))
                n += 1
    return n


def classify(hours: int = 24) -> int:
    rows = _unclassified(hours)
    if not rows:
        print("没有待分类的文章。")
        return 0
    cats_str = "、".join(CATEGORIES)
    total = 0
    uncertain: list = []

    # ---- 第一级：仅标题 ----
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        id_map = {r["id"][:12]: r for r in batch}
        listing = "\n".join(f"{r['id'][:12]}\t{r['title']}" for r in batch)
        user = (f"可用分类：{cats_str}\n\n"
                f"每行是「短ID<tab>标题」，为每篇返回分类；判不准的标 uncertain：\n{listing}")
        try:
            text = complete("classify", _SYSTEM1, user, max_tokens=MAX_TOK, temperature=0).text
        except Exception as e:
            # 日额度耗尽时后面每一批都会同样失败，接着循环只是刷屏
            if is_daily_limit(e):
                print(f"  分类因当日额度耗尽中止，已处理 {i} 篇，明日续。")
                break
            print(f"  [分类批 {i//BATCH + 1} 失败] {type(e).__name__}: {e}")
            continue
        data = parse_json(text) or {}
        got = {}
        for a in data.get("assignments", []):
            got[str(a.get("id", ""))] = a
        for short, row in id_map.items():
            a = got.get(short) or {}
            cats = [c for c in (a.get("categories") or []) if c in CATEGORIES]
            if a.get("uncertain") or not cats:
                uncertain.append(row)          # 交给第二级，不强行分类
            else:
                total += _save(row["id"], cats[:2])
        print(f"  [一级] 批 {i//BATCH + 1}: {len(batch)} 篇，累计待补正文 {len(uncertain)}")

    # ---- 第二级：补正文开头再判 ----
    if uncertain:
        print(f"  [二级] {len(uncertain)} 篇标题信息不足，补充正文开头后重判…")
    for i in range(0, len(uncertain), BATCH2):
        batch = uncertain[i:i + BATCH2]
        parts, id_map = [], {}
        for r in batch:
            body = extract.backfill_excerpt(r)
            if body and body != (r["excerpt"] or ""):
                db.set_excerpt(r["id"], body)
            id_map[r["id"][:12]] = r
            parts.append(f"{r['id'][:12]}\t标题：{r['title']}\n\t正文开头：{body or '（暂无）'}")
        user = f"可用分类：{cats_str}\n\n{chr(10).join(parts)}"
        try:
            text = complete("classify", _SYSTEM2, user, max_tokens=MAX_TOK, temperature=0).text
            data = parse_json(text) or {}
            got = {str(a.get("id", "")): a for a in data.get("assignments", [])}
        except Exception as e:
            # 日额度耗尽：直接跳出，**不要**落到下面的兜底分类。
            # 下面那段在拿不到结果时会把文章塞进 FALLBACK_CATEGORY，一旦写进库
            # 就等于永久定成「社会」，明天也不会再重判了。留着不分类，下一轮能正常判。
            if is_daily_limit(e):
                print(f"  二级分类因当日额度耗尽中止，剩余 {len(uncertain) - i} 篇留待明日重判。")
                break
            print(f"  [二级批 {i//BATCH2 + 1} 失败] {type(e).__name__}: {e}")
            got = {}
        for short, row in id_map.items():
            cats = [c for c in ((got.get(short) or {}).get("categories") or []) if c in CATEGORIES]
            total += _save(row["id"], cats[:2] or [FALLBACK_CATEGORY])
        print(f"  [二级] 批 {i//BATCH2 + 1}: {len(batch)} 篇")

    print(f"分类完成，共写入 {total} 条(篇×类)标签。")
    return total


if __name__ == "__main__":
    classify()
