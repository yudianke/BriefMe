"""外文标题 -> 中文译题。

原则：**只新增 title_zh，绝不修改 title**。前端把英文原题作为主标题展示，
中文译题作为副标题；中文源不翻译。
"""
from __future__ import annotations


from . import db
from .ai import complete, is_daily_limit, parse_json

BATCH = 25
MAX_TOK = 1800

_SYSTEM = (
    "你是新闻标题翻译。把每条英文新闻标题翻译成简洁、准确的中文标题，"
    "保留人名/地名/机构的通用中文译名，不加评论、不扩写、不加书名号。"
    '严格只输出 JSON：{"items":[{"id":"短ID","zh":"中文标题"}]}'
)


def translate(hours: int = 24) -> int:
    rows = db.untranslated(hours)
    if not rows:
        print("没有待翻译的标题。")
        return 0
    done = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        id_map = {r["id"][:12]: r["id"] for r in batch}
        listing = "\n".join(f"{r['id'][:12]}\t{r['title']}" for r in batch)
        user = f"把下面每行「短ID<tab>英文标题」翻译为中文：\n{listing}"
        try:
            text = complete("translate", _SYSTEM, user, max_tokens=MAX_TOK, temperature=0).text
        except Exception as e:
            # 日额度耗尽时后面每一批都会同样失败，接着循环只是刷屏
            if is_daily_limit(e):
                print(f"  翻译因当日额度耗尽中止，已完成 {done} 条，明日续。")
                break
            print(f"  [翻译批 {i//BATCH + 1} 失败] {type(e).__name__}: {e}")
            continue
        data = parse_json(text) or {}
        for item in data.get("items", []):
            full = id_map.get(str(item.get("id", "")))
            zh = (item.get("zh") or "").strip()
            if full and zh:
                db.set_title_zh(full, zh)
                done += 1
        print(f"  翻译批 {i//BATCH + 1}: {len(batch)} 条")
    print(f"翻译完成，共 {done} 条中文译题。")
    return done


if __name__ == "__main__":
    translate()
