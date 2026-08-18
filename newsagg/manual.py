"""Claude Code 模式：不调用任何 API，改由 Claude Code 直接完成 AI 工作。

用法（两步）：
  1) python run.py --manual      抓取 -> 导出待办到 manual/ -> 用现有数据渲染
  2) 在 Claude Code 里说：「处理 pachong/manual 的待办」
     Claude 读 manual/*.md，把结果写成 manual/results.json，然后执行：
       python -m newsagg.manual load manual/results.json && python -m newsagg.render

导出的每个文件都自带说明和输出格式，Claude 照做即可，无需额外解释。

单项命令（按需）：
  python -m newsagg.manual export           只导出待办
  python -m newsagg.manual export --en      同上，并要求一并给出英文版
  python -m newsagg.manual load <结果.json>  回写结果
  python -m newsagg.manual status           看还剩多少待办
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from . import db
from .models import CATEGORIES, REGIONS, WINDOW_HOURS

MANUAL = Path(__file__).resolve().parent.parent / "manual"
TOP_N = 5
CAND = 100         # Top5 候选条数（手动模式不花 API 额度，可比 events.CANDIDATES 宽）
PER_CAT = 30       # 每个分类送给 Claude 的报道条数（同上，窗口 48h 后适度上调）
TR_LIMIT = 400     # 单次导出的最大待翻译条数


# ----------------------------------------------------------------- 导出

def _w(name: str, text: str) -> Path:
    MANUAL.mkdir(exist_ok=True)
    p = MANUAL / name
    p.write_text(text, encoding="utf-8")
    return p


def _pending_translations() -> list:
    return db.untranslated()[:TR_LIMIT]


def _pending_translations_en() -> list:
    return db.untranslated_en()[:TR_LIMIT]


def _pending_classify() -> list:
    rows = db.recent_articles()
    with db.connect() as conn:
        done = {r[0] for r in conn.execute("SELECT DISTINCT article_id FROM article_categories")}
    return [r for r in rows if r["id"] not in done]


def _pending_summaries() -> list:
    """待写纪要的 (region, category)，含「已写过但素材已更新」的（见 db.summaries_todo）。"""
    order = {(r, c): (i, j) for i, r in enumerate(REGIONS) for j, c in enumerate(CATEGORIES)}
    return sorted(db.summaries_todo(), key=lambda k: order.get(k, (99, 99)))


def export(en: bool = False) -> dict:
    """导出全部待办，返回各项数量。

    en=True 时额外要英文版。注意这里不能像 `--en` 那样「先出中文再翻译」：
    手动模式下中文结果要等助手写回 results.json 才存在，届时再导一轮英文待办
    就得让用户跑两遍。所以改为在同一批待办里直接要求中英两版一起给出。
    """
    MANUAL.mkdir(exist_ok=True)
    # 先清掉上一轮的待办文件。任务集合会随开关变化（比如 06 只有 --en 才有），
    # 残留的旧文件会被下面的 glob 收进清单，让助手去做一份本轮并不需要的工作。
    for old in MANUAL.glob("0*.md"):
        old.unlink()
    # 上一轮的 results.json 也要清掉：README 里让用户跑
    # `manual load manual/results.json`，助手若没写出新的，那条命令会把**昨天的**
    # 结果又回写一遍，看上去成功、实际什么都没更新。宁可让它因文件不存在而报错。
    stale = MANUAL / "results.json"
    if stale.exists():
        stale.unlink()
    counts = {}

    # 1. 翻译
    rows = _pending_translations()
    counts["translate"] = len(rows)
    if rows:
        body = "\n".join(f"{r['id'][:12]}\t{r['title']}" for r in rows)
        _w("01-translate.md",
           "# 任务1：英文标题翻译\n\n"
           "把下面每行「短ID<TAB>英文标题」翻译成**简洁准确的中文标题**。\n"
           "保留人名/地名/机构的通用中文译名；不加评论、不扩写、不加书名号。\n\n"
           "输出到 results.json 的 `translations`：\n"
           '```json\n{"translations":[{"id":"短ID","zh":"中文标题"}]}\n```\n\n'
           f"共 {len(rows)} 条：\n\n```\n{body}\n```\n")

    # 2. 分类
    rows = _pending_classify()
    counts["classify"] = len(rows)
    if rows:
        body = "\n".join(
            f"{r['id'][:12]}\t{r['title']}"
            + (f"\n\t（正文开头：{r['excerpt'][:110]}）" if r["excerpt"] else "")
            for r in rows)
        _w("02-classify.md",
           "# 任务2：新闻分类\n\n"
           f"可用分类（只能用这些，不要新增）：{'、'.join(CATEGORIES)}\n\n"
           "为每篇归 1-2 个最贴切的分类。**先看标题**；标题信息不足时看给出的正文开头；\n"
           "仍判不准就归「社会」。\n\n"
           "输出到 results.json 的 `classifications`：\n"
           '```json\n{"classifications":[{"id":"短ID","categories":["分类1","分类2"]}]}\n```\n\n'
           f"共 {len(rows)} 条：\n\n```\n{body}\n```\n")

    # 3. Top5 事件
    parts = []
    for region in REGIONS:
        rows = db.recent_articles(WINDOW_HOURS, region, limit=CAND)
        if not rows:
            continue
        lines = "\n".join(
            f"{r['id'][:12]}\t[{r['source_name']}]\t{r['title']}"
            + (f" / {r['title_zh']}" if r["title_zh"] else "")
            for r in rows)
        parts.append(f"## {region}（{REGIONS[region]}，共 {len(rows)} 条候选）\n\n```\n{lines}\n```\n")
    counts["events"] = len(parts)
    if parts:
        _w("03-events.md",
           "# 任务3：首页 Top5 事件\n\n"
           "为每个地区选出**最重要的 5 个事件**。\n\n"
           "**第一步 跨媒体合并**：把不同媒体报道的同一件事合并为一个事件，列出其全部短ID。\n"
           "判断依据是「是否同一件事实」，而非措辞是否相同（中英文标题差异很大）。\n"
           "务必回扫整个列表找齐所有报道该事件的媒体——最常见的错误是把同一家媒体的多篇\n"
           "聚成一个事件，却漏掉别家对同一事件的报道。同一事件只占一个名额。\n\n"
           "**第二步 排序**：来源多样性（不同媒体家数）权重最高，其次是事件重要性、\n"
           "报道篇数、时效性。3 家媒体报道的事件应排在只有 1 家报道的事件之前。\n\n"
           "**第三步**：每个事件写中文标题（≤30字）+ 一句事实纪要（谁做了什么、发生了什么、\n"
           "结果如何）。禁止评论、预测和套话。\n\n"
           + ("**第四步（英文版）**：为每个事件再给一份英文标题与纪要，"
              "内容必须与中文一一对应（同样的事件、同样的信息量），"
              "用平实的新闻英语，人名地名机构用通行英文名。\n\n"
              "输出到 results.json 的 `events`：\n"
              '```json\n{"events":{"china":[{"title":"..","summary":"..",'
              '"title_en":"..","summary_en":"..","ids":["短ID"]}],"world":[...]}}\n```\n\n'
              if en else
              "输出到 results.json 的 `events`：\n"
              '```json\n{"events":{"china":[{"title":"..","summary":"..","ids":["短ID"]}],"world":[...]}}\n```\n\n')
           + "\n".join(parts))

    # 4. 分类纪要
    pend = _pending_summaries()
    counts["summaries"] = len(pend)
    if pend:
        blocks = []
        for region, cat in pend:
            arts = db.recent_by_category(region, cat, limit=PER_CAT)
            lines = "\n".join(
                f"- [{a['source_name']}] {a['title']}"
                + (f" ｜ {a['excerpt'][:100]}" if a["excerpt"] else "")
                for a in arts)
            blocks.append(f"## {region} | {cat}（{len(arts)} 篇）\n\n```\n{lines}\n```\n")
        _w("04-summaries.md",
           "# 任务4：分类事件纪要\n\n"
           "为下面每个「地区 | 分类」写一段**中文**事件纪要（即使素材是英文）。\n\n"
           "先把多家媒体报道的同一事件合并为一条，再按重要性排序，每条写 1-2 句事实纪要，\n"
           "包含：谁做了什么、发生了什么、结果或最新进展如何。多家报道同一事件时在句末\n"
           "用括号标注来源，如（路透/AP/BBC）。\n\n"
           "严格禁止：评论、观点、预测、影响分析、空泛背景，以及「值得关注的是」\n"
           "「引发广泛关注」「与此同时」「总体来看」这类套话。不写开场白和结语。\n"
           "输出 3-6 条，每条一行以「· 」开头，全文 ≤400 字。\n\n"
           + ("**英文版**：每段再给一份英文，逐条对应中文（同样的条数、同样的顺序、"
              "同样的信息），媒体名用通行英文名（路透→Reuters、新华社→Xinhua 等）。\n\n"
              "输出到 results.json 的 `summaries`：\n"
              '```json\n{"summaries":[{"region":"world","category":"中东",'
              '"text":"· ...\\n· ...","text_en":"· ...\\n· ..."}]}\n```\n\n'
              if en else
              "输出到 results.json 的 `summaries`：\n"
              '```json\n{"summaries":[{"region":"world","category":"中东","text":"· ...\\n· ..."}]}\n```\n\n')
           + "\n".join(blocks))

    # 5. 琐碎新闻判定（只需列出琐碎的那些 ID，不必逐条表态）
    rows = db.unjudged_trivial()
    counts["trivial"] = len(rows)
    if rows:
        body = "\n".join(f"{r['id'][:12]}\t{r['title']}" for r in rows)
        _w("05-trivial.md",
           "# 任务5：标出琐碎新闻\n\n"
           "从下面挑出**琐碎/花边**的条目，只返回它们的短ID即可（其余不用管）。\n\n"
           "算琐碎的：名人八卦与私生活、体育赛果与转会花絮、娱乐圈动态、猎奇趣闻、\n"
           "宠物/奇葩比赛、网红与社媒争议、生活方式小贴士、星座测验、赛事博彩前瞻，\n"
           "以及「某地某人因某段视频被调查」这类无公共影响的个例。\n\n"
           "**不算**琐碎的：政策、外交、战争与冲突、灾害与事故、司法与执法、经济金融、\n"
           "科技与科研、公共卫生、重大企业动向，以及虽属文化领域但有公共意义的报道\n"
           "（如文物保护、历史纪念、重要人物逝世）。\n\n"
           "拿不准就**不标**——这个开关默认关闭，误杀比漏判更让人困扰。\n\n"
           "输出到 results.json 的 `trivial`：\n"
           '```json\n{"trivial":["短ID","短ID"]}\n```\n\n'
           f"共 {len(rows)} 条待判定：\n\n```\n{body}\n```\n")

    # 6. 中文标题 -> 英文译题（仅 --en；与任务1 方向相反）
    if en:
        rows = _pending_translations_en()
        counts["en_titles"] = len(rows)
        if rows:
            body = "\n".join(f"{r['id'][:12]}\t{r['title']}" for r in rows)
            _w("07-en-titles.md",
               "# 任务6：中文标题翻译成英文\n\n"
               "把下面每行「短ID<TAB>中文标题」翻译成**简洁准确的英文新闻标题**。\n"
               "人名/地名/机构用通行英文名；不加评论、不扩写、不加引号。\n\n"
               "输出到 results.json 的 `titles_en`：\n"
               '```json\n{"titles_en":[{"id":"短ID","en":"English headline"}]}\n```\n\n'
               f"共 {len(rows)} 条：\n\n```\n{body}\n```\n")

    # 汇总说明
    todo = [f"- `{f}`" for f in sorted(p.name for p in MANUAL.glob("0*.md"))]
    _w("README.md",
       "# 待办（Claude Code 模式）\n\n"
       "本目录由 `python run.py --manual` 生成，用于**不消耗 API 额度**地完成 AI 环节。\n\n"
       "## 给 Claude Code 的指示\n\n"
       "请依次处理下列文件，每个文件顶部都写明了任务与输出格式：\n\n"
       + "\n".join(todo) +
       "\n\n把所有结果**合并成一个** `manual/results.json`（顶层键可同时包含 "
       "`translations` / `classifications` / `events` / `summaries` / `trivial`"
       + ("／`titles_en`，且 events 与 summaries 里要带上英文字段" if en else "")
       + "），然后运行：\n\n"
       "```bash\npython -m newsagg.manual load manual/results.json && python -m newsagg.render\n```\n\n"
       "## 当前待办量\n\n"
       + "\n".join(f"- {k}：{v}" for k, v in counts.items()) + "\n")
    return counts


# ----------------------------------------------------------------- 回写

def load(path: str) -> None:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = db.recent_articles()
    short = {r["id"][:12]: r["id"] for r in rows}

    if tr := data.get("translations"):
        n = 0
        for it in tr:
            full, zh = short.get(str(it.get("id", ""))), (it.get("zh") or "").strip()
            if full and zh:
                db.set_title_zh(full, zh)
                n += 1
        print(f"译题回写 {n} 条")

    if tr := data.get("titles_en"):
        n = 0
        for it in tr:
            full, en = short.get(str(it.get("id", ""))), (it.get("en") or "").strip()
            if full and en:
                db.set_title_en(full, en)
                n += 1
        print(f"英文译题回写 {n} 条")

    if cl := data.get("classifications"):
        n = 0
        with db.connect() as conn:
            for it in cl:
                full = short.get(str(it.get("id", "")))
                if not full:
                    continue
                for c in [c for c in (it.get("categories") or []) if c in CATEGORIES][:2]:
                    conn.execute(
                        "INSERT OR IGNORE INTO article_categories(article_id,category) VALUES(?,?)",
                        (full, c))
                    n += 1
        print(f"分类回写 {n} 条(篇×类)")

    for region, items in (data.get("events") or {}).items():
        out, used = [], set()
        for e in items[:TOP_N]:
            ids = [short[str(s)] for s in (e.get("ids") or []) if str(s) in short]
            ids = [i for i in ids if i not in used]
            if not e.get("title") or not ids:
                continue
            used.update(ids)
            eid = hashlib.sha1(f"{region}|{db.today_key()}|{e['title']}".encode()).hexdigest()[:16]
            out.append({"id": eid, "title": e["title"].strip(),
                        "summary": (e.get("summary") or "").strip(), "article_ids": ids,
                        "title_en": (e.get("title_en") or "").strip(),
                        "summary_en": (e.get("summary_en") or "").strip()})
        if out:
            db.save_events(region, out)
            # save_events 是覆盖写，英文字段要在写完之后单独补上
            for e in out:
                if e["title_en"]:
                    db.set_event_en(e["id"], e["title_en"], e["summary_en"])
            n_en = sum(1 for e in out if e["title_en"])
            print(f"{REGIONS.get(region, region)} Top{len(out)} 事件已回写"
                  + (f"（含 {n_en} 条英文）" if n_en else ""))

    if sm := data.get("summaries"):
        now, today = db.utc_now(), db.today_key()
        n_en = 0
        with db.connect() as conn:
            for s in sm:
                text_en = (s.get("text_en") or "").strip()
                n_en += bool(text_en)
                conn.execute(
                    """INSERT OR REPLACE INTO
                       summaries(region,category,date,ai_text,ai_text_en,generated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (s["region"], s["category"], today, s["text"].strip(), text_en, now))
        print(f"分类纪要回写 {len(sm)} 段" + (f"（含 {n_en} 段英文）" if n_en else ""))

    # 琐碎判定：给出的 ID 标为琐碎；本批送审过的全部记为「已判定」，
    # 这样未被列出的就等于「判过=不琐碎」，下次不会重复送审。
    if (tv := data.get("trivial")) is not None:
        ids = [short[str(s)] for s in tv if str(s) in short]
        n = db.set_trivial(ids)
        judged = [r["id"] for r in db.unjudged_trivial()]
        db.mark_judged(judged)
        print(f"琐碎新闻标记 {n} 条（本批 {len(judged)} 条已完成判定）")

    # 导出是一次性快照：写它的时候这批文章还没分类，所以纪要任务不可能包含它们。
    # 分类回写之后才谈得上「这些分类该写纪要了」，此时需要再导出一轮。
    if pend := _pending_summaries():
        print(f"\n提示：分类回写后有 {len(pend)} 个分类的纪要待写（新分类，或原纪要素材已更新）。")
        print("      再跑一次导出即可拿到这批任务：")
        print("        python -m newsagg.manual export")


def status() -> None:
    print(f"待翻译       {len(_pending_translations())}")
    print(f"待翻译(英)   {len(_pending_translations_en())}")
    print(f"待分类       {len(_pending_classify())}")
    print(f"待汇总(分类) {len(_pending_summaries())}")
    print(f"待判定琐碎   {len(db.unjudged_trivial())}")
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM events WHERE date=?", (db.today_key(),)).fetchone()[0]
    print(f"今日 Top5 事件 {n} 条（0 表示待生成）")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "export":
        c = export(en="--en" in sys.argv)
        print("已导出到 manual/：", c)
    elif cmd == "load":
        load(sys.argv[2])
    elif cmd == "status":
        status()
    else:
        print(__doc__)
