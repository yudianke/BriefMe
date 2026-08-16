"""英文界面的 AI 内容（仅 `--en` 模式运行）。

做法是**翻译已生成的中文结果**，而不是用英文把 AI 环节再跑一遍。原因：

1. 一致性。Top5 若分别用中英各选一次，两次选出的事件和排序几乎必然不同，
   读者一按切换按钮就会看到两份对不上的榜单。翻译则保证中英是同一批事件、
   同一个顺序。分类纪要同理。
2. 成本。翻译的输入是几百字的成稿，而重跑要把上百条候选标题再送一遍。

三件事，各自独立、可分别中断重跑：
  1) 中文报道 -> 英文译题（title_en，与 translate.py 完全对称）
  2) 分类纪要 -> 英文版（summaries.ai_text_en）
  3) Top5 事件标题与纪要 -> 英文版（events.title_en / summary_en）

任何一步失败都只是「英文缺一块」，页面会自动回退显示中文，不影响中文界面。
"""
from __future__ import annotations

import time

from . import db
from .ai import complete, parse_json
from .models import WINDOW_HOURS

TITLE_BATCH = 25       # 标题短，一批 25 条
TITLE_MAX_TOK = 1800
TEXT_CHAR_BUDGET = 1800  # 纪要较长，按字数攒批，避免单次请求过大被限流
TEXT_MAX_TOK = 2200

_TITLE_SYSTEM = (
    "You translate Chinese news headlines into English. "
    "Produce a concise, accurate English headline in news style. "
    "Use the conventional English names of people, places and organisations. "
    "Do not editorialise, do not expand, do not add quotation marks.\n"
    'Output JSON only: {"items":[{"id":"shortID","en":"English headline"}]}'
)

_TEXT_SYSTEM = (
    "You translate Chinese news briefs into English for a news digest.\n"
    "Rules:\n"
    "- Keep the exact line structure: one line per bullet, each starting with '- '.\n"
    "- Keep source attributions in brackets as they are, converting outlet names to "
    "their usual English form (路透 -> Reuters, 新华社 -> Xinhua, 央视 -> CCTV, "
    "财新 -> Caixin, 第一财经 -> Yicai, 参考消息 -> Cankao Xiaoxi, "
    "南方周末 -> Southern Weekly, 联合早报 -> Lianhe Zaobao).\n"
    "- Plain factual news English. Do not add, drop, soften or comment on anything.\n"
    'Output JSON only: {"items":[{"id":"ID","en":"translated text"}]}'
)

_EVENT_SYSTEM = (
    "You translate Chinese news items into English for a news digest.\n"
    "Each item has a short headline and a one-sentence factual brief. "
    "Translate both into plain news English, keeping them equally short. "
    "Use conventional English names of people, places and organisations. "
    "Do not add or drop information.\n"
    'Output JSON only: {"items":[{"id":"ID","title":"...","summary":"..."}]}'
)


def _batches_by_chars(rows: list, text_of, budget: int = TEXT_CHAR_BUDGET):
    """按字符预算攒批：长文本不能像标题那样固定条数，否则单批体积失控。"""
    batch, size = [], 0
    for r in rows:
        n = len(text_of(r))
        if batch and size + n > budget:
            yield batch
            batch, size = [], 0
        batch.append(r)
        size += n
    if batch:
        yield batch


# ----------------------------------------------------------------- 1. 译题

def titles(hours: int = WINDOW_HOURS) -> int:
    rows = db.untranslated_en(hours)
    if not rows:
        print("  英文译题：无待翻译条目。")
        return 0
    done = 0
    for i in range(0, len(rows), TITLE_BATCH):
        batch = rows[i:i + TITLE_BATCH]
        id_map = {r["id"][:12]: r["id"] for r in batch}
        listing = "\n".join(f"{r['id'][:12]}\t{r['title']}" for r in batch)
        user = f"Translate each line (shortID<tab>Chinese headline) into English:\n{listing}"
        try:
            text = complete("translate", _TITLE_SYSTEM, user,
                            max_tokens=TITLE_MAX_TOK, temperature=0).text
        except Exception as e:
            print(f"  [英文译题批 {i // TITLE_BATCH + 1} 失败] {type(e).__name__}: {e}")
            continue
        for item in (parse_json(text) or {}).get("items", []):
            full = id_map.get(str(item.get("id", "")))
            en = (item.get("en") or "").strip()
            if full and en:
                db.set_title_en(full, en)
                done += 1
        print(f"  英文译题批 {i // TITLE_BATCH + 1}: {len(batch)} 条")
        time.sleep(1)
    print(f"  英文译题完成，共 {done} 条。")
    return done


# ----------------------------------------------------------------- 2. 分类纪要

def summaries() -> int:
    rows = db.summaries_without_en()
    if not rows:
        print("  英文纪要：无待翻译条目。")
        return 0
    done = 0
    for n, batch in enumerate(_batches_by_chars(rows, lambda r: r["ai_text"]), 1):
        # 用 region|category 当 ID：本身就唯一，回写时不必再查表
        id_map = {f"{r['region']}|{r['category']}": r for r in batch}
        blocks = "\n\n".join(f"### {k}\n{r['ai_text']}" for k, r in id_map.items())
        user = ("Translate each block below into English. "
                "The heading line (### region|category) is the item's ID.\n\n" + blocks)
        try:
            text = complete("translate", _TEXT_SYSTEM, user,
                            max_tokens=TEXT_MAX_TOK, temperature=0).text
        except Exception as e:
            print(f"  [英文纪要批 {n} 失败] {type(e).__name__}: {e}")
            continue
        for item in (parse_json(text) or {}).get("items", []):
            row = id_map.get(str(item.get("id", "")))
            en = (item.get("en") or "").strip()
            if row is not None and en:
                db.set_summary_en(row["region"], row["category"], en)
                done += 1
        print(f"  英文纪要批 {n}: {len(batch)} 段")
        time.sleep(1)
    print(f"  英文纪要完成，共 {done} 段。")
    return done


# ----------------------------------------------------------------- 3. Top5 事件

def events() -> int:
    rows = db.events_without_en()
    if not rows:
        print("  英文 Top5：无待翻译条目。")
        return 0
    done = 0
    for n, batch in enumerate(
            _batches_by_chars(rows, lambda r: r["title"] + r["summary"]), 1):
        id_map = {r["id"]: r for r in batch}
        blocks = "\n\n".join(
            f"### {r['id']}\ntitle: {r['title']}\nsummary: {r['summary']}" for r in batch)
        user = ("Translate each item below into English. "
                "The heading line (### ID) is the item's ID.\n\n" + blocks)
        try:
            text = complete("translate", _EVENT_SYSTEM, user,
                            max_tokens=TEXT_MAX_TOK, temperature=0).text
        except Exception as e:
            print(f"  [英文 Top5 批 {n} 失败] {type(e).__name__}: {e}")
            continue
        for item in (parse_json(text) or {}).get("items", []):
            row = id_map.get(str(item.get("id", "")))
            title_en = (item.get("title") or "").strip()
            if row is not None and title_en:
                db.set_event_en(row["id"], title_en, (item.get("summary") or "").strip())
                done += 1
        print(f"  英文 Top5 批 {n}: {len(batch)} 条")
        time.sleep(1)
    print(f"  英文 Top5 完成，共 {done} 条。")
    return done


def run_all(hours: int = WINDOW_HOURS) -> int:
    """三步依次执行；单步异常不影响其余两步（缺的部分页面回退中文）。"""
    total = 0
    for label, fn in (("译题", lambda: titles(hours)),
                      ("分类纪要", summaries),
                      ("Top5 事件", events)):
        try:
            total += fn()
        except Exception as e:
            print(f"  [英文{label} 中断] {type(e).__name__}: {e}")
    return total


if __name__ == "__main__":
    run_all()
