"""回归测试：窗口内的每一篇文章都必须能在 output/ 的某个页面上找到。

为什么要有这条测试：分类页只显示已分类的文章，而分类是 AI（或 --manual）做的。
一旦出现「部分已分类 + 部分未分类」的混合状态，未分类的那批就会从整站消失——
实测曾有 1028 篇、占窗口 38% 的文章抓到了却任何页面都点不到，而且没有任何报错。
按来源页（src-{region}.html）就是为兜住这个洞而存在的，这条测试盯着它别再破。

用法：python tools/check_coverage.py     （退出码非 0 表示有文章看不到）
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from newsagg import db                                    # noqa: E402
from newsagg.models import WINDOW_HOURS                    # noqa: E402


def main() -> int:
    out = ROOT / "output"
    pages = sorted(out.glob("*.html"))
    if not pages:
        print("output/ 里没有页面，先跑 python -m newsagg.render")
        return 2
    blob = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in pages)

    rows = db.recent_articles(WINDOW_HOURS)
    missing = [r for r in rows
               if r["url"] not in blob and html.escape(r["url"]) not in blob]

    seen = len(rows) - len(missing)
    pct = 100 * seen // len(rows) if rows else 100
    print(f"窗口 {WINDOW_HOURS}h：{len(rows)} 篇，站上可见 {seen} 篇（{pct}%），"
          f"看不见 {len(missing)} 篇  [{len(pages)} 个页面]")
    if not missing:
        print("✅ 通过：不存在抓到了却在站上看不到的文章")
        return 0
    print("❌ 以下文章在任何页面上都找不到：")
    for r in missing[:15]:
        print(f"    [{r['source_name']}] {r['title'][:60]}")
    if len(missing) > 15:
        print(f"    …… 另有 {len(missing) - 15} 篇")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
