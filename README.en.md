# BriefMe · 今日简讯

*[中文](README.md)*

BriefMe pulls the **last 48 hours** of news from twenty-odd Chinese and
international outlets, has an AI sort it into categories and write short briefs,
and generates a web page you can open offline. One command a day, five minutes
to read. A button in the top-right switches the whole site between 中文 and English.

```
Home                 China Top 5 / World Top 5 (one slot per event, not per article)
 ├─ China News       four-column grid of 16 categories, filter by outlet on the left
 │   └─ World Politics   AI event brief + every report in that category
 ├─ World News       original headline stays; the translation goes underneath
 └─ Journals        19 journals across 8 disciplines + five AI picks each for SCI/SSCI
                    (a separate track — it does not touch the news pipeline)
```

> The interface is fully bilingual out of the box. Translating the
> **AI-generated** text as well is opt-in — see [Language switching](#language-switching).

---

## Quick start

```bash
git clone <your repo URL>
cd briefme
pip install -r requirements.txt

cp .env.example .env      # Windows: copy .env.example .env
# edit .env and paste in a key for any one AI provider

python run.py
```

The generated page opens automatically when the run finishes. To read it again
later, double-click `view.bat` — no need to re-run anything.

---

## How it works

The news pipeline has five stages, each writing its output to a local SQLite
database so any stage can be re-run on its own. (Journals are a parallel track
that takes no part in these stages — see the Journals section.)

| Stage | What it does | Notes |
|---|---|---|
| **1. Fetch** | RSS / Google News / per-site parsing for a few outlets | Takes **headline, short excerpt, link** only — never the full article; also filters out non-article pages (see below) |
| **2. Translate** | English headline → Chinese | The original headline is never modified; the translation is stored in a separate column |
| **3. Classify** | 1–2 categories per article | Headline first; only reads the excerpt when the headline is not enough |
| **4. Aggregate** | Picks the Top 5 **events** per region | Several outlets covering one story count as one event; more outlets ranks it higher |
| **5. Summarise + render** | One brief per category, then static HTML | Plain static pages, no server required |

Some deliberate trade-offs:

- **Rolling 48-hour window.** The database keeps history, but the pages and the
  AI input always use the last 48 hours, measured by the outlet's **publication**
  time, not when the article was fetched. 48 rather than 24 so the site reads the
  same across time zones — check it before bed and after waking and you still
  won't miss what was published while you slept. Change `WINDOW_HOURS` in
  `newsagg/models.py` to adjust; Google News sources expand into several
  `when:Nd` queries to match the window automatically (see below).
- **Timestamps stored in UTC**, converted to your local time in the browser.
- **Briefs are computed up front**, not on click, so navigation is instant and
  the AI cost stays predictable.
- **Static output.** `output/` is just HTML — open it in a browser, copy it to a
  USB stick, or drop it on any static host.
- **Non-article pages are dropped before they reach the database.** Google News
  also picks up stock quote pages (`SPYA.N - | Stock Price…`), show and podcast
  pages (`CNN Newsroom`) and topic tags (`Virginia - AP News`) and presents them
  as articles — around 12% of what gets fetched, in practice. They carry no
  content but would still be classified and summarised, so they are discarded at
  fetch time. The rules live in `JUNK_TITLE_PATTERNS` in `newsagg/fetch.py`, they
  deliberately err on the side of letting junk through rather than dropping real
  news, and **every dropped item is logged** so you can audit for false positives
  (see below).

---

## Configuring an AI provider

Put **any one** key in `.env` and it will run. Ten providers are built in, and
the first one with a key configured is used automatically:

**Reachable from mainland China:** DeepSeek, Zhipu GLM, Qwen, MiniMax
**Elsewhere:** ChatGPT, Gemini, Grok, Claude, Groq

To control the order, set `AI_PROVIDERS=deepseek,qwen` — the first is primary,
and the rest are fallbacks.

To add another provider, add one entry to `PROVIDERS` in `newsagg/ai.py`. As long
as it exposes an OpenAI-compatible endpoint, the URL, environment variable name
and model name are all it needs:

```python
"your-provider": Provider(
    name="your-provider",
    base_url="https://api.example.com/v1",
    api_key_env="YOUR_API_KEY",
    models={"default": "model-name"},
),
```

> Model names change over time. If you get a "model not found" error, check the
> provider's docs for the current name and edit that line.

### Free quotas run out

Free tiers have a **daily** token cap on top of the per-minute one — Groq's
`gpt-oss-120b` allows roughly 200k tokens a day. That number is **not in the
response headers**; it only shows up in the error once you hit it. So the program
keeps a local per-day tally:

```bash
python -m newsagg.ai --quota
```

This prints your per-minute remaining tokens, request quota, and today's
cumulative usage from this machine.

When the daily budget is exhausted the program does **not** sit and wait — it says
so explicitly ("used X of Y, back in N minutes"), skips that provider, and still
renders whatever finished. That is deliberately different from per-minute rate
limiting, where waiting a few seconds does help; waiting out a daily cap would
just hang for half an hour.

Rough cost per run: about 90k tokens when 200 new articles come in, about 30k when
only a few dozen do. If you are short on quota, `--no-ai` refreshes just the
headline lists, and `--manual` hands the AI work to a coding assistant instead.

**News and journals share one daily budget, and news comes first.** The paper
translations and picks run after every news step, on whatever is left. The initial
backfill is around a thousand titles, throttled to 300 per run and spread over a few
runs, with the remaining count printed each time. This ordering matters: running
papers first exhausted the quota during testing and left the news with no category
briefs and no Top 5 at all.

When the daily quota does run out, nothing spins. The step stops immediately and says
so, instead of retrying each remaining batch or category against a limit that will not
lift until tomorrow, and whatever finished is still rendered.

---

## Run modes

```bash
python run.py             # full run (calls the AI)
python run.py --cn-only   # mainland Chinese sources only
python run.py --all       # skip the connectivity probe, fetch everything
python run.py --no-ai     # fetch and render only, no AI calls
python run.py --manual    # spend no API credit — hand the AI work to a coding assistant
python run.py --en        # also generate the English version of the AI content
```

### Auditing what got filtered out

Fetching discards non-article pages (stock quotes, show pages, topic tags). The
risk with rules like these is dropping real news, so **every dropped item is
logged together with the rule that caught it**:

```bash
python -m newsagg.fetch --dropped
```

Output is grouped by rule, with **the least-used rules first** — a rule that
caught forty stock quote pages is almost certainly fine, while one that fired
only once or twice is where a false positive would hide. Each entry shows the
outlet, the full headline and the original link, so you can open it and check.

To save it as a file (on Windows, don't use `>` — the console writes Chinese in
cp936 and mangles it):

```bash
python -m newsagg.fetch --dropped --out data/dropped-review.txt
```

If you find real news being dropped, edit the offending entry in
`JUNK_TITLE_PATTERNS` in `newsagg/fetch.py`; the next fetch will let it through.
The log lives in the `dropped_titles` table, deduplicated by URL — a page that
reappears every day occupies one row with a running count.

The reverse also matters: **after you add a rule, pages already in the database
stay there** and keep showing up on the pages. This re-checks the whole database
against the current rules (preview only by default):

```bash
python -m newsagg.fetch --purge-junk        # review the list first
python -m newsagg.fetch --purge-junk --yes  # delete once you're satisfied
```

Anything removed is logged to `dropped_titles` too, so `--dropped` can still show it.

### Journals

The fourth nav item: 19 journals grouped into 8 disciplines, plus five AI-selected
papers each for SCI and SSCI.

**Data comes from the public [Crossref](https://www.crossref.org/) API** — publishers
deposit their own metadata there, so one interface covers every journal, free and
without a key. That means no per-publisher scraping and no robots.txt to negotiate.
Records missing an abstract are topped up from [OpenAlex](https://openalex.org/).

| | Value | Why |
|---|---|---|
| Display window | 90 days | Quarterlies need it: *Demography* publishes 28 papers per 90 days, *Academy of Management Perspectives* 10. A 24-hour window would be empty most days |
| Picks drawn from | 30 days | Over 90 days the list would barely change for months |
| Fetch frequency | once per 24h | Papers do not update hourly |
| Cap per journal | 200 most recent | *Nature* runs to ~1000 per 90 days and would bury *Demography*'s 28 on the same page. Each discipline page shows the journal's **actual** coverage range rather than claiming 90 days |
| Retention | 365 days | ~9,000 rows a year — nothing for SQLite |

**This is a separate track from the news.** Papers live in their own `papers` table
rather than in `articles`, because the translate / classify / Top-5 / summary steps
all query without a region filter — anything landing in `articles` gets swept into
the whole AI pipeline, burning tokens and polluting the news categories. A separate
table is structural isolation: the existing code cannot reach it.

**On the "cited" figure: that is the OpenAlex 2-year mean citedness, not the Clarivate
Journal Impact Factor.** The JIF is proprietary; this project cannot obtain it and
should not substitute another metric while implying otherwise.

**On how far to trust the five picks.** This step is a *selection* task: the model may
only choose from the supplied candidates and write one sentence that stays inside the
title and abstract. Every returned DOI must match a candidate, or the whole batch is
discarded and the previous picks kept. Roughly 70% of records carry no publisher
abstract (Nature, the three IEEE titles and *American Psychologist* supply almost
none), so those are judged on the title alone — the page says so. Journal metrics and
DOI links are emitted directly by the program, never by the model, so every entry can
be opened and checked.

To add a journal, edit `config/journals.yaml` with its ISSN, discipline and
`sci: true/false`. Note that a journal's print and electronic ISSNs are separate
records in Crossref, and picking the wrong one can return data that stopped years ago
(*World Politics* requires the electronic ISSN).

### Language switching

The button in the top-right switches the whole site; your choice is remembered.
There are two layers to it:

| | Needs `--en`? | What it covers |
|---|---|---|
| **Interface text** | No | Navigation, buttons, category names, outlet names, relative timestamps — all bilingual, built in |
| **AI-generated text** | Yes | English headlines for Chinese articles, category briefs, Top 5 events |

Without `--en`, switching to English still gives you an English interface; the
AI-written parts fall back to Chinese. Nothing goes blank and nothing errors.

`--en` works by **translating the Chinese output that was already generated**,
rather than running the whole AI pipeline a second time in English. That keeps
both languages describing the same events in the same order — otherwise the two
Top 5 lists would disagree and the toggle would be confusing — and it costs far
less. The trade-off is roughly 40% more AI calls per run, which is why it is off
by default.

> **The original headline is never replaced.** English reports always show the
> English headline as the main title, Chinese reports always show the Chinese one.
> Translations only ever appear as a subtitle, and which one appears depends on
> the interface language.

### Using `--manual` mode

For when you have no API credit but do have an AI coding assistant.
**Two steps:**

**Step 1 — you run:**

```bash
python run.py --manual
```

Fetching happens as usual, but instead of calling an AI it writes the pending
work into `manual/*.md` — one file each for translation, classification, Top 5
and briefs, each stating its task and output format. Add `--en` here too if you
want the English version; the assistant will be asked for both languages in the
same pass.

**Step 2 — you tell your assistant:**

> Handle the tasks in manual/

It reads those markdown files, writes the results to `manual/results.json`, and
**runs the load command itself**. Refresh the page when it's done.

---

<details>
<summary>If the assistant wrote results.json but didn't load it (manual fallback)</summary>

Run this yourself:

```bash
python -m newsagg.manual load manual/results.json && python -m newsagg.render
```

It does two things: reads the results from `results.json` into the database
(`load`), then regenerates the pages (`render`). Normally the assistant does
this for you and you never need to remember it.

</details>

---

## Notes for users in mainland China

**It works without a proxy — you just won't see the international outlets.**

At startup the program probes whether overseas sites are reachable, over the
same network path used for fetching:

- **Reachable** → all 23 sources are fetched normally
- **Not reachable** → it says so, skips the 17 overseas sources, and fetches the
  6 mainland outlets (Xinhua, CCTV News, Caixin, Yicai, Cankao Xiaoxi,
  Southern Weekly). The pages are generated as usual.

The point is to avoid having every overseas source time out one by one and drag
a single run out for minutes.

**The probe uses exactly the same network path as the fetching** (httpx with
`trust_env`), so proxy environment variables, a Windows system proxy (what
Clash / v2rayN normally set) and TUN/global mode are all detected correctly.
Users with a working connection are not blocked.

If it does get it wrong anyway — a very slow proxy, or routing rules that send
the probe domains direct — use `--all` to skip the probe and fetch everything:

```bash
python run.py --all
```

Conversely, if you know you have no access and want to skip the few seconds the
probe takes, use `--cn-only`.

The same applies to AI providers: DeepSeek, GLM, Qwen and MiniMax are directly
reachable from the mainland, so using one of those needs no proxy at all.

---

## Adding a news source

Edit `config/sources.yaml`:

```yaml
  - id: example                 # unique id
    name: 示例日报               # name shown in the Chinese interface
    name_en: Example Daily      # name shown in the English interface (falls back to name)
    region: china               # china or world
    lang: zh                    # zh or en
    method: rss                 # rss / gnews / scrape
    feeds:
      - https://example.com/rss.xml
```

- `rss` — use this whenever a feed exists
- `gnews` — for a broken or missing feed, fall back to a Google News `site:` query
- `scrape` — when neither works, write a parser for it in `newsagg/scrapers.py`

Then just run `python run.py`. No other code needs to change.

> **Leave `when:1d` as-is in gnews queries — the program expands it.**
> Google News returns at most ~100 results per query. With a 48-hour window the
> obvious move is `when:2d`, but measured, that *reduces* recent coverage: Xinhua
> returns 100 items under `when:1d`, all within 24h; under `when:2d` it is still
> capped at 100 but spread over two days, leaving only 50 within 24h. So each
> query is expanded into `when:1d`, `when:2d`, … and merged by URL: `1d`
> guarantees density near the present, larger `when` values fill in the tail.
> Measured for Xinhua: 155 unique items, 105 of them within 24h — better than
> either query alone. Changing `WINDOW_HOURS` adjusts this automatically.

---

## Project layout

```
config/sources.yaml     the source list (add sources here)
config/journals.yaml    the journal list (add journals here)
newsagg/
  fetch.py              fetching + the overseas connectivity probe
  scrapers.py           per-site parsing (respects robots.txt)
  classify.py           categories    translate.py  foreign -> Chinese headlines
  events.py             Top 5 events  summarize.py  category briefs
  english.py            --en mode: translates the Chinese output into English
  render.py             static site   ai.py         AI provider registry
  manual.py             export/load for --manual mode
  journals.py           journal fetching (Crossref)  paperai.py  paper titles + picks
templates/              page templates + CSS + front-end scripts (i18n.js runs the toggle)
run.py                  entry point   view.bat      opens the generated page
```

> **A note on the source comments.** The comments and docstrings throughout
> `newsagg/` are written in Chinese, as are the prompts sent to the AI — the
> prompts are functional, and rewriting them would change what language the model
> replies in. The code itself, along with all identifiers, is standard Python,
> and this file documents the behaviour of every module.

A fresh clone has empty `data/`, `output/`, `manual/` and `logos/` directories —
those are local artifacts and appear after the first run.

---

## Permissions and privacy

**No administrator rights are required.** A normal `python run.py` is enough and
will not trigger a UAC prompt.

| | |
|---|---|
| **Reads** | Your system proxy settings (read-only, current user); the API key you put in `.env` |
| **Writes** | Only `data/`, `output/` and `manual/` inside the project directory — nothing anywhere else |
| **Network** | Only the news sites, and whichever AI provider you configured in `.env` |

**Why it reads the system proxy:** to decide whether outgoing requests should go
through a proxy. That step is done by the `httpx` / `urllib` standard library, not
by any call in this project's code; `pip`, `requests` and most other tools take the
same path. On Windows it reads the current user's `Internet Settings` — the same
proxy box you see in Internet Options — and the standard library implementation
only queries it, never writes to it.

**What it does not do:** it does not modify system settings or the registry, does
not spawn subprocesses, and does not collect or upload any of your local data.
The news it fetches stays on your own machine.

**About antivirus software:** a `.py` script run from source is not usually
flagged. If you package it into an `.exe` yourself with something like
PyInstaller, you may hit the false positives those packers are known for — that
comes from the packaging, not from this project's code.

There isn't much code. If you'd rather check for yourself, `newsagg/` is a dozen
or so plain Python files.

---

## Content and copyright

- Only **headlines, excerpts of at most 300 characters, and links** are fetched
  and displayed. Full article text is never fetched or stored.
- Every item names its source outlet and links back to the original.
- Sites with a custom parser have their `robots.txt` checked first; disallowed
  paths are not fetched.
- Fetched content stays on your own machine. The repository contains no news data.
- Outlet logos are third-party trademarks and are not distributed with the
  repository. When an image is missing the page falls back to a text badge.
  To add your own, put the files in `logos/` — see the README in that directory.

This project is intended for personal reading. If you deploy it publicly or use
it commercially, check the terms of the outlets involved yourself — their rules
on republication and aggregation are not the same.

The code is MIT licensed; see [LICENSE](LICENSE).
