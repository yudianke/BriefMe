"""一键流水线：抓取 -> 翻译 -> AI 分类 -> Top5 事件 -> 分类纪要 -> 渲染。

用法:
  python run.py            # 完整跑一遍（调用 API，需 .env 里有 provider key）
  python run.py --manual   # Claude Code 模式：抓取+导出待办+渲染，不调用任何 API
  python run.py --no-ai    # 只抓取+渲染，不做 AI 也不导出待办
  python run.py --cn-only  # 只抓国内源（无法访问外网时用，避免长时间超时）
  python run.py --all      # 跳过连通性探测，强制抓全部源（确定自己能上外网时用）
  python run.py --en       # 额外生成英文版 AI 内容（译题/纪要/Top5），约多花 40% 调用

页面右上角的中/EN 切换按钮始终可用：界面文字是内置的，不需要 --en；
--en 只影响 AI 生成的那部分内容（标题译文、分类纪要、Top5）是否也有英文版。

学术期刊是并行的一条支线：数据走 Crossref，存在独立的 papers 表，
**不参与**新闻的分类 / Top5 / 分类纪要，失败也不会影响新闻部分。
--cn-only 时整段跳过（Crossref 在境外）。
"""
from __future__ import annotations

import sys
import time
import webbrowser
from pathlib import Path

from newsagg import fetch, render
from newsagg.models import WINDOW_HOURS
from newsagg.render import OUT

STEPS = [
    ("翻译外文标题", "newsagg.translate", "translate"),
    ("AI 分类", "newsagg.classify", "classify"),
    ("Top5 事件", "newsagg.events", "build_all"),
    ("分类事件纪要", "newsagg.summarize", "summarize"),
]


def main() -> None:
    no_ai = "--no-ai" in sys.argv
    manual = "--manual" in sys.argv
    want_en = "--en" in sys.argv
    t0 = time.time()
    total = len(STEPS) + 2 + (1 if want_en and not no_ai else 0)

    print("=" * 54, f"\n[1/{total}] 抓取（窗口 {WINDOW_HOURS}h）")
    cn_only = "--cn-only" in sys.argv
    fetch.run(hours=WINDOW_HOURS, cn_only=cn_only, skip_probe="--all" in sys.argv)

    # 学术期刊的**抓取**：不调用 AI，放在这里没有代价。
    # 它的 AI 环节（译题 + 五篇精选）刻意放到全部新闻步骤之后，见下方 journal_ai()。
    if cn_only:
        print("\n[学术期刊] --cn-only：Crossref 在境外，跳过")
    else:
        try:
            from newsagg import journals
            from newsagg.db import PAPER_WINDOW_DAYS
            print(f"\n[学术期刊] 抓取（窗口 {PAPER_WINDOW_DAYS} 天）")
            journals.fetch()
        except Exception as e:
            print(f"  [中断] {type(e).__name__}: {e}\n  不影响新闻部分，下一轮会重试。")

    def journal_ai() -> None:
        """论文译题 + 五篇精选。

        **必须排在所有新闻步骤之后。** 免费档每天 20 万 token 是新闻和论文共用的，
        论文首次回填有上千条待译；先跑论文就会把额度吃光，新闻的分类纪要和 Top5
        全部拿不到额度——实测过一次，正是这个顺序导致当天新闻侧一段纪要都没生成。
        新闻是主功能，论文是附加，所以让论文捡剩下的。
        """
        if cn_only or no_ai or manual:
            return
        try:
            from newsagg import paperai
            print(f"\n[学术期刊] AI 环节（译题 + 五篇精选）")
            paperai.translate_papers()
            paperai.pick_all()
        except Exception as e:
            print(f"  [中断] {type(e).__name__}: {e}\n  不影响新闻部分，下一轮会重试。")

    if manual:
        from newsagg import manual as mn
        print("\n[Claude Code 模式] 导出待办，不调用任何 API")
        counts = mn.export(en=want_en)
        for k, v in counts.items():
            print(f"  {k:<10} {v}")
        print(f"\n  待办已写入 {mn.MANUAL}")
        print("  下一步：在 Claude Code 里说「处理manual/的待办」")
    elif no_ai:
        print("\n[跳过 AI] --no-ai")
    else:
        import importlib
        step_no = 1
        for label, mod_name, fn_name in STEPS:
            step_no += 1
            print(f"\n[{step_no}/{total}] {label}")
            try:
                fn = getattr(importlib.import_module(mod_name), fn_name)
                fn(hours=WINDOW_HOURS) if fn_name != "build_all" else fn(WINDOW_HOURS)
            except RuntimeError as e:      # 没有可用 provider
                print(f"  [跳过] {e}")
                break
            except Exception as e:         # 限流等中断：已完成的部分照常渲染
                print(f"  [中断] {type(e).__name__}: {e}\n  已完成的部分会照常渲染，重跑可续。")
        # 英文版放在最后：它翻译的是上面几步的产物，缺了也只是英文界面回退中文
        if want_en:
            step_no += 1
            print(f"\n[{step_no}/{total}] 英文版 AI 内容（--en）")
            try:
                from newsagg import english
                english.run_all(hours=WINDOW_HOURS)
            except RuntimeError as e:
                print(f"  [跳过] {e}")
            except Exception as e:
                print(f"  [中断] {type(e).__name__}: {e}\n  英文缺失的部分会回退显示中文。")

    journal_ai()

    print(f"\n[{total}/{total}] 渲染")
    render.render(hours=WINDOW_HOURS)

    print("=" * 54)
    print(f"完成，用时 {time.time() - t0:.1f}s")
    index = OUT / "index.html"
    print(f"打开: {index}")
    try:
        webbrowser.open(Path(index).as_uri())
    except Exception:
        pass


if __name__ == "__main__":
    main()
