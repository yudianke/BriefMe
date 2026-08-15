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
    "国际政治", "中国", "美国", "欧洲", "俄乌战争", "中东",
    "经济", "金融市场", "科技", "AI", "产业", "社会", "科学", "医疗", "文化",
]
FALLBACK_CATEGORY = "社会"  # AI 分类失败时的兜底

# 需要生成中文译题的语言（英文源保留原标题，另存 title_zh）
TRANSLATE_LANGS = {"en"}

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
    title_zh     TEXT DEFAULT '',
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
    generated_at TEXT NOT NULL,
    PRIMARY KEY (region, category, date)
);

-- Top5 事件（按事件而非单篇文章聚合）
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    region       TEXT NOT NULL,
    date         TEXT NOT NULL,
    rank         INTEGER NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_region_date ON events(region, date);

CREATE TABLE IF NOT EXISTS event_articles (
    event_id   TEXT NOT NULL,
    article_id TEXT NOT NULL,
    PRIMARY KEY (event_id, article_id)
);
"""
