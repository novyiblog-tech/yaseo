[Русский](README.md) · **English**

# yaseo

Ask Claude "Check example.ru" and get a plan: what to fix on the site, in what order, and why.

yaseo is a free Claude Code plugin. It shows a site from two sides: how Yandex sees it and how AI search sees it. Built for marketers and business owners who already work in Claude Code.

![Claude Code checks a site with yaseo and returns an action plan](docs/demo/site-check.png)

## First check, no keys

No keys, no card, for the first check.

1. Install [Claude Code](https://code.claude.com/docs/en/setup) and [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. In Claude Code, enter these two commands one at a time:
   ```text
   /plugin marketplace add https://github.com/novyiblog-tech/yaseo.git
   ```
   ```text
   /plugin install yaseo@yaseo
   ```
3. Ask Claude:
   ```text
   Check example.ru
   ```

Claude will crawl up to 30 pages, check whether the site lets AI search bots in, and put together a plan of fixes. This costs nothing: yaseo only talks to your site.

New to the terminal? There's a guide with a check after every step: [docs/INSTALL.en.md](docs/INSTALL.en.md).

## What you get

### Action plan

This is the point of the whole project. Each item in the plan has a page, what's on it right now (a quote or a number with the measurement date), what to do, a ready instruction for Claude or a developer, and a way to check the result. Rules set the order: first everything that blocks indexing, then pages that are close to the top of the results. The plan doesn't promise ranking growth.

If Claude has access to the site's code, it can act on an item right away. It shows what it's about to change first and saves only after you agree.

### How Yandex sees you

- Technical audit: indexing, broken links, redirects, title and description, canonical URLs, structured data, internal linking.
- Positions for your queries with measurement history: what went up and what dropped.
- Wordstat demand and query suggestions for a section of the site.
- Live search results, who's in the top, and which queries competitors rank for while you don't.
- An article brief based on what's already in the top, plus a check of whether the right page on the site actually ranks for the article's query.
- Impressions and clicks from Webmaster, behavior from Metrica. These reports currently run from the terminal only.

### How AI search sees you

- Site readiness: whether robots.txt lets AI search bots in, whether `llms.txt` exists, and whether JSON-LD markup is there. Free, no keys.
- Citation: whether the Yandex generative answer, Perplexity, OpenAI, Gemini and Claude cite your site, with which URL, and who gets cited instead. Checks build up into a history.

## When you need keys

The audit, AI-search readiness and the plan work without keys. Positions, live search results, Wordstat and the generative answer come from the official Yandex Search API, and for those you need a Yandex AI Studio key with a card attached. How to get one: [INSTALL.en.md, step 5](docs/INSTALL.en.md#5-connect-yandex). Where the keys live and how to change them: [docs/KEYS.en.md](docs/KEYS.en.md).

Keys go in with one command in the terminal:

```
uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.3 yaseo init
```

You pay for the calls yourself, at Yandex's price list: https://aistudio.yandex.ru/ru/docs/search-api/pricing (in Russian). Before any paid step, Claude tells you how many calls it will make and waits for your yes. The generative answer is the most expensive part: as of 2026-09-16 it costs 5,080 RUB per 1,000 requests. Perplexity, OpenAI, Gemini and Claude keys are optional, and you pay each provider at your own rate.

yaseo itself is free. The code is open, MIT-licensed.

## Using yaseo outside Russia

Some parts of yaseo need no Yandex account at all: the site audit, the AI-search readiness check, the citation check with your own Perplexity, OpenAI, Gemini or Claude keys, and the action plan built from that data.

Positions, live search results, Wordstat and the Yandex generative answer need a Yandex AI Studio account with billing. Webmaster and Metrica need a Yandex OAuth token.

Tool output, tables and messages, is in Russian for now. Claude answers you in your own language, but the raw data underneath comes back in Russian.

## What you can ask

```text
Check example.ru
```

```text
Give me a plan to improve example.ru's rankings
```

```text
Build a keyword set for the "apartment renovation" section
```

```text
Prepare an article brief for the query "how to choose laminate flooring"
```

```text
What are our positions and what changed?
```

```text
Does AI search cite example.ru for the query "where to order a renovation"?
```

---

What follows is for anyone curious how it all works.

## How it works

- Yandex data comes straight from Yandex, through the official Yandex Search API. The default region is all of Russia.
- Results for a single query jump around: three requests in a row can return position 11, then 6, then 5. So a query that has already shown up in the top gets captured three times, and the tracker records the median. A query that has never been in the top gets captured once: two more "not in the top" results wouldn't add anything. The tracker looks at the top 10.
- Every finding comes with evidence. If a source has nothing, yaseo says "no data" instead of making up a number.
- Everything it collects stays with you, in a local SQLite database.
- The MCP server is written in Python and uses only the standard library, no external dependencies.

## Other install methods

The plugin from the section above connects the `yaseo` MCP server and seven skills: `yaseo-setup`, `seo-site-check`, `keyword-research`, `position-tracking`, `content-brief`, `improve-positions`, `ai-visibility`. The server starts with the command `uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.3 yaseo-mcp`, so it needs `uv` to run at all.

### Only the MCP server

For Claude Code without the plugin:

```
claude mcp add --transport stdio yaseo -- uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.3 yaseo-mcp
```

In another MCP client, point it at the `uvx` command with the arguments `--from git+https://github.com/novyiblog-tech/yaseo@v0.1.3 yaseo-mcp`.

A permanent `yaseo` command in the terminal:

```
uv tool install git+https://github.com/novyiblog-tech/yaseo@v0.1.3
yaseo --help
```

### From source

```
git clone https://github.com/novyiblog-tech/yaseo
cd yaseo
uv run yaseo --help
uv run yaseo-mcp < /dev/null   # the server starts and exits right away: that's the startup check
```

Listing the tools by hand:

```
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | uv run yaseo-mcp
```

## MCP tools

The "spends" column names the paid API a tool calls and how many times per call. "No" means the tool reads the local database or only reaches your own site. Result depth is fetched in pages of 10: top 10 is one call, top 30 is three, top 50 (the cap) is five.

| tool | what it does | spends |
|---|---|---|
| `whoami` | where the keys come from, what's missing, where the database is, the default domain | no |
| `research_keywords` | expand 1–5 phrases via Wordstat: search volume and related queries | Wordstat, 1 per phrase |
| `get_keyword_metrics` | search volume and competitiveness, up to 25 queries | Wordstat and Search API, 1 each per query |
| `get_serp_results` | live Yandex results for a query | Search API, per page of depth `n` (top 10 is 1) |
| `find_serp_competitors` | who keeps showing up across a set of up to 10 queries | Search API, 1 per query |
| `get_competition` | competitiveness broken down by factor, with evidence | Search API, 1 per query |
| `build_brief` | pre-writing research: top results, formats, gaps, sub-queries | Wordstat 1, Search API per page of depth `n` (top 10 is 1) |
| `expand_query_pool` | candidates for the tracking pool with a reason; saved when `apply=true` | Wordstat, 1 |
| `track_query` | add queries to tracking or remove them | no |
| `run_tracking` | capture positions and write them to history. Stops before the first request if the calls would exceed the cap `YASEO_TRACKING_MAX_CALLS`, or if the domain was measured recently (override with `now=true`) | Search API, up to 3 snapshots per query × pages of depth (top 10 is 1 call per snapshot) |
| `get_positions` | current positions and the change since the last measurement | no |
| `get_position_history` | position history for a query, competitiveness trend | no |
| `get_competitor_keywords` | where a competitor is in the top and you aren't, from collected snapshots | no |
| `audit_site` | technical site audit; http/https only, private addresses need `YASEO_ALLOW_PRIVATE=1` | no |
| `check_articles` | whether this specific blog article ranks for its query. Stops before the first request if the calls would exceed the cap `articles_max_calls`/`YASEO_ARTICLES_MAX_CALLS` | Search API, per page of depth for each unique query; a query shared by several articles is paid once |
| `get_article_effectiveness` | article report and list of cannibalizations | no |
| `get_storage_stats` | what has been collected in the database | no |
| `get_action_plan` | prioritized fix plan: evidence, instruction for Claude, verification method | no: only reaches the site itself, everything else comes from the database |
| `geo_readiness` | site readiness for AI search; http/https only, private addresses need `YASEO_ALLOW_PRIVATE=1` | no |
| `geo_providers` | which AI providers are configured and which keys are missing | no |
| `geo_check_visibility` | whether AI search cites the site; without `confirm`, returns only an estimate | only with `confirm=true` and a `providers` list |
| `geo_history` | history of checks: citation share, frequent competitors | no |

Only three tools estimate cost in the code itself. `run_tracking` and `check_articles` stop before the first paid request if they'd go over the cap. `geo_check_visibility` spends nothing without `confirm=true`. The other paid tools (`get_keyword_metrics`, `get_serp_results`, `find_serp_competitors`, `get_competition`, `build_brief`, `research_keywords`, `expand_query_pool`) spend as soon as they're called, and Claude gives you the estimate beforehand, following instructions from the skill.

The `yandex` provider in `geo_check_visibility` is the YandexGPT generative answer built on Search results (`POST /v2/gen/search` in the Yandex Search API). Don't confuse it with "Neuro" or Alice. Every source in the answer carries a `used` flag, and a source being shown doesn't mean it was used: in a run on 2026-09-16, 3 of 5 sources made it into the answer. yaseo counts a site as cited only when its source was used.

## How the plan is built

`get_action_plan` (`yaseo plan --domain example.ru` from the terminal) builds a list of fixes from whatever is already measured or free to check: the technical audit, AI-search readiness, position history, Webmaster exports, the article registry, and collected search snapshots. It makes no paid requests. When some piece of data is missing, the plan names the tool that would supply it and what that would cost.

There's no overall score. Rules set the order:

1. Indexing. Critical audit findings: a page that doesn't load, a broken internal link, a redirect chain, noindex or Disallow on a URL from sitemap.xml. Also a robots.txt that doesn't load.
2. Quick wins. A query already shows up, but not high enough: average position 4–15 per Webmaster, or position 4–10 per the tracker (the tracker only sees the top 10, and demand for the query needs to be known). The page gets adjusted for the query, with the current title and H1 quoted.
3. Snippet. At least 30 impressions, zero clicks, position no lower than 10th.
4. Cannibalization. Two pages of the site compete for the same query.
5. AI search. robots.txt blocks AI search bots, no `llms.txt`, no JSON-LD. Blocking Google-Extended doesn't affect Google Search, and the plan notes that.
6. Competitor gaps. A competitor shows up in collected results for a query with known demand, and the site has no page for it.
7. Everything else. Remaining audit findings, one item per type.

Each item lists the page, the current state (a quote or number, source, measurement date), what to do, an "Instruction for Claude" block for your own Claude or a developer, how to verify the result, and when to expect an effect. The plan suggests re-measuring positions no sooner than the interval set in the tracker settings.

Parameters: `domain`, `url` (the address to audit), `max_pages` (30 by default), `sections` (which sections to build), `limit` (10 items by default, the rest collapse into a per-section count) and `fresh_audit`. If there's no audit in the database, or it's older than 7 days, a new one runs and gets saved.

Working through the plan item by item is covered in the `improve-positions` skill.

## Settings

| variable | what it sets | default |
|---|---|---|
| `YASEO_DOMAIN` | site domain for "our" positions | not set; if the database has one project, its domain is used |
| `YASEO_DB` | path to the SQLite database | `~/.local/share/yaseo/yaseo.db` |
| `YASEO_ENV_FILE` | your own key file | not set |
| `YASEO_USE_PROXY` | `1`: reach Yandex through the system proxy | `0`: connect to Yandex directly |
| `YASEO_TRACKING_MAX_CALLS` | cap on Search API calls per position run | `1000` |
| `YASEO_TRACKING_MIN_INTERVAL_DAYS` | days between runs for the same domain; enforced by both `yaseo track --run` and MCP `run_tracking`; override once with `--now` in the CLI or `now=true` in MCP | `13` |
| `YASEO_TRACKING_FILE` | tracker settings file | `~/.config/yaseo/tracking.json` |
| `YASEO_ARTICLES_MAX_CALLS` | cap on Search API calls per `check_articles`/`yaseo articles --check` run | `100` |
| `YASEO_ALLOW_PRIVATE` | `1`: allow the audit and AI-search readiness check to reach internal addresses (localhost, `10.0.0.0/8`, `192.168.0.0/16` and similar); `yaseo audit` and `yaseo plan` have the same switch as the `--allow-private` flag | `0`: refuse |
| `YASEO_RATES` | your own rates file for estimates | built-in `rates.json` |
| `YASEO_RATE_SEARCH_API`, `YASEO_RATE_WORDSTAT` | rate per call, in RUB, for estimates | from `rates.json` |

Data directories follow `XDG_DATA_HOME` and `XDG_CONFIG_HOME` if they are set.

## Security

Built into the code:

- Site content and AI provider answers come back in a separate, clearly labeled block, as data. Claude does not execute instructions found inside them.
- Requests carrying a key or token go over https only and never follow redirects, so a key can't end up on someone else's domain. The shared network layer `net.py` enforces this.
- The scanner only uses http and https. localhost and internal addresses (`10.0.0.0/8`, `192.168.0.0/16` and similar) are reached only with your permission: `YASEO_ALLOW_PRIVATE=1`, or the `--allow-private` flag on `yaseo audit` and `yaseo plan`. Numeric addresses like `127.0.0.1`, `[::1]` and `10.0.0.5` are always blocked. Hostnames are checked against whatever address DNS returns for them. Behind a proxy running fake-ip mode (addresses in `198.18.0.0/15`), that check can't help: every hostname gets a substitute address.

## Privacy

Where data goes:

- Yandex (`searchapi.api.cloud.yandex.net`): your queries for Wordstat, search results and the generative answer, together with your key.
- Yandex ID, Webmaster and Metrica (`oauth.yandex.ru`, `api.webmaster.yandex.net`, `api-metrika.yandex.net`), if you connected OAuth. The token check `yaseo yandex --check` also asks the Direct API (`api.direct.yandex.com`) whether that token has access to it.
- AI providers (`api.perplexity.ai`, `api.openai.com`, `generativelanguage.googleapis.com`, `api.anthropic.com`), if you added their key, picked the provider and confirmed the check. The query text is sent. For Gemini's source links, yaseo resolves redirects with a `HEAD` request.
- Sites you check: the audit and readiness check read their pages.

There's no telemetry. Aside from the addresses above, yaseo sends nothing anywhere. You can verify this in the code: every network call goes through `net.py`, and the only callers are `yandex_serp.py`, `wordstat_client.py`, `yandex_auth.py`, `audit.py`, `geo/providers.py` and `geo/readiness.py`.

What's stored on your machine:

- keys: `~/.config/yaseo/.env`, owner-only access;
- database: `~/.local/share/yaseo/yaseo.db`: search snapshots, positions, articles, AI provider answers (text and the raw API response);
- Wordstat snapshots and reports: alongside it, in `~/.local/share/yaseo/`.

Keys are never printed in full in any output.

## Limitations

- yaseo doesn't see links, so competitiveness is based only on what's in the search results.
- Results deeper than the top 50 aren't captured. For what this package is for, distant positions don't matter, and each extra 10 positions is another paid call.
- Wordstat accepts 100 requests an hour; long lists need to be split up.
- A zero in Wordstat doesn't mean there's no demand: Wordstat can miss rare phrasings.
- The Yandex generative answer is expensive and accepts no more than one request a second.
- AI answers change from run to run. One run is a snapshot; repeats and history give you the real picture.
- Webmaster and Metrica are only available from the terminal for now.
- Yandex's limits and prices change. Check https://aistudio.yandex.ru/ru/docs/search-api/concepts/limits and https://aistudio.yandex.ru/ru/docs/search-api/pricing.

## License

MIT, see [LICENSE](LICENSE).
