"""渲染静态站。

页面结构：
  index.html          首页 —— 中国 Top5 / 国际 Top5（按事件聚合）
  region-{region}.html 分类总览 —— 四列分类网格
  cat-{region}-{i}.html 分类详情 —— AI 事件纪要 + 全部报道

所有时间以 UTC ISO 写进 data-utc，由前端 JS 转成用户当地时间。
所有取数一律走 db.recent_*（强制 24 小时滚动窗口）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db
from .models import CATEGORIES, REGION_NAV, REGIONS, SOURCE_LOGOS, WINDOW_HOURS

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
TEMPLATES = ROOT / "templates"
LOGOS = ROOT / "logos"

PREVIEW_PER_CAT = 6   # 分类网格里每个分类预览条数


def cat_filename(region: str, category: str) -> str:
    return f"cat-{region}-{CATEGORIES.index(category)}.html"


def region_filename(region: str) -> str:
    return f"region-{region}.html"


def _summary(region: str, category: str) -> str:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT ai_text FROM summaries WHERE region=? AND category=? AND date=?",
            (region, category, db.today_key()),
        ).fetchone()
    return row["ai_text"] if row else ""


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
    env.globals["region_file"] = region_filename
    env.globals["logo_for"] = logo_for
    env.globals["logo_box"] = logo_box
    env.globals["logo_initials"] = logo_initials
    return env


def render(hours: int = WINDOW_HOURS) -> None:
    OUT.mkdir(exist_ok=True)
    env = _env()
    generated_utc = datetime.now(timezone.utc).isoformat()

    # ---------- 首页：Top5 事件 ----------
    home = {}
    for region in REGIONS:
        events = db.top_events(region, limit=5, hours=hours)
        fallback = [] if events else db.recent_articles(hours, region, limit=5)
        home[region] = {
            "label": REGION_NAV[region],
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
                ({"id": s, "name": per_name[s], "count": n} for s, n in per_src.items()),
                key=lambda x: -x["count"])
            cards.append({
                "category": cat,
                "count": counts[cat],
                "link": cat_filename(region, cat),
                "articles": arts[:PREVIEW_PER_CAT],
                "src_counts": json.dumps(per_src, ensure_ascii=False),
                "trivial_counts": json.dumps(per_trivial, ensure_ascii=False),
            })

        # 该地区窗口内出现过的媒体，按条数降序（左侧筛选栏用）
        media: dict[str, dict] = {}
        for a in db.recent_articles(hours, region):
            m = media.setdefault(a["source"],
                                 {"id": a["source"], "name": a["source_name"], "count": 0})
            m["count"] += 1
        media_list = sorted(media.values(), key=lambda x: -x["count"])
        # 未分类时的回退：按媒体分组，保证没有 AI 也能浏览标题
        sources = []
        if not cards:
            by_src: dict[str, list] = {}
            for a in db.recent_articles(hours, region):
                by_src.setdefault(a["source_name"], []).append(a)
            sources = [{"name": k, "articles": v} for k, v in by_src.items()]

        (OUT / region_filename(region)).write_text(
            env.get_template("region.html").render(
                region=region, label=REGION_NAV[region], cards=cards,
                sources=sources, media=media_list, generated_utc=generated_utc),
            encoding="utf-8")

        # ---------- 分类详情页 ----------
        cat_tpl = env.get_template("category.html")
        for c in cards:
            arts = cat_articles[c["category"]]
            (OUT / c["link"]).write_text(
                cat_tpl.render(region=region, region_label=REGION_NAV[region],
                               category=c["category"], summary=_summary(region, c["category"]),
                               articles=arts, media=cat_media[c["category"]],
                               generated_utc=generated_utc),
                encoding="utf-8")
            n_cat_pages += 1

    for asset in ("style.css", "localtime.js", "srcfilter.js"):
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
    print(f"渲染完成：首页 + {len(REGIONS)} 个地区页 + {n_cat_pages} 个分类页 -> {OUT}")


if __name__ == "__main__":
    render()
