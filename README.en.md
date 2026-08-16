# BriefMe · 今日简讯

*[中文](README.md)*

BriefMe pulls the **last 24 hours** of news from around twenty Chinese and
international outlets, has an AI sort it into categories and write short briefs,
and generates a web page you can open offline. One command a day, five minutes
to read. A button in the top-right switches the whole site between 中文 and English.

```
Home                 China Top 5 / World Top 5 (one slot per event, not per article)
 ├─ China News       four-column grid of 15 categories, filter by outlet on the left
 │   └─ World Politics   AI event brief + every report in that category
 └─ World News       original headline stays; the translation goes underneath
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

Five stages. Each one writes its output to a local SQLite database, so any
stage can be re-run on its own:

| Stage | What it does | Notes |
|---|---|---|
| **1. Fetch** | RSS / Google News / per-site parsing for a few outlets | Takes **headline, short excerpt, link** only — never the full article |
| **2. Translate** | English headline → Chinese | The original headline is never modified; the translation is stored in a separate column |
| **3. Classify** | 1–2 categories per article | Headline first; only reads the excerpt when the headline is not enough |
| **4. Aggregate** | Picks the Top 5 **events** per region | Several outlets covering one story count as one event; more outlets ranks it higher |
| **5. Summarise + render** | One brief per category, then static HTML | Plain static pages, no server required |

Some deliberate trade-offs:

- **24 hours only.** The database keeps history, but the pages and the AI input
  always use the last 24 hours, measured by the outlet's **publication** time,
  not when the article was fetched.
- **Timestamps stored in UTC**, converted to your local time in the browser.
- **Briefs are computed up front**, not on click, so navigation is instant and
  the AI cost stays predictable.
- **Static output.** `output/` is just HTML — open it in a browser, copy it to a
  USB stick, or drop it on any static host.

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

- **Reachable** → all 20 sources are fetched normally
- **Not reachable** → it says so, skips the 14 overseas sources, and fetches the
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

---

## Project layout

```
config/sources.yaml     the source list (add sources here)
newsagg/
  fetch.py              fetching + the overseas connectivity probe
  scrapers.py           per-site parsing (respects robots.txt)
  classify.py           categories    translate.py  foreign -> Chinese headlines
  events.py             Top 5 events  summarize.py  category briefs
  english.py            --en mode: translates the Chinese output into English
  render.py             static site   ai.py         AI provider registry
  manual.py             export/load for --manual mode
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
