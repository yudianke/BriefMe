"""一键流水线：抓取 -> 翻译 -> AI 分类 -> Top5 事件 -> 分类纪要 -> 渲染。

用法:
  python run.py            # 完整跑一遍（调用 API，需 .env 里有 provider key）
  python run.py --manual   # Claude Code 模式：抓取+导出待办+渲染，不调用任何 API
  python run.py --no-ai    # 只抓取+渲染，不做 AI 也不导出待办
  python run.py --cn-only  # 只抓国内源（无法访问外网时用，避免长时间超时）
  python run.py --all      # 跳过连通性探测，强制抓全部源（确定自己能上外网时用）
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
    t0 = time.time()
    total = len(STEPS) + 2

    print("=" * 54, f"\n[1/{total}] 抓取（窗口 {WINDOW_HOURS}h）")
    fetch.run(hours=WINDOW_HOURS,
              cn_only="--cn-only" in sys.argv,
              skip_probe="--all" in sys.argv)

    if manual:
        from newsagg import manual as mn
        print("\n[Claude Code 模式] 导出待办，不调用任何 API")
        counts = mn.export()
        for k, v in counts.items():
            print(f"  {k:<10} {v}")
        print(f"\n  待办已写入 {mn.MANUAL}")
        print("  下一步：在 Claude Code 里说「处理 pachong/manual 的待办」")
    elif no_ai:
        print("\n[跳过 AI] --no-ai")
    else:
        import importlib
        for i, (label, mod_name, fn_name) in enumerate(STEPS, start=2):
            print(f"\n[{i}/{total}] {label}")
            try:
                fn = getattr(importlib.import_module(mod_name), fn_name)
                fn(hours=WINDOW_HOURS) if fn_name != "build_all" else fn(WINDOW_HOURS)
            except RuntimeError as e:      # 没有可用 provider
                print(f"  [跳过] {e}")
                break
            except Exception as e:         # 限流等中断：已完成的部分照常渲染
                print(f"  [中断] {type(e).__name__}: {e}\n  已完成的部分会照常渲染，重跑可续。")

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
