[Русский](README.md) · **English**

# yaseo

Open-source SEO and AI-search visibility toolkit for Yandex (Russian web), packaged as an MCP server and a Claude Code plugin.

**SEO and AI-search visibility for sites on the Russian web, right inside your Claude.**

yaseo is a set of tools that Claude calls on its own: search volume from Wordstat, live Yandex search results, positions with history, a technical audit, an article brief, and a check of whether AI search cites your site. It runs on your own Yandex keys and, optionally, on AI provider keys for the AI-search check. The code is open, free to use, MIT-licensed.

A conversation looks like this:

> Check whether yaseo is set up.
> Check my site example.ru.
> Build a keyword set for the "apartment renovation" section.
> Does AI search cite example.ru for the query "where to order a renovation"?
> Give me a plan to improve example.ru's positions.

Step-by-step installation for people opening a terminal for the first time: **[docs/INSTALL.en.md](docs/INSTALL.en.md)**.

## Using yaseo outside Russia

Some features work without any Yandex account:

- **Site audit** (`audit_site`) and **AI-search readiness check** (`geo_readiness`): both free, no keys needed.
- **AI citation check** (`geo_check_visibility`) via Perplexity, OpenAI, Gemini and Claude, using your own keys for those providers.
- **Action plan** (`get_action_plan`): makes no new paid requests; it builds on the free checks and on whatever data is already in the local database.

Other features need Yandex access:

- **SERP positions, Wordstat and the Yandex generative answer** need a Yandex Cloud (Yandex AI Studio) account with billing: `YC_FOLDER_ID` and `YANDEX_AI_STUDIO_API_KEY`.
- **Webmaster and Metrica** need a Yandex OAuth token.

Tool output (tables, messages) is currently in Russian. Claude answers in your language, but the raw tables arrive in Russian.

---

## What it does

**Keywords.** Wordstat search volume, expansions of a seed phrase (refinements and related queries), and suggested candidates for the tracking pool with a reason for each.

**SERP and competitors.** Live Yandex results for a query; who keeps appearing in the top across a set of queries (split into sites and large platforms); query competitiveness broken down by factor; which queries a competitor ranks in the top for while you are absent.

**Positions.** Query tracking, measurements with history, queries that went up and down, history for a single query.

**Technical audit.** Indexing, metadata, headings, canonical URLs, structured data, internal linking. Every finding comes with evidence and advice on what to do.

**Articles and cannibalization.** A pre-writing brief based on what the top results contain. Blog article measurement shows whether that specific article ranks or another page of the site ranks instead.

**Webmaster and Metrica.** Impressions, clicks and indexing from the site owner's side, behavior on landing pages. Requires a Yandex OAuth token. Exported from the command line (`yaseo webmaster`, `yaseo metrika`). There is no separate export among the MCP tools, but `get_action_plan` reads the already exported data from the database.

**Action plan.** A prioritized list of site fixes: each fix has dated evidence, a ready-made instruction for Claude or a developer, and a way to verify it. Details in the [Action plan](#action-plan) section.

**AI visibility.** Site readiness for AI search without keys: `llms.txt`, AI bot access in robots.txt, JSON-LD, FAQ markup. A check of whether the Yandex generative answer, Perplexity, OpenAI, Gemini and Claude cite the site, with which URL, and whom they cite instead.

## How it works

- **Yandex data comes from Yandex itself.** yaseo gets Wordstat and search results through the official Yandex Search API, with Yandex regions (all of Russia by default).
- **A tracker position is the median of several snapshots, wherever there is anything to take a median of.** Identical queries in a row can return positions 11, 6 and 5, so a phrase that has already been in the top (or a new one) is captured several times (three by default). A phrase that has never been in the top is captured once: three "not in top" results in a row would add nothing to the first one. "Cold" phrases therefore have a single snapshot. By default the tracker looks at the top 10.
- **Every finding has evidence.** The audit, competitiveness and brief show what each conclusion is based on.
- **"No data" instead of a made-up number.** If a source returned nothing, yaseo says so.
- **Cost estimates are not built in uniformly.** `run_tracking`, `check_articles` and `geo_check_visibility` count the number of calls themselves and stop before spending if the count exceeds the cap (or, for the AI-search check, without your `confirm`). For the other paid tools, the estimate before the call is given by the skill agent, not the code; details in [MCP tools](#mcp-tools).
- **Your data stays with you.** Everything collected is stored in a local SQLite database.
- **Built for agents.** The MCP server is pure Python with no external dependencies, standard library only.

## Installation

You need [Claude Code](https://code.claude.com/docs/en/setup) and [uv](https://docs.astral.sh/uv/getting-started/installation/). Detailed instructions, with a check at every step: [docs/INSTALL.en.md](docs/INSTALL.en.md).

### 1. Claude Code plugin (main path)

In Claude Code:

```
/plugin marketplace add https://github.com/novyiblog-tech/yaseo.git
/plugin install yaseo@yaseo
```

The plugin connects the `yaseo` MCP server and seven skills: `yaseo-setup`, `seo-site-check`, `keyword-research`, `position-tracking`, `content-brief`, `improve-positions`, `ai-visibility`. The server is started with `uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.2 yaseo-mcp`, so `uv` must be installed on the machine.

Then the keys:

```
uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.2 yaseo init
```

### 2. MCP server only, via uvx

For Claude Code without the plugin, or for another MCP client:

```
claude mcp add --transport stdio yaseo -- uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.2 yaseo-mcp
```

In other clients, specify the `uvx` command with the arguments `--from git+https://github.com/novyiblog-tech/yaseo@v0.1.2 yaseo-mcp`.

A permanent `yaseo` command in the terminal:

```
uv tool install git+https://github.com/novyiblog-tech/yaseo@v0.1.2
yaseo --help
```

### 3. From source

```
git clone https://github.com/novyiblog-tech/yaseo
cd yaseo
uv run yaseo --help
uv run yaseo-mcp < /dev/null   # the server starts and exits immediately: a startup check
```

Checking the server manually:

```
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | uv run yaseo-mcp
```

## Keys

Two values are required: `YC_FOLDER_ID` and `YANDEX_AI_STUDIO_API_KEY` from Yandex AI Studio. The other keys are optional: Webmaster and Metrica, AI providers. Where to get each key, where it is stored and how to change it: **[docs/KEYS.en.md](docs/KEYS.en.md)**.

The keys are yours, and so are the costs of the calls: Yandex charges according to its price list (https://aistudio.yandex.ru/ru/docs/search-api/pricing (in Russian)), foreign providers according to your account's plan. yaseo shows a cost estimate before spending. One figure for reference: the Yandex generative answer is noticeably more expensive than regular search results; as of 16.09.2026 it costs 5,080 RUB per 1,000 requests.

## MCP tools

"Spends" means which paid API the tool calls and how many times per invocation. Tools marked "no" either read the local database or only access your own site. Where there is more than one call because of result depth (the `n` or `depth` parameter), depth is fetched in pages of 10: top 10 is 1 call, top 30 is 3, top 50 (the maximum) is 5.

| tool | what it does | spends |
|---|---|---|
| `whoami` | where the keys were loaded from, what is missing, where the database is, the default domain | no |
| `research_keywords` | expand 1–5 phrases via Wordstat: search volume and related queries | Wordstat, 1 per phrase |
| `get_keyword_metrics` | search volume and competitiveness for up to 25 queries | Wordstat and Search API, 1 each per query |
| `get_serp_results` | live Yandex results for a query | Search API, per page of depth `n` (default top 10 is 1) |
| `find_serp_competitors` | who keeps appearing in results across a set of up to 10 queries | Search API, 1 per query |
| `get_competition` | competitiveness broken down by factor, with evidence | Search API, 1 per query |
| `build_brief` | pre-writing research: top results, formats, gaps, sub-queries | Wordstat 1, Search API per page of depth `n` (default top 10 is 1) |
| `expand_query_pool` | candidates for the tracking pool, with reasons; saved when `apply=true` | Wordstat, 1 |
| `track_query` | add queries to tracking or remove them | no |
| `run_tracking` | capture positions for tracked queries and write them to history; stops before the first request and explains why if the number of calls would exceed the cap (`YASEO_TRACKING_MAX_CALLS`) or the domain was measured recently (override once with `now=true`) | Search API, up to 3 snapshots per query × pages of depth (default top 10 is 1 call per snapshot) |
| `get_positions` | current positions, change since the previous measurement | no |
| `get_position_history` | position history for a query and competitiveness trend | no |
| `get_competitor_keywords` | where a competitor is in the top and you are not, based on collected snapshots | no |
| `audit_site` | technical site audit; http/https only, private addresses only with `YASEO_ALLOW_PRIVATE=1` | no |
| `check_articles` | blog article measurement: whether that specific article ranks; stops before the first request and explains why if the number of calls would exceed the cap (`articles_max_calls`/`YASEO_ARTICLES_MAX_CALLS`) | Search API, per page of depth per unique query; a query shared by several articles is paid once, not once per article |
| `get_article_effectiveness` | article report and list of cannibalizations | no |
| `get_storage_stats` | what has been collected in the database | no |
| `get_action_plan` | prioritized site fix plan: evidence, instruction for Claude, verification method | no: HTTP only to the site itself, everything else from the database |
| `geo_readiness` | site readiness for AI search; http/https only, private addresses only with `YASEO_ALLOW_PRIVATE=1` | no |
| `geo_providers` | which AI providers are configured and which keys are missing | no |
| `geo_check_visibility` | whether AI search cites the site; without `confirm`, only an estimate | only with `confirm=true` and a `providers` list |
| `geo_history` | history of visibility checks: citation share, frequent competitors | no |

**Cost estimates: some in code, some in the skill.** Only three tools have a built-in estimate and cap in the code itself, stopping BEFORE the first paid request: `run_tracking`, `check_articles` and `geo_check_visibility` (the last one simply spends nothing without your `confirm`). The other paid tools, `get_keyword_metrics` (up to 25 requests to Search API and Wordstat), `get_serp_results`, `find_serp_competitors`, `get_competition`, `build_brief`, `research_keywords` and `expand_query_pool`, spend as soon as they are called: they have no brake of their own in the code, and the estimate before the call comes from the skill agent, not the code.

About the `yandex` provider in `geo_check_visibility`: this is the YandexGPT generative answer based on Search results (`POST /v2/gen/search` in the Yandex Search API), not "Neuro" itself or Alice. Every source in the answer has a `used` flag. "Source shown" and "source used in the answer" are different things: in a live run on 16.09.2026, 3 of 5 sources were used. yaseo counts a site as cited only if its source was used.

## Action plan

`get_action_plan` (in the terminal, `yaseo plan --domain example.ru`) builds a list of site fixes from what has already been measured or can be checked for free: the technical audit, AI-search readiness, position history, Webmaster exports, the article registry and collected SERP snapshots. The tool makes no new paid requests. What is not in the database is not in the plan either: instead of an item, it names the tool that would provide the data, along with its cost estimate.

The order is set by named rules; there is no overall score:

1. **Indexing**: critical audit findings: a page is not served, a broken internal link, a redirect chain, noindex or Disallow on a URL from sitemap.xml. Also a robots.txt that is not served.
2. **Quick wins**: a query is already shown, but not in the top spots: per Webmaster, average position 4–15; per the tracker, position 4–10, because the tracker captures the top 10 (the tracker requires known demand). The page is improved for the query; the current title and H1 are quoted.
3. **Snippet**: at least 30 impressions, zero clicks, position no lower than 10th.
4. **Cannibalization**: two pages of the site compete for the same query.
5. **AI search**: robots.txt blocks AI bots, no `llms.txt`, no JSON-LD. Blocking Google-Extended does not affect Google Search, and the plan says so.
6. **Competitor gaps**: a competitor appears in the collected results for a query with known search volume, and the site has no page for that query.
7. **Everything else**: other audit findings, one item per type.

Each item contains: the page; the current state (quote or number, source, measurement date); what to do; an "Instruction for Claude" block that can be handed to your own Claude or a developer; how to verify afterwards; when to expect an effect. Re-measuring positions is suggested no earlier than the interval set in the tracker settings. The plan does not promise position growth.

Parameters: `domain`, `url` (the address to audit), `max_pages` (30 by default), `sections` (which sections to build), `limit` (10 items by default, the rest collapse into a per-section counter) and `fresh_audit`. By default the audit is re-run if there is none in the database or it is older than 7 days. A fresh audit is saved to the database.

Working through the plan and executing it one item at a time is described in the `improve-positions` skill.

## Settings

| variable | what it sets | default |
|---|---|---|
| `YASEO_DOMAIN` | site domain for "our" positions | not set; if the database has one project, its domain is used |
| `YASEO_DB` | path to the SQLite database | `~/.local/share/yaseo/yaseo.db` |
| `YASEO_ENV_FILE` | your own key file | not set |
| `YASEO_USE_PROXY` | `1`: reach Yandex through the system proxy | `0`: connect to Yandex directly |
| `YASEO_TRACKING_MAX_CALLS` | cap on Search API calls per position run | `1000` |
| `YASEO_TRACKING_MIN_INTERVAL_DAYS` | days between runs for the same domain; enforced by both `yaseo track --run` and MCP `run_tracking`; to override once: `--now` in the CLI, `now=true` in MCP | `13` |
| `YASEO_TRACKING_FILE` | tracker settings file | `~/.config/yaseo/tracking.json` |
| `YASEO_ARTICLES_MAX_CALLS` | cap on Search API calls per `check_articles`/`yaseo articles --check` run | `100` |
| `YASEO_ALLOW_PRIVATE` | `1`: allow the audit and AI-search readiness check to access internal addresses (localhost, `10.0.0.0/8`, `192.168.0.0/16` and similar); for `yaseo audit` and `yaseo plan` the `--allow-private` flag does the same | `0`: refuse |
| `YASEO_RATES` | your own rates file for estimates | built-in `rates.json` |
| `YASEO_RATE_SEARCH_API`, `YASEO_RATE_WORDSTAT` | rate per call, in RUB, for estimates | from `rates.json` |

Data directories follow `XDG_DATA_HOME` and `XDG_CONFIG_HOME` if they are set.

## Security

Properties of version 0.1.2:

- **External data is shown as data, not as commands.** Site content during an audit and AI provider answers during a visibility check go into the output as a separate, clearly labeled block; instructions inside them are not executed.
- **A key is never sent to another domain.** Requests carrying a key or token go over https only and do not follow redirects; this is enforced by the package's shared network layer (`net.py`).
- **The scanner only uses http and https.** Private and local addresses (localhost, `10.0.0.0/8`, `192.168.0.0/16` and similar) are accessed only with explicit permission: `YASEO_ALLOW_PRIVATE=1` or the `--allow-private` flag for `yaseo audit` and `yaseo plan`. The default is to refuse. Numeric internal addresses (`127.0.0.1`, `[::1]`, `10.0.0.5`) are always recognized, while hostnames are checked by the address they resolve to, and behind a proxy in fake-ip mode (addresses `198.18.0.0/15`) this check does not work: any hostname gets a substitute address.

## Privacy

Where data goes:

- **Yandex** (`searchapi.api.cloud.yandex.net`): your queries for Wordstat, search results and the generative answer, with your key.
- **Yandex ID, Webmaster, Metrica** (`oauth.yandex.ru`, `api.webmaster.yandex.net`, `api-metrika.yandex.net`): only if you connected OAuth. The token check `yaseo yandex --check` also queries the Direct API (`api.direct.yandex.com`) to tell whether the token has access to it.
- **AI providers** (`api.perplexity.ai`, `api.openai.com`, `generativelanguage.googleapis.com`, `api.anthropic.com`): only if you added their key, selected the provider and confirmed the check. The query text is sent. For Gemini, yaseo additionally resolves source redirect links with a `HEAD` request.
- **Sites you check**: the audit and the AI readiness check read their pages.

There is no telemetry. yaseo sends no information about itself, about you or about your work anywhere other than the addresses listed. This can be verified in the code: all network calls go through the shared transport `net.py`, which is called by `yandex_serp.py`, `wordstat_client.py`, `yandex_auth.py`, `audit.py`, `geo/providers.py` and `geo/readiness.py`.

What is stored on your machine:

- keys: `~/.config/yaseo/.env`, owner-only access;
- database: `~/.local/share/yaseo/yaseo.db`: SERP snapshots, positions, articles, AI provider answers (answer text and raw API response);
- Wordstat snapshots and reports: next to the database, in `~/.local/share/yaseo/`.

Keys are never printed in full in any output.

## Limitations

- **No link weight.** Competitiveness is calculated from the composition of search results; yaseo does not see links.
- **Result depth is capped at top 50.** Deeper positions make no difference for the package's tasks, while the cost grows linearly: every 10 positions is one more paid call.
- **Wordstat: 100 requests per hour** under the Yandex quota. Large lists need to be split into parts.
- **A zero in Wordstat does not prove there is no demand.** Wordstat may not show rare phrasings.
- **The Yandex generative answer is expensive** and accepts no more than one request per second.
- **AI answers change.** One run is a snapshot. Repeated runs and history give a stable picture.
- **The `yandex` provider is not "Neuro".** It is a generative answer based on Search results from the Search API.
- **Webmaster and Metrica** are currently available only from the command line.
- **Yandex limits and prices change.** Check https://aistudio.yandex.ru/ru/docs/search-api/concepts/limits (in Russian) and https://aistudio.yandex.ru/ru/docs/search-api/pricing (in Russian).

## License

MIT, see [LICENSE](LICENSE).
