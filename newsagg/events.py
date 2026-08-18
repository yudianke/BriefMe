"""首页 Top5：按「事件」而非单篇文章聚合。

同一事件被 Reuters/AP/BBC 等多家报道时只占一个位置，并记录全部来源。
排序综合：事件重要性、报道媒体数量、来源多样性、时效性。
"""
from __future__ import annotations

import hashlib

from . import db
from .ai import complete, parse_json
from .models import REGIONS, WINDOW_HOURS

CANDIDATES = 90   # 送给模型的候选文章数（窗口内最新的）
# 为什么窗口从 24h 变成 48h 后这个数**没有**跟着翻倍：Groq 的 TPM 上限是 8000，
# 而 max_tokens 是预扣的。实测国际区候选带中文译题，输入本就比国内区大一倍——
# 90 条约 4451 token，150 条会涨到 7658，加上预扣的 1600 就是 9258，直接越过 8000，
# 必然触发限流或把 JSON 截断（正是之前 Top5 整个消失的那个故障）。
# 因此维持 90：Top5 取窗口内最新的 90 条，本来也应当偏向最新。
# 48 小时的完整覆盖体现在文章列表与分类页上，那里不经模型、没有预算限制。
TOP_N = 5
MAX_TOK = 1600        # 常规输出预算；Groq 按 max_tokens 预扣 TPM，不能随意调大
MAX_TOK_RETRY = 2600  # 仅在解析失败时启用：宁可这一次多占些配额，也别丢掉整个 Top5
DIVERSITY_W = 1.5  # 排序时每多一家不同媒体，等效前移的名次数（越大越看重来源多样性）

# 提示词里的窗口必须跟着 WINDOW_HOURS 走：写死的话，窗口改成 48h 后
# 仍会告诉模型「过去 24 小时」，与实际给出的素材对不上。
_SYSTEM = (
    f"你是新闻主编，从过去 {WINDOW_HOURS} 小时的报道中选出最重要的 5 个**事件**。\n"
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
    # 两段式预算：常规用 MAX_TOK，解析失败才用 MAX_TOK_RETRY 重来一次。
    # 不能一开始就给大预算——Groq 按 max_tokens 预扣 TPM（实测 max_tokens=4000
    # 会让 8000 的余量直接掉到 3900），常规调用给大了等于自找限流。
    # 国际区的候选带中文译题，输入约是国内的两倍，输出也更长：实测 completion
    # 常年在 1278/1600 一线，推理稍多就把 JSON 截断在半路，于是 Top5 整个消失。
    data, text = None, ""
    for budget in (MAX_TOK, MAX_TOK_RETRY):
        try:
            text = complete("events", _SYSTEM, user,
                            max_tokens=budget, temperature=0.2).text
        except Exception as e:
            print(f"  [Top5 {REGIONS[region]} 失败] {type(e).__name__}: {e}")
            return 0
        data = parse_json(text)
        if data and data.get("events"):
            break
        if budget != MAX_TOK_RETRY:
            print(f"  [Top5 {REGIONS[region]}] 输出不完整（{len(text)} 字符），"
                  f"用 {MAX_TOK_RETRY} token 预算重试…")

    if not data or not data.get("events"):
        # 别静默返回 0：过去这种情况只会打印「Top0 事件已生成」，
        # 看不出是模型没返回、JSON 坏了、还是 ID 全对不上。
        print(f"  [Top5 {REGIONS[region]} 无结果] "
              f"{'JSON 解析失败' if data is None else 'events 字段为空'}"
              f"，模型输出 {len(text)} 字符：{text[:160]!r}")
        return 0

    src_of = {r["id"]: r["source_name"] for r in rows}
    events = []
    used: set[str] = set()
    dropped = {"无标题": 0, "ID对不上": 0}
    for model_rank, e in enumerate(data.get("events", [])[:TOP_N]):
        title = (e.get("title") or "").strip()
        summary = (e.get("summary") or "").strip()
        raw_ids = e.get("ids") or []
        ids = [id_map[str(s)] for s in raw_ids if str(s) in id_map]
        ids = [i for i in ids if i not in used]      # 防止同一文章跨事件重复
        if not title or not ids:
            dropped["无标题" if not title else "ID对不上"] += 1
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
    else:
        # 模型给了 events 但一条都没留下——说明它编了不存在的短ID，或全无标题。
        # 旧库里上一次的结果保持不动，页面不会因此变空。
        why = "、".join(f"{k} {v} 条" for k, v in dropped.items() if v)
        print(f"  [Top5 {REGIONS[region]} 无有效事件] 模型返回 {len(data.get('events', []))} 条，"
              f"全部被丢弃（{why or '原因不明'}）。保留上一次的结果。")
    return len(events)


def build_all(hours: int = WINDOW_HOURS) -> int:
    n = 0
    for region in REGIONS:
        n += build(region, hours)
    return n


if __name__ == "__main__":
    build_all()
