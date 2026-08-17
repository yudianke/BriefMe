"""数据结构与固定分类体系。改分类只改这里。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# 全局滚动时间窗口（小时）。UI 与 AI 输入一律只看这个窗口内「媒体发布」的新闻。
WINDOW_HOURS = 24

# 地区维度（由新闻源决定）
REGIONS = {"china": "国内", "world": "国外"}
REGION_NAV = {"china": "中国新闻", "world": "国际新闻"}

# 主题分类（由 AI 判定，一篇 1-2 类，只能用这里的值）
CATEGORIES = [
    "国际政治", "中国", "美国", "欧洲", "日本", "俄乌战争", "中东",
    "经济", "金融市场", "科技", "AI", "产业", "社会", "科学", "医疗", "文化",
]
FALLBACK_CATEGORY = "社会"  # AI 分类失败时的兜底

# ---- 页面英文版用的固定译名 ----
# 这些是 UI 骨架文字，与 AI 无关：即使没跑 --en，切到英文界面也能正常显示。
# 分类中文名是数据库里的主键，绝不改动；这里只是显示层的映射。
CATEGORIES_EN = {
    "国际政治": "World Politics", "中国": "China", "美国": "United States",
    "欧洲": "Europe", "日本": "Japan", "俄乌战争": "Russia–Ukraine War", "中东": "Middle East",
    "经济": "Economy", "金融市场": "Markets", "科技": "Technology",
    "AI": "AI", "产业": "Industry", "社会": "Society",
    "科学": "Science", "医疗": "Health", "文化": "Culture",
}
REGIONS_EN = {"china": "China", "world": "World"}
REGION_NAV_EN = {"china": "China News", "world": "World News"}

# 「要闻简报」关注的分类：只看政治/军事/经济层面的重大冲突与进展。
# 想调整简报盯什么，改这一行即可，不必动别处。
BRIEFING_CATEGORIES = ["国际政治", "中国", "美国", "欧洲", "俄乌战争", "中东", "经济", "金融市场"]
BRIEFING_NAV = "要闻简报"
BRIEFING_NAV_EN = "Briefing"

# 需要生成中文译题的语言（英文源保留原标题，另存 title_zh）
TRANSLATE_LANGS = {"en"}
# 需要生成英文译题的语言（中文源保留原标题，另存 title_en；仅 --en 模式下生成）
TRANSLATE_LANGS_EN = {"zh"}

# 媒体 logo：源 id -> logos/ 下的文件名。没有对应文件的源自动回退到文字占位块。
# 新增媒体时把文件丢进 logos/ 并在这里加一行即可。
SOURCE_LOGOS = {
    "bbc": "BBC.png",
    "guardian": "GUARDIAN.png",
    "ap": "AP.png",
    "reuters": "REUTERS.png",
    "xinhua": "XINHUASHE.png",
    "cctv": "CCTV.png",
    "caixin": "CAIXIN.png",
    "yicai": "DIYICAIJING.png",
    # cnn: 暂无 logo 文件 -> 使用占位块
}


@dataclass
class Article:
    source: str          # 源 id，如 bbc
    source_name: str     # 显示名，如 BBC
    region: str          # china | world
    title: str
    url: str
    lang: str            # en | zh
    published_at: str    # ISO8601 UTC，如 2026-08-15T03:20:00+00:00
    excerpt: str = ""
    fetched_at: str = ""
    categories: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return hashlib.sha1(self.url.encode("utf-8")).hexdigest()


DDL = """
CREATE TABLE IF NOT EXISTS articles (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    region       TEXT NOT NULL,
    title        TEXT NOT NULL,
    -- 译题只新增、绝不覆盖 title：zh 给中文界面看，en 给英文界面看
    title_zh     TEXT DEFAULT '',
    title_en     TEXT DEFAULT '',
    url          TEXT NOT NULL,
    lang         TEXT NOT NULL,
    published_at TEXT NOT NULL,
    excerpt      TEXT DEFAULT '',
    fetched_at   TEXT NOT NULL,
    -- 1 = 琐碎/花边新闻（名人八卦、体育花絮、猎奇趣闻等），供前端一键隐藏
    trivial      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_region ON articles(region);

-- 被标题黑名单拦下的条目。留档只为一件事：**事后复查有没有误杀**。
-- 记下命中的具体规则，发现真新闻被拦时可以直接定位到是哪条规则过宽。
-- 按 url 哈希去重，同一个行情页天天出现也只占一行，n_seen 计数。
CREATE TABLE IF NOT EXISTS dropped_titles (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    source_name TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    n_seen      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS article_categories (
    article_id TEXT NOT NULL,
    category   TEXT NOT NULL,
    PRIMARY KEY (article_id, category)
);

CREATE TABLE IF NOT EXISTS summaries (
    region       TEXT NOT NULL,
    category     TEXT NOT NULL,
    date         TEXT NOT NULL,
    ai_text      TEXT NOT NULL,
    -- 英文版纪要：由 --en 把 ai_text 译过去，保证中英讲的是同一批事件
    ai_text_en   TEXT DEFAULT '',
    generated_at TEXT NOT NULL,
    PRIMARY KEY (region, category, date)
);

-- 「要闻简报」：跨地区的主题式提要，只盯政治/军事/经济层面的重大冲突与进展。
-- 不按日期做主键：内容窗口是滚动 24 小时，按日历日缓存会在 UTC 零点整批失效
-- （见 db.summaries_todo 的同类教训）。这里只追加，读取时取 generated_at 最新的一条。
CREATE TABLE IF NOT EXISTS briefings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    ai_text      TEXT NOT NULL,
    ai_text_en   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_briefings_gen ON briefings(generated_at);

-- Top5 事件（按事件而非单篇文章聚合）
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    region       TEXT NOT NULL,
    date         TEXT NOT NULL,
    rank         INTEGER NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    -- 英文版同样由 --en 翻译中文原文而来：两种语言的 Top5 必须是同样的 5 个事件、
    -- 同样的排序，否则切换语言时读者会看到两份对不上的榜单。
    title_en     TEXT DEFAULT '',
    summary_en   TEXT DEFAULT '',
    generated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_region_date ON events(region, date);

CREATE TABLE IF NOT EXISTS event_articles (
    event_id   TEXT NOT NULL,
    article_id TEXT NOT NULL,
    PRIMARY KEY (event_id, article_id)
);
"""
