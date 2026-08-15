"""正文开头抓取：仅在分类阶段「标题信息不足」时按需调用（不做全量抓取）。

只取公开页面的首段若干字，用于帮助 AI 二次判断分类；不转载全文。
失败不阻塞——返回空串，调用方回退到仅用标题。
"""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
LIMIT = 300  # 节选最多字数


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def fetch_opening(url: str, timeout: float = 12.0) -> str:
    """抓取文章正文开头。失败返回 ''。"""
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout,
                          headers={"User-Agent": UA}) as c:
            r = c.get(url)
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                return ""
            soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return ""

    # 1) 优先用 meta description（大多数新闻站都有，且是公开摘要）
    for sel in ('meta[property="og:description"]', 'meta[name="description"]'):
        tag = soup.select_one(sel)
        if tag and tag.get("content"):
            t = _clean(tag["content"])
            if len(t) > 40:
                return t[:LIMIT]

    # 2) 退而求其次：正文里第一个足够长的 <p>
    root = soup.find("article") or soup
    for p in root.find_all("p", limit=25):
        t = _clean(p.get_text())
        if len(t) > 60:
            return t[:LIMIT]
    return ""


def backfill_excerpt(row) -> str:
    """给一篇文章拿到可用的判定文本：优先已有 excerpt，否则现抓正文开头。"""
    if row["excerpt"] and len(row["excerpt"].strip()) > 40:
        return row["excerpt"].strip()[:LIMIT]
    return fetch_opening(row["url"])
