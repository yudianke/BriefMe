"""要闻简报：跨地区的主题式提要，只盯政治/军事/经济层面的重大冲突与进展。

与「分类纪要」的区别，是这个页面存在的理由：
  - 分类纪要按 (地区,分类) 切片，一个分类一段，覆盖面全但没有取舍；
  - 要闻简报横跨中国与国际两个来源池，**只挑最重要的几件事**，
    并且盯的是「事情往哪走」——冲突升级到哪一步、谈判卡在哪、谁做了什么让局势变了。

素材取自 BRIEFING_CATEGORIES 所列分类，剔除已判定为琐碎的条目。
"""
from __future__ import annotations

from . import db
from .models import BRIEFING_CATEGORIES, WINDOW_HOURS

MAX_TOK = 1800
MAX_TOK_RETRY = 2600   # 输出被截断时才启用；Groq 按 max_tokens 预扣 TPM，常规不宜给大

_SYSTEM = (
    "你在为长期关注地缘政治的读者做 24 小时要闻筛选。他们已知背景，"
    "只想知道「哪几件事真的推动了局势，分别有谁在报」。\n"
    "\n"
    "**必须全部用简体中文输出**（人名、地名、机构用通用中文译名）。\n"
    "\n"
    "── 最重要的一条规则 ──\n"
    "**你拿到的绝大多数材料只有一行标题，没有正文。**\n"
    "因此：只能复述标题里已经写明的内容，一个字都不许多。具体来说，材料没写的\n"
    "人名、职务、机构名、数字、地点、时间、场合、因果关系，**一律不准出现**。\n"
    "宁可写得短而空，也不许补全细节——补出来的都是假的。\n"
    "举例：材料只说「俄罗斯以反战言论判处一名反对派政客 11 年」，那就只能这么写，\n"
    "不能加上他叫什么、是哪一级法院判的、是不是议员。\n"
    "英文标题里的词义也不许反转：stockpiling 是增加储备，不是投放储备。\n"
    "\n"
    "── 选题 ──\n"
    "只写政治、军事、经济层面的重大冲突与进展，重点区域是中国、美国、欧洲、"
    "俄乌、中东。优先级依次是：\n"
    "  1) **多家媒体独立报道同一件事**——这是最强的重要性信号，务必先把材料通读一遍，\n"
    "     把讲同一件事的条目跨分类、跨语言合并起来（中英文标题措辞差别很大，\n"
    "     要按事实判断而非字面）。合并后媒体家数最多的，就排在最前面。\n"
    "  2) 有正文的条目优先于只有标题的——前者你能写出实质内容，后者只能复述。\n"
    "  3) 事件本身是否改变了力量对比、谈判格局或政策方向。\n"
    "**尽量不要选只有单一媒体报道、且只有标题的条目**：那种你只能把标题换个说法，\n"
    "对读者没有增量。宁可少写一段，也不要凑数。\n"
    "人事变动、单家媒体的评论文章、例行会晤、企业财报一律不写。\n"
    "自然灾害、事故、社会新闻、天气也不写——除非它直接造成了政治或军事后果。\n"
    "\n"
    "── 写法 ──\n"
    "分 5-7 段，每段一件事，段首用「■ 」加一个不超过 16 字的短标题，换行后写正文。\n"
    "正文只写 1-2 句，把多家媒体标题里的信息合并成一句完整陈述即可；\n"
    "材料给的信息少，就只写一句短的。\n"
    "每段结尾必须用括号标注**报道该事件的全部媒体**，如（路透/BBC/新华社）——\n"
    "媒体名只能从材料的方括号里照抄，不许出现材料中没有的媒体。\n"
    "\n"
    "严格禁止：评论、立场、预测、影响分析、意义拔高，以及「显示」「凸显」「表明」\n"
    "「加剧了」「暗示」「引发广泛关注」「值得关注的是」这类带判断的连接。\n"
    "只陈述发生了什么，不解释它意味着什么。不要开场白，不要结语。全文不超过 600 字。\n"
    "\n"
    "最后再强调一次语言：**正文每一句都必须是简体中文**。素材大部分是英文标题，"
    "你要把它们译成中文再写，绝不能原样照抄英文句子——这一条实测最容易被忽略。"
)


def _is_chinese(text: str, floor: float = 0.35) -> bool:
    """正文是否确实是中文。

    段首小标题几乎总是中文，正文却可能整段照抄英文，所以按全文汉字占比判断：
    中文简报里汉字通常占七成以上，英文正文＋中文小标题只有一两成。
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    cjk = sum(1 for c in letters if "一" <= c <= "鿿")
    return cjk / len(letters) >= floor


def build(hours: int = WINDOW_HOURS, force: bool = False) -> int:
    """生成一份要闻简报。返回 1 表示写入，0 表示跳过或失败。"""
    from .ai import complete

    if not force and not db.briefing_stale(hours):
        print("  要闻简报：素材变化不大，沿用上一份。")
        return 0

    rows = db.briefing_material(hours)
    if not rows:
        print("  要闻简报：窗口内没有相关分类的报道，跳过。")
        return 0

    # 按分类分组给材料：让模型看得见「哪些报道属于同一议题」，便于跨媒体合并
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["cat"], []).append(r)
    blocks = []
    for cat in BRIEFING_CATEGORIES:
        items = by_cat.get(cat)
        if not items:
            continue
        lines = []
        for a in items:
            t = a["title"]
            if a["title_zh"]:                     # 外文报道附上中文译题，帮助跨语言合并同一事件
                t = f"{t} / {a['title_zh']}"
            # 明确标注这条有没有正文：让模型自己看得见信息边界在哪，
            # 而不是对着一行标题去想象细节（实测不标注时会编造人名与数字）
            if a["excerpt"]:
                lines.append(f"- [{a['source_name']}] {t}\n    正文开头：{a['excerpt'][:110]}")
            else:
                lines.append(f"- [{a['source_name']}] {t}\n    （仅标题，无正文）")
        blocks.append(f"### {cat}\n" + "\n".join(lines))

    n_no_body = sum(1 for r in rows if not r["excerpt"])
    user = (f"以下是过去 {hours} 小时内，各家媒体在重点分类下的报道"
            f"（按分类分组，同一议题可能跨分类出现）。\n"
            f"共 {len(rows)} 条，其中 {n_no_body} 条**只有标题、没有正文**，"
            f"这些条目只能复述标题原意，不得补充任何细节。\n\n"
            + "\n\n".join(blocks)
            + "\n\n请据此写出这 24 小时的要闻简报。用简体中文写，不要照抄英文原句。")

    text = ""
    for budget in (MAX_TOK, MAX_TOK_RETRY):
        try:
            text = complete("summarize", _SYSTEM, user,
                            max_tokens=budget, temperature=0.2).text.strip()
        except Exception as e:
            print(f"  [要闻简报失败] {type(e).__name__}: {e}")
            return 0
        # 截断的输出往往最后一段没有来源括号，甚至断在半句上；用段数粗判是否完整
        if text.count("■") >= 3 and _is_chinese(text):
            break
        if budget != MAX_TOK_RETRY:
            why = "输出偏短" if text.count("■") < 3 else "正文没用中文"
            print(f"  [要闻简报] {why}（{len(text)} 字符），重试…")

    if text.count("■") < 2:
        print(f"  [要闻简报无结果] 模型只给出 {text.count('■')} 段，保留上一份。原文：{text[:120]!r}")
        return 0
    # 素材全是英文标题时，模型有一定概率直接照抄英文（实测复现过）。
    # 这种输出对中文读者没用，宁可留着上一份也不覆盖。
    if not _is_chinese(text):
        print("  [要闻简报] 模型输出的是英文正文，重试后仍未纠正，保留上一份。")
        return 0

    db.save_briefing(text)
    print(f"  要闻简报已生成（{text.count('■')} 段，{len(text)} 字，取材 {len(rows)} 篇）")
    return 1


if __name__ == "__main__":
    build()
