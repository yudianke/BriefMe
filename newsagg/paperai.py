"""学术期刊的 AI 环节：标题翻译 + 两个五篇精选。

单独成一个模块，是为了让「论文不碰新闻流水线」这件事在文件层面就看得出来：
translate.py / classify.py / events.py / summarize.py 只查 articles，
本模块只查 papers，两边没有交集。
"""
from __future__ import annotations

from . import db
from .ai import complete, is_daily_limit, parse_json

# ---------- 标题翻译 ----------

BATCH = 25
MAX_TOK = 2000

# 每轮最多翻译多少篇。这个节流是必需的：首次抓完有约一千篇待译，
# 一次翻完要 40 多次请求、约 6 万 token，会把 Groq 免费档每天 20 万的额度
# 挤掉三成，新闻部分就不够用了。分几轮补齐，稳态下每天只有几十篇新论文。
TRANSLATE_PER_RUN = 300

_T_SYSTEM = (
    "你是学术论文标题翻译。把每条英文论文标题翻译成准确的中文标题。\n"
    "要求：保留学科术语的通行中文译法；人名、基因名、模型名、数据集名等专有名词"
    "若无公认译名就保留英文原文；不加评论、不扩写、不加书名号；"
    "冒号分隔的主副标题结构保持不变。\n"
    '严格只输出 JSON：{"items":[{"id":"短ID","zh":"中文标题"}]}'
)


def translate_papers(limit: int = TRANSLATE_PER_RUN) -> int:
    """翻译论文标题。返回本轮完成条数。"""
    total = db.papers_untranslated_count()
    if not total:
        print("  论文标题：无待翻译。")
        return 0
    rows = db.papers_untranslated(limit)
    done = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        id_map = {r["id"][:12]: r["id"] for r in batch}
        listing = "\n".join(f"{r['id'][:12]}\t{r['title']}" for r in batch)
        user = f"把下面每行「短ID<tab>英文论文标题」翻译为中文：\n{listing}"
        try:
            text = complete("translate", _T_SYSTEM, user,
                            max_tokens=MAX_TOK, temperature=0).text
        except Exception as e:
            # 日额度耗尽时后面每一批都会同样失败，接着循环只是刷屏
            if is_daily_limit(e):
                print(f"    论文翻译因当日额度耗尽中止，已完成 {done} 条，明日续。")
                break
            print(f"    [论文翻译批 {i // BATCH + 1} 失败] {type(e).__name__}: {e}")
            continue
        for item in (parse_json(text) or {}).get("items", []):
            full = id_map.get(str(item.get("id", "")))
            zh = (item.get("zh") or "").strip()
            if full and zh:
                db.set_paper_title_zh(full, zh)
                done += 1
    left = db.papers_untranslated_count()
    if left:
        # 让积压可见：否则用户只会看到「翻译了 300 条」，不知道还差多少轮
        print(f"  论文标题翻译 {done}/{total} 条，剩余 {left} 条，"
              f"约再跑 {-(-left // max(limit, 1))} 轮补齐。")
    else:
        print(f"  论文标题翻译 {done} 条，已全部补齐。")
    return done


# ---------- SCI / SSCI 五篇精选 ----------

PICK_MAX_TOK = 1400
POOLS = {"sci": ("SCI（自然科学与工程）", True),
         "ssci": ("SSCI（社会科学）", False)}

# 这是**选择题**，不是写作题——模型只从给定候选里挑，理由必须落在标题/摘要之内。
# 之前撤掉的「要闻简报」失败在让模型凭一行标题写叙述，必然要填空；
# 这里的输出形态从根上避开了那个问题，再加上 DOI 必须命中候选集合的程序性校验。
_P_SYSTEM = (
    "你在为一个跨学科读者挑选值得一读的近期论文。\n"
    "\n"
    "从给定候选中选出 5 篇，标准依次是：\n"
    "  1) 结论本身的重要性——是否推翻/确立了某个认识，而不是渐进改良；\n"
    "  2) 跨学科可读性——非本领域的人也能理解它为什么重要；\n"
    "  3) 学科分散——尽量不要 5 篇都来自同一本刊或同一个方向。\n"
    "\n"
    "**每篇写一句不超过 45 字的推荐理由，只能复述标题和摘要里已有的信息。**\n"
    "材料里约七成条目只有标题没有摘要，对这些只需用中文把标题的主张说清楚即可，"
    "**不许补充研究方法、样本量、数据、结论数字、作者单位等任何未写明的内容**——"
    "补出来的都是假的。不要写「意义重大」「值得关注」这类空话。\n"
    "\n"
    "理由用简体中文。id 必须原样抄自候选列表，不得改写或杜撰。\n"
    '严格只输出 JSON：{"picks":[{"id":"短ID","reason":"中文理由"}]}'
)


def _candidate_block(rows: list) -> str:
    lines = []
    for r in rows:
        head = f"{r['id'][:12]}\t[{r['journal_name']}] {r['title']}"
        if r["abstract"]:
            lines.append(f"{head}\n    摘要：{r['abstract'][:200]}")
        else:
            lines.append(f"{head}\n    （仅标题，无摘要）")
    return "\n".join(lines)


def pick(pool: str, force: bool = False) -> int:
    """为一个池选出五篇。返回写入条数（0 表示跳过或失败，此时保留上一版）。"""
    label, sci = POOLS[pool]
    if not force and not db.picks_stale(pool):
        print(f"  {label} 精选：24h 内已生成，沿用上一份。")
        return 0

    rows = db.pick_candidates(sci)
    if len(rows) < 5:
        print(f"  {label} 精选：候选只有 {len(rows)} 篇，不足 5 篇，跳过。")
        return 0

    id_map = {r["id"][:12]: r["id"] for r in rows}
    n_no_abs = sum(1 for r in rows if not r["abstract"])
    user = (f"以下是最近 {db.PAPER_PICK_DAYS} 天的候选论文，共 {len(rows)} 篇，"
            f"其中 {n_no_abs} 篇**只有标题、没有摘要**。\n\n"
            + _candidate_block(rows)
            + "\n\n请选出 5 篇并给出推荐理由。")
    try:
        text = complete("summarize", _P_SYSTEM, user,
                        max_tokens=PICK_MAX_TOK, temperature=0.2).text
    except Exception as e:
        print(f"  [{label} 精选失败] {type(e).__name__}: {e}")
        return 0

    raw = (parse_json(text) or {}).get("picks") or []
    picks, bad = [], []
    for p in raw:
        sid = str(p.get("id", "")).strip()
        full = id_map.get(sid)
        reason = (p.get("reason") or "").strip()
        if not full:
            bad.append(sid)          # 模型编了一个不存在的 id
            continue
        if any(x["paper_id"] == full for x in picks):
            continue                 # 同一篇挑了两次
        picks.append({"paper_id": full, "reason": reason[:60]})

    # 只要出现杜撰的 id 就整批作废：宁可页面上留着昨天那五篇，
    # 也不要展示一条指向不存在论文的推荐。同 events.py 的处理。
    if bad:
        print(f"  [{label} 精选作废] 模型给出 {len(bad)} 个候选集合里没有的 id"
              f"（{', '.join(bad[:3])}…），保留上一份。")
        return 0
    if len(picks) < 5:
        print(f"  [{label} 精选作废] 只解析出 {len(picks)} 篇，保留上一份。"
              f"原文：{text[:100]!r}")
        return 0

    db.save_picks(pool, picks[:5])
    print(f"  {label} 精选已生成（候选 {len(rows)} 篇 -> 5 篇）")
    return 5


def pick_all(force: bool = False) -> int:
    return sum(pick(p, force) for p in POOLS)


if __name__ == "__main__":
    import sys
    if "--translate" in sys.argv:
        translate_papers()
    elif "--picks" in sys.argv:
        pick_all(force="--force" in sys.argv)
    else:
        translate_papers()
        pick_all(force="--force" in sys.argv)
