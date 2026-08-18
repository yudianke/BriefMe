"""渲染静态站。

页面结构：
  index.html          首页 —— 中国 Top5 / 国际 Top5（按事件聚合）
  region-{region}.html 分类总览 —— 四列分类网格
  cat-{region}-{i}.html 分类详情 —— AI 事件纪要 + 全部报道
  src-{region}.html    按来源 —— 按媒体罗列窗口内全部报道（不依赖 AI 分类）

所有时间以 UTC ISO 写进 data-utc，由前端 JS 转成用户当地时间。
所有取数一律走 db.recent_*（强制滚动窗口，长度见 models.WINDOW_HOURS）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db
from .models import (CATEGORIES, CATEGORIES_EN, REGION_NAV, REGION_NAV_EN, REGIONS,
                     SOURCE_LOGOS, SOURCE_WINDOW_HOURS, WINDOW_HOURS)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
TEMPLATES = ROOT / "templates"
LOGOS = ROOT / "logos"

PREVIEW_PER_CAT = 6   # 分类网格里每个分类预览条数


def cat_filename(region: str, category: str) -> str:
    return f"cat-{region}-{CATEGORIES.index(category)}.html"


def region_filename(region: str) -> str:
    return f"region-{region}.html"


def source_filename(region: str) -> str:
    return f"src-{region}.html"


def _summary(region: str, category: str) -> tuple[str, str]:
    """返回 (中文纪要, 英文纪要)。没跑过 --en 时英文回退成中文，页面不会空着。

    取最近一次而非「今天的」：纪要按 UTC 日历日存，页面窗口却是滚动的，
    按日期查会让 UTC 零点一过页面上的纪要集体消失，直到重跑为止。
    是否需要重写由 db.summaries_todo() 按素材变化判断，与日历无关。
    """
    with db.connect() as conn:
        row = conn.execute(
            """SELECT ai_text, ai_text_en FROM summaries
               WHERE region=? AND category=? ORDER BY generated_at DESC LIMIT 1""",
            (region, category),
        ).fetchone()
    if not row:
        return "", ""
    return row["ai_text"], (row["ai_text_en"] or row["ai_text"])


def source_names_en() -> dict[str, str]:
    """源 id -> 英文名（取自 config/sources.yaml 的 name_en）。

    读配置而不是另建一张映射表，这样加新源时只用改 sources.yaml 一个地方。
    """
    try:
        from .fetch import load_sources
        return {s["id"]: s.get("name_en", "") for s in load_sources()}
    except Exception:
        return {}


LOGO_HEIGHTS = {"xs": 13, "sm": 16, "md": 22}   # 与 style.css 保持一致
LOGO_MAX_PX = 96                                 # 输出图最大边长（够 retina 用，且体积小）


def logo_for(source_id: str) -> str | None:
    """返回该媒体 logo 的相对路径；无 logo 文件则返回 None（前端用文字占位）。"""
    fn = SOURCE_LOGOS.get(source_id)
    if fn and (LOGOS / fn).exists():
        return f"logos/{fn}"
    return None


def logo_box(source_id: str, size: str = "sm") -> dict | None:
    """按原图宽高比算出该尺寸下的精确像素宽高。

    把 width/height 写进 <img> 可让浏览器在图片加载前就预留正确空间，
    避免懒加载造成的布局抖动。
    """
    src = logo_for(source_id)
    if not src:
        return None
    h = LOGO_HEIGHTS.get(size, 16)
    try:
        from PIL import Image
        with Image.open(LOGOS / SOURCE_LOGOS[source_id]) as im:
            w0, h0 = im.size
        w = max(1, round(h * w0 / h0))
    except Exception:
        w = h  # 读不出尺寸就按正方形预留
    return {"src": src, "w": w, "h": h}


def logo_initials(source_name: str) -> str:
    """占位块文字：ASCII 名取前 3 字母；中文名尽量显示全称（最多 4 字）。

    中文只取 2 字会撞车（「纽约时报」和「纽约客」都会变成「纽约」），
    所以保留到 4 字，绝大多数媒体名可完整显示。
    """
    name = (source_name or "?").strip()
    if name.isascii():
        return name[:3].upper()
    # 「AP 美联社」这类混排，取中文部分
    cjk = "".join(ch for ch in name if not ch.isascii()).strip()
    return (cjk or name)[:4]


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["window_hours"] = WINDOW_HOURS
    env.globals["region_nav"] = REGION_NAV
    env.globals["region_nav_en"] = REGION_NAV_EN
    env.globals["region_file"] = region_filename
    env.globals["logo_for"] = logo_for
    env.globals["logo_box"] = logo_box
    env.globals["logo_initials"] = logo_initials
    # 媒体英文名：查不到就返回空串，模板会退回只显示中文名
    names_en = source_names_en()
    env.globals["source_en"] = lambda sid: names_en.get(sid, "")
    env.globals["long_window_sources"] = long_window_sources()
    # 纯文本，供 title= 属性使用。**不能**用 m.t() 生成：那个宏输出的是 <span> 标签，
    # 放进属性值会把属性提前闭合，页面结构就坏了。
    env.globals["cite_tip"] = (
        "OpenAlex 两年篇均被引，不是 Clarivate 影响因子（JIF） / "
        "OpenAlex 2-year mean citedness, not the Clarivate JIF")
    return env


def long_window_sources() -> list[dict]:
    """窗口比默认更长的源，供页面上加一句说明（见 _macros.window_note）。

    名字从 sources.yaml 现取，避免和源清单各写一份而对不上。
    """
    try:
        from .fetch import load_sources
        by_id = {s["id"]: s for s in load_sources()}
    except Exception:
        by_id = {}
    out = []
    for sid, hours in SOURCE_WINDOW_HOURS.items():
        if hours <= WINDOW_HOURS:
            continue
        s = by_id.get(sid)
        if not s:
            continue
        out.append({"name": s["name"], "name_en": s.get("name_en") or s["name"],
                    "hours": hours})
    return out


def discipline_filename(disc_id: str) -> str:
    return f"disc-{disc_id}.html"


def render_journals(env: Environment, generated_utc: str) -> None:
    """学术期刊总览页 + 每个学科一页。

    与新闻部分完全分离：只读 papers / paper_picks / journal_metrics 三张表，
    不碰 articles，也不参与 Top5 与分类纪要。
    """
    try:
        from .journals import load_journals
        disciplines, journals = load_journals()
    except Exception as e:
        print(f"  [学术期刊页跳过] 读不到 config/journals.yaml：{e}")
        return

    jmeta = {j["id"]: j for j in journals}
    metrics = db.journal_metrics()

    # 每个学科只取一次，总览页与学科页共用这一份。
    # 必须共用：papers_by_discipline 会按单刊上限截断，而库里的原始计数不会——
    # 两边各查各的，就会出现卡片徽章写 129、点进去只有 115 篇的对不上。
    disc_rows = {d["id"]: db.papers_by_discipline(d["id"]) for d in disciplines}
    per_journal = {
        d: {jid: sum(1 for r in rows if r["journal_id"] == jid)
            for jid in {r["journal_id"] for r in rows}}
        for d, rows in disc_rows.items()}

    def media_list(pairs) -> list[dict]:
        """把 (期刊 id, 篇数) 序列聚成筛选栏要的清单，按篇数降序。

        入参必须是**扁平的** (jid, n)：早先误把 jcounts.items() 直接传进来，
        于是 jid 变成了 ("学科","期刊") 这个元组，渲染出的 checkbox value 是
        "('ai', 'tpami')"，与卡片 data-counts 里的键对不上，所有徽章都变成 0。
        """
        agg: dict[str, int] = {}
        for jid, n in pairs:
            agg[jid] = agg.get(jid, 0) + n
        out = []
        for jid, n in agg.items():
            j = jmeta.get(jid, {})
            out.append({"id": jid, "name": j.get("name", jid),
                        "name_en": j.get("name_en") or j.get("name", jid), "count": n})
        return sorted(out, key=lambda x: -x["count"])

    def decorate(p: dict) -> dict:
        """补上期刊英文名与指标——这些来自配置和数据库，不经模型。"""
        j = jmeta.get(p["journal_id"], {})
        m = metrics.get(p["journal_id"], {})
        return {**p,
                "journal_name_en": j.get("name_en") or p["journal_name"],
                "mean_citedness": m.get("mean_citedness")}

    picks = {pool: [decorate(p) for p in db.latest_picks(pool)]
             for pool in ("sci", "ssci")}

    cards = []
    for d in disciplines:
        rows = disc_rows[d["id"]]
        if not rows:
            continue
        per_j = per_journal[d["id"]]
        cards.append({"name": d["name"], "name_en": d["name_en"], "count": len(rows),
                      "link": discipline_filename(d["id"]),
                      "papers": [decorate(dict(p)) for p in rows[:PREVIEW_PER_CAT]],
                      "src_counts": json.dumps(per_j, ensure_ascii=False)})

    (OUT / "journals.html").write_text(
        env.get_template("journals.html").render(
            picks=picks, cards=cards, n_journals=len(journals),
            media=media_list((jid, n) for pj in per_journal.values()
                             for jid, n in pj.items()),
            window_days=db.PAPER_WINDOW_DAYS, pick_days=db.PAPER_PICK_DAYS,
            generated_utc=generated_utc),
        encoding="utf-8")

    tpl = env.get_template("discipline.html")
    n_pages = 0
    for d in disciplines:
        rows = disc_rows[d["id"]]
        if not rows:
            continue
        by_journal: dict[str, list] = {}
        for r in rows:
            by_journal.setdefault(r["journal_id"], []).append(dict(r))
        groups = []
        for jid, ps in sorted(by_journal.items(), key=lambda x: -len(x[1])):
            j = jmeta.get(jid, {})
            m = metrics.get(jid, {})
            groups.append({
                "id": jid,
                "name": ps[0]["journal_name"],
                "name_en": j.get("name_en") or ps[0]["journal_name"],
                "mean_citedness": m.get("mean_citedness"), "h_index": m.get("h_index"),
                # 真实覆盖区间：高产刊会被单刊抓取上限截断，页头不能笼统写「90 天」
                "lo": min(p["published_at"] for p in ps),
                "hi": max(p["published_at"] for p in ps),
                "papers": ps})
        # 学科页的筛选栏用**页面上实际显示的**条数（已按单刊上限截断），
        # 而不是库里的总数，否则勾掉一本刊后剩余数会对不上肉眼可见的条目。
        (OUT / discipline_filename(d["id"])).write_text(
            tpl.render(name=d["name"], name_en=d["name_en"], journals=groups,
                       media=media_list((g["id"], len(g["papers"])) for g in groups),
                       total=len(rows), generated_utc=generated_utc),
            encoding="utf-8")
        n_pages += 1
    if cards:
        print(f"  学术期刊：总览页 + {n_pages} 个学科页"
              f"（{sum(len(r) for r in disc_rows.values())} 篇论文）")


def render(hours: int = WINDOW_HOURS) -> None:
    OUT.mkdir(exist_ok=True)
    env = _env()
    names_en = source_names_en()
    generated_utc = datetime.now(timezone.utc).isoformat()

    # ---------- 首页：Top5 事件 ----------
    home = {}
    for region in REGIONS:
        events = db.top_events(region, limit=5, hours=hours)
        fallback = [] if events else db.recent_articles(hours, region, limit=5)
        home[region] = {
            "label": REGION_NAV[region],
            "label_en": REGION_NAV_EN[region],
            "events": events,
            "fallback": fallback,
            "total": len(db.recent_articles(hours, region)),
        }
    (OUT / "index.html").write_text(
        env.get_template("home.html").render(
            home=home, region_keys=list(REGIONS), generated_utc=generated_utc),
        encoding="utf-8")

    # ---------- 地区页：四列分类网格 ----------
    n_cat_pages = 0
    for region in REGIONS:
        counts = db.category_counts(region, hours)
        cards = []
        cat_articles: dict[str, list] = {}     # 分类 -> 全部文章（详情页复用，避免重复查询）
        cat_media: dict[str, list] = {}        # 分类 -> 该分类下的媒体列表（详情页筛选栏）
        for cat in CATEGORIES:
            if not counts.get(cat):
                continue
            arts = db.recent_by_category(region, cat, hours)
            cat_articles[cat] = arts
            # 该分类下各家媒体的条数：前端按媒体筛选后据此更新计数徽章
            per_src: dict[str, int] = {}
            per_name: dict[str, str] = {}
            per_trivial: dict[str, int] = {}   # 各媒体中琐碎条数，供开关打开时扣减徽章
            for a in arts:
                per_src[a["source"]] = per_src.get(a["source"], 0) + 1
                per_name[a["source"]] = a["source_name"]
                if a["trivial"]:
                    per_trivial[a["source"]] = per_trivial.get(a["source"], 0) + 1
            # 分类详情页左侧筛选栏用（只列该分类下出现过的媒体）
            cat_media[cat] = sorted(
                ({"id": s, "name": per_name[s],
                  "name_en": names_en.get(s) or per_name[s], "count": n}
                 for s, n in per_src.items()),
                key=lambda x: -x["count"])
            cards.append({
                "category": cat,
                "category_en": CATEGORIES_EN.get(cat, cat),
                "count": counts[cat],
                "link": cat_filename(region, cat),
                "articles": arts[:PREVIEW_PER_CAT],
                "src_counts": json.dumps(per_src, ensure_ascii=False),
                "trivial_counts": json.dumps(per_trivial, ensure_ascii=False),
            })

        # 左侧筛选栏的计数**只统计本页点得到的（已分类的）文章**，与分类徽章同源。
        # 早先这里用的是 db.recent_articles（含未分类），于是侧栏说新华社 339 篇、
        # 而所有分类徽章加起来只有 230 篇，差的那些在站上根本点不到。
        # 未分类的部分改由下面的 n_unclassified 提示条交代，并在按来源页上全量展示。
        # 按**去重后的文章数**统计，不是「篇×类」：一篇可归 1-2 类，
        # 累加各分类会把双分类的文章数两遍，结果可能比按来源页上同一媒体的
        # 总数还大，看起来像 bug。这里的口径是「本页点得到该媒体多少篇」，
        # 与按来源页相减恰好等于未分类数，正好和提示条对得上。
        media: dict[str, dict] = {}
        seen_by_src: dict[str, set] = {}
        for arts in cat_articles.values():
            for a in arts:
                m = media.setdefault(a["source"],
                                     {"id": a["source"], "name": a["source_name"],
                                      "name_en": names_en.get(a["source"]) or a["source_name"],
                                      "count": 0})
                ids = seen_by_src.setdefault(a["source"], set())
                if a["id"] not in ids:
                    ids.add(a["id"])
                    m["count"] += 1
        media_list = sorted(media.values(), key=lambda x: -x["count"])

        # 窗口内有多少篇尚未分类——这些进不了任何分类页，页面上要如实说一句，
        # 否则用户只会看到「抓了很多但页面上没有」。跑完整流程后它归零，提示自动消失。
        classified = {a["id"] for arts in cat_articles.values() for a in arts}
        n_unclassified = sum(1 for a in db.recent_articles(hours, region)
                             if a["id"] not in classified)

        (OUT / region_filename(region)).write_text(
            env.get_template("region.html").render(
                region=region, label=REGION_NAV[region], label_en=REGION_NAV_EN[region],
                cards=cards, media=media_list, n_unclassified=n_unclassified,
                source_link=source_filename(region),
                generated_utc=generated_utc),
            encoding="utf-8")

        # ---------- 按来源页 ----------
        # 存在的理由：分类页只能显示已分类的文章，未分类的那批在站上会彻底消失
        # （实测曾有 1028 篇、占窗口的 38% 抓到了却任何页面都点不到）。
        # 这一页直接走 db.recent_articles，不 JOIN article_categories，
        # 因此按构造包含窗口内的每一篇，结构上兜住了这个洞。
        by_src: dict[str, dict] = {}
        for a in db.recent_articles(hours, region):
            g = by_src.setdefault(a["source"], {
                "id": a["source"], "name": a["source_name"],
                "name_en": names_en.get(a["source"]) or a["source_name"],
                "articles": []})
            g["articles"].append(a)
        src_groups = sorted(by_src.values(), key=lambda g: -len(g["articles"]))
        src_media = sorted(
            ({"id": g["id"], "name": g["name"], "name_en": g["name_en"],
              "count": len(g["articles"])} for g in src_groups),
            key=lambda x: -x["count"])
        (OUT / source_filename(region)).write_text(
            env.get_template("source.html").render(
                region=region, label=REGION_NAV[region], label_en=REGION_NAV_EN[region],
                sources=src_groups, media=src_media,
                total=sum(len(g["articles"]) for g in src_groups),
                generated_utc=generated_utc),
            encoding="utf-8")

        # ---------- 分类详情页 ----------
        cat_tpl = env.get_template("category.html")
        for c in cards:
            arts = cat_articles[c["category"]]
            summary, summary_en = _summary(region, c["category"])
            (OUT / c["link"]).write_text(
                cat_tpl.render(region=region, region_label=REGION_NAV[region],
                               region_label_en=REGION_NAV_EN[region],
                               category=c["category"], category_en=c["category_en"],
                               summary=summary, summary_en=summary_en,
                               articles=arts, media=cat_media[c["category"]],
                               generated_utc=generated_utc),
                encoding="utf-8")
            n_cat_pages += 1

    render_journals(env, generated_utc)

    for asset in ("style.css", "i18n.js", "localtime.js", "srcfilter.js"):
        src = TEMPLATES / asset
        if src.exists():
            (OUT / asset).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    # 部署 logo：原图往往是几千像素的大图，而页面只显示十几 px，
    # 这里缩到 LOGO_MAX_PX 再输出，体积可降一到两个数量级。原始 logos/ 不改动。
    n_logo, src_kb, out_kb = 0, 0, 0
    if LOGOS.exists():
        dest = OUT / "logos"
        dest.mkdir(exist_ok=True)
        for f in sorted(LOGOS.iterdir()):
            if not (f.is_file() and f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".svg"}):
                continue
            n_logo += 1
            src_kb += f.stat().st_size
            t = dest / f.name
            if t.exists() and t.stat().st_mtime >= f.stat().st_mtime:
                out_kb += t.stat().st_size
                continue
            if f.suffix.lower() == ".svg":
                t.write_bytes(f.read_bytes())
            else:
                try:
                    from PIL import Image
                    with Image.open(f) as im:
                        im = im.convert("RGBA")
                        im.thumbnail((LOGO_MAX_PX, LOGO_MAX_PX), Image.LANCZOS)
                        im.save(t, "PNG", optimize=True)
                except Exception:
                    t.write_bytes(f.read_bytes())   # 压缩失败就原样复制
            out_kb += t.stat().st_size
    if n_logo:
        print(f"  已部署 {n_logo} 个 logo：{src_kb // 1024}KB -> {out_kb // 1024}KB")
    missing = [s for s in {a["source"]: a["source_name"] for a in db.recent_articles(hours)}
               if not logo_for(s)]
    if missing:
        print(f"  提示：{', '.join(missing)} 无 logo 文件，使用文字占位块")
    print(f"渲染完成：首页 + {len(REGIONS)} 个地区页 + {len(REGIONS)} 个来源页 "
          f"+ {n_cat_pages} 个分类页 -> {OUT}")


if __name__ == "__main__":
    render()
