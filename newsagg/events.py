"""首页 Top5：按「事件」而非单篇文章聚合。

同一事件被 Reuters/AP/BBC 等多家报道时只占一个位置，并记录全部来源。
排序综合：事件重要性、报道媒体数量、来源多样性、时效性。
"""
from __future__ import annotations

import hashlib
import time

from . import db
from .ai import complete, parse_json
from .models import REGIONS, WINDOW_HOURS

CANDIDATES = 90   # 送给模型的候选文章数（窗口内最新的）
TOP_N = 5
DIVERSITY_W = 1.5  # 排序时每多一家不同媒体，等效前移的名次数（越大越看重来源多样性）

_SYSTEM = (
    "你是新闻主编，从过去 24 小时的报道中选出最重要的 5 个**事件**。\n"
    "\n"
    "第一步：跨媒体合并。**优先把不同媒体对同一事件的报道合并成一个事件**。\n"
    "  - 判断是否同一事件，看的是「同一件事实」，而不是措辞是否相同：不同媒体\n"
    "    对同一事件的标题往往用词、角度、详略都不同（中英文标题更是如此），\n"
    "    只要讲的是同一起事件，就必须合并。\n"
    "  - 合并前请主动回扫整个列表：为每个候选事件找齐**所有**报道它的媒体，\n"
    "    不要只合并连续几条或同一家媒体的多篇。\n"
    "  - 常见错误：把同一家媒体的多篇报道聚成一个事件，却漏掉了其他媒体对\n"
    "    同一事件的报道。务必避免。\n"
    "  - 同一事件只能占一个名额，其 ids 要列出全部相关文章的短ID。\n"
    "\n"
    "第二步：排序。综合考虑，并**给来源多样性较高的权重**：\n"
    "  - 来源多样性（报道该事件的**不同媒体家数**）——权重最高，家数越多越靠前；\n"
    "  - 事件本身的重要性；\n"
    "  - 报道总篇数；\n"
    "  - 时效性。\n"
    "  在重要性相近时，**由 3 家不同媒体报道的事件应排在只有 1 家报道的事件之前**；\n"
    "  来自 4 家不同媒体的事件，优先于同一家媒体发了 4 篇的事件。\n"
    "\n"
    "第三步：每个事件给一个中文标题（不超过 30 字）和一句事实纪要，"
    "写清谁做了什么、发生了什么、结果如何。禁止评论、预测和套话。\n"
    '严格只输出 JSON：{"events":[{"title":"...","summary":"...","ids":["短ID"]}]}'
)


def build(region: str, hours: int = WINDOW_HOURS) -> int:
    rows = db.recent_articles(hours, region, limit=CANDIDATES)
    if not rows:
        print(f"  {REGIONS[region]}：窗口内无文章，跳过 Top5。")
        return 0
    id_map = {r["id"][:12]: r["id"] for r in rows}
    lines = []
    for r in rows:
        # 有中文译题时一并给出，帮助模型合并同一事件
        t = r["title"]
        if r["title_zh"]:
            t = f"{t} / {r['title_zh']}"
        lines.append(f"{r['id'][:12]}\t[{r['source_name']}]\t{t}")
    user = (f"以下是过去 {hours} 小时 {REGIONS[region]} 的报道"
            f"（短ID<tab>来源<tab>标题）：\n" + "\n".join(lines) +
            f"\n\n请选出最重要的 {TOP_N} 个事件。")
    try:
        text = complete("events", _SYSTEM, user, max_tokens=1600, temperature=0.2).text
    except Exception as e:
        print(f"  [Top5 {REGIONS[region]} 失败] {type(e).__name__}: {e}")
        return 0

    data = parse_json(text) or {}
    src_of = {r["id"]: r["source_name"] for r in rows}
    events = []
    used: set[str] = set()
    for model_rank, e in enumerate(data.get("events", [])[:TOP_N]):
        title = (e.get("title") or "").strip()
        summary = (e.get("summary") or "").strip()
        ids = [id_map[str(s)] for s in (e.get("ids") or []) if str(s) in id_map]
        ids = [i for i in ids if i not in used]      # 防止同一文章跨事件重复
        if not title or not ids:
            continue
        used.update(ids)
        eid = hashlib.sha1(f"{region}|{db.today_key()}|{title}".encode()).hexdigest()[:16]
        events.append({
            "id": eid, "title": title, "summary": summary, "article_ids": ids,
            "model_rank": model_rank,
            "n_sources": len({src_of.get(i) for i in ids}),
        })

    # 排序层兑现「来源多样性权重」：模型给的重要性顺序仍是主信号，
    # 但每多一家不同媒体报道，相当于向前挪 DIVERSITY_W 个名次。
    # 这样「4 家媒体各 1 篇」会排在「同一家媒体 4 篇」之前。
    events.sort(key=lambda e: -((TOP_N - e["model_rank"])
                                + DIVERSITY_W * (e["n_sources"] - 1)))

    if events:
        db.save_events(region, events)
    detail = "，".join(f"{e['n_sources']}家" for e in events)
    print(f"  {REGIONS[region]} Top{len(events)} 事件已生成"
          f"（覆盖 {sum(len(e['article_ids']) for e in events)} 篇报道；来源家数：{detail}）")
    return len(events)


def build_all(hours: int = WINDOW_HOURS) -> int:
    n = 0
    for region in REGIONS:
        n += build(region, hours)
        time.sleep(1)
    return n


if __name__ == "__main__":
    build_all()
