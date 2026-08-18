"""AI 汇总：按 (地区,分类) 生成「事件纪要」式汇总。

风格要求：先合并多家媒体报道的同一事件，再以「谁做了什么、发生了什么、结果如何」
为核心写事实纪要。禁止评论、预测、空泛背景与 AI 套话。
"""
from __future__ import annotations


from . import db
from .ai import complete, is_daily_limit
from .models import CATEGORIES, REGIONS, WINDOW_HOURS

_SYSTEM = (
    "你是通讯社编辑，负责写当日事件纪要。\n"
    "**必须全部用简体中文输出**：即使原始材料是英文，也要用中文转述"
    "（人名、地名、机构用通用中文译名）。\n"
    "步骤：1) 先把多家媒体报道的同一事件合并为一条，不要重复计数；"
    "2) 按事件重要性排序；3) 每个事件写 1-2 句事实纪要。\n"
    "每句必须包含：谁做了什么、发生了什么、结果或最新进展如何。"
    "多家媒体报道同一事件时，可在句末用括号标注来源，如（路透/AP/BBC）。\n"
    "严格禁止：评论、观点、预测、影响分析、空泛背景铺垫，以及"
    "「值得关注的是」「引发广泛关注」「彰显」「与此同时」「总体来看」"
    "「共识在于」「分歧则」这类套话。不要写开场白和结语。\n"
    "只依据给出的材料，不得臆造事实、数字或因果。"
    "输出 3-6 条，每条一行，以「· 」开头，全文不超过 400 字。"
)


def summarize(hours: int = WINDOW_HOURS, force: bool = False) -> int:
    today = db.today_key()
    now_iso = db.utc_now()
    made = 0
    # 待写清单：没写过的，加上写过但之后又抓到新文章的（纪要按日期缓存、
    # 窗口却是滚动 24 小时，不重算就会一直停在当天第一次运行的素材上）
    todo = None if force else set(db.summaries_todo(hours))
    for region in REGIONS:
        for cat in CATEGORIES:
            arts = db.recent_by_category(region, cat, hours)
            if not arts:
                continue
            if todo is not None and (region, cat) not in todo:
                continue
            listing = "\n".join(
                f"- [{a['source_name']}] {a['title']}"
                + (f"｜{a['excerpt'][:100]}" if a["excerpt"] else "")
                for a in arts[:30]
            )
            user = (f"主题：{cat}（{REGIONS[region]}）\n"
                    f"过去 {hours} 小时各媒体报道：\n{listing}")
            try:
                text = complete("summarize", _SYSTEM, user,
                                max_tokens=900, temperature=0.2).text.strip()
            except Exception as e:
                # 日额度耗尽：剩下十几个分类会以同样理由逐个失败，
                # 继续循环只是刷屏（实测一次跑连撞 12 次）。直接收工，已写的照常渲染。
                if is_daily_limit(e):
                    print(f"  汇总因当日额度耗尽中止，本次已生成 {made} 段，明日续。")
                    return made
                print(f"  [汇总 {REGIONS[region]}·{cat} 失败] {type(e).__name__}: {e}")
                continue
            with db.connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO summaries(region,category,date,ai_text,generated_at)
                       VALUES(?,?,?,?,?)""",
                    (region, cat, today, text, now_iso),
                )
            made += 1
            print(f"  汇总 {REGIONS[region]}·{cat}（{len(arts)} 篇）")
    print(f"汇总完成，本次生成 {made} 段。")
    return made


if __name__ == "__main__":
    summarize()
