[Русский](KEYS.md) · **English**

# Keys and where they live

yaseo runs on your own keys, and you don't need all of them right away. The site check, AI-search readiness and the fix plan work with no keys at all. Positions, search results and Wordstat need two Yandex values; everything else is optional.

The `yaseo …` commands below are given in short form. If you haven't installed yaseo with `uv tool install`, write this instead of `yaseo`: `uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.3 yaseo …`.

## Where yaseo looks for keys

For each key, the first source that has it wins:

1. the process environment variables;
2. the file whose path is set in `YASEO_ENV_FILE`;
3. `~/.config/yaseo/.env` (or `$XDG_CONFIG_HOME/yaseo/.env`).

`yaseo init` writes to the third file and restricts it to the owner only (permissions 0600). When yaseo writes a key itself, for example a refreshed Yandex token, it strips group and other permissions from the file. If the file is a symbolic link, the write goes to the real file, and the link stays in place.

Worth knowing:

- yaseo doesn't read a `.env` in the current directory. The MCP server starts from whatever project directory you're working in, and that project's `.env` might hold someone else's keys: OpenAI, Yandex for a different project. A file of your own outside `~/.config/yaseo/` is only used when you point to it explicitly, with `YASEO_ENV_FILE`.
- AI provider keys can go in the file or in an environment variable. The process environment is checked first for every key the package knows about: Yandex, Yandex ID OAuth, `PERPLEXITY_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, and every `YASEO_*` variable.
- An environment variable overrides the file. If one is set, editing the file changes nothing, and `yaseo init` warns you about it.

File format: `NAME=value` lines, no spaces around `=`:

```
YC_FOLDER_ID=b1g...
YANDEX_AI_STUDIO_API_KEY=AQVN...
YASEO_DOMAIN=example.ru
```

## Yandex: search results and Wordstat

| variable | what it is | doesn't work without it |
|---|---|---|
| `YC_FOLDER_ID` | folder ID in Yandex AI Studio | Wordstat, search results, competitiveness, positions, briefs, article checks, Yandex generative answer |
| `YANDEX_AI_STUDIO_API_KEY` | secret value of the API key | same |

Where to get them (official guide: https://aistudio.yandex.ru/ru/docs/search-api/quickstart/, in Russian):

1. Sign in to AI Studio (https://aistudio.yandex.cloud/platform/) with your Yandex ID.
2. Create an organization. The cloud and the `default` folder appear on their own.
3. Link a billing account with a card. Its status should become `ACTIVE` or `TRIAL_ACTIVE`.
4. "Create API key" («Создать API-ключ») → validity period → "Create" («Создать»). Copy the **secret value**: once the window closes, it won't be shown again. This is `YANDEX_AI_STUDIO_API_KEY`.
5. Hover over the folder name at the top of the screen and click the copy icon (https://aistudio.yandex.ru/ru/docs/ai-studio/quickstart/, in Russian). This is `YC_FOLDER_ID`.

Along with the key, AI Studio creates a service account with a Search API role. If Yandex answers 403, check that the key was created in the folder whose ID you entered, and that the billing account is active. For the generative answer, the service account needs the `search-api.webSearch.user` role. If a check failed with 403 before, `geo_providers` suggests what to do.

Prices are in Yandex's price list: https://aistudio.yandex.ru/ru/docs/search-api/pricing (in Russian). Limits: https://aistudio.yandex.ru/ru/docs/search-api/concepts/limits (in Russian).

## Yandex ID: Webmaster and Metrica (optional)

| variable | what it is |
|---|---|
| `YANDEX_OAUTH_CLIENT_ID` | ClientID of your app on oauth.yandex.ru |
| `YANDEX_OAUTH_CLIENT_SECRET` | Client secret of the same app |
| `YANDEX_OAUTH_TOKEN` | OAuth token, written automatically after the code exchange |
| `YANDEX_OAUTH_REFRESH` | refresh token, issued together with the OAuth token |

Without them, `yaseo webmaster` and `yaseo metrika` don't work. The AI Studio key doesn't cover Webmaster and Metrica; they use a separate kind of access.

Full steps are in [INSTALL.en.md](INSTALL.en.md), step 7. In short:

1. Create an app: https://oauth.yandex.ru/client/new. Platform: "Web services" («Веб-сервисы»), Redirect URI: `https://oauth.yandex.ru/verification_code`. Access: `webmaster:hostinfo`, `webmaster:verify` (https://yandex.ru/dev/webmaster/doc/ru/tasks/how-to-get-oauth, in Russian) and `metrika:read` (https://yandex.com/dev/metrika/en/intro/authorization).
2. Put the ClientID and Client secret into `~/.config/yaseo/.env`.
3. Open `https://oauth.yandex.ru/authorize?response_type=code&client_id=<ClientID>`, allow access, and copy the confirmation code.
4. Run `yaseo yandex --exchange <code>`. The token and refresh token are written automatically.
5. Check: `yaseo yandex --check`.

The token lasts about six months. When Yandex answers 401, yaseo refreshes it itself.

## AI providers (optional)

Needed only for `geo_check_visibility`. `geo_readiness` works with no keys, and the Yandex generative answer uses the Yandex keys from the section above.

| variable | provider in yaseo | provider docs |
|---|---|---|
| `PERPLEXITY_API_KEY` | `perplexity` | https://docs.perplexity.ai/docs/getting-started/overview |
| `OPENAI_API_KEY` | `openai` | https://developers.openai.com/api/docs/guides/tools-web-search |
| `GEMINI_API_KEY` | `gemini` | https://ai.google.dev/gemini-api/docs/generate-content/google-search |
| `ANTHROPIC_API_KEY` | `anthropic` | https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool |

You create the key in the provider's own dashboard. `geo_providers` shows the current list with links and prices.

If `ANTHROPIC_API_KEY` or another such key is already sitting in your environment for some other reason, yaseo treats that provider as configured. That alone spends nothing: a paid check only runs with `confirm=true` and an explicit `providers` list. `geo_providers` shows where each key came from.

You pay the provider yourself, at your own account's rate. yaseo doesn't guess prices for providers outside Russia; in the estimate it writes "at your account's rate with the provider".

## Settings (optional)

| variable | what it sets |
|---|---|
| `YASEO_DOMAIN` | your site's default domain |
| `YASEO_DB` | path to the SQLite database |
| `YASEO_ENV_FILE` | your own key file |
| `YASEO_USE_PROXY` | `1`: reach Yandex through the system proxy (direct by default) |
| `YASEO_TRACKING_MAX_CALLS` | cap on calls per position-tracking run, 1000 by default |
| `YASEO_TRACKING_MIN_INTERVAL_DAYS` | days to wait between runs for the same domain, 13 by default (`yaseo track --run` and `run_tracking`); override once with `--now` in the CLI or `now=true` in MCP |
| `YASEO_TRACKING_FILE` | tracker settings file |
| `YASEO_ARTICLES_MAX_CALLS` | cap on calls per `check_articles`/`yaseo articles --check` run, 100 by default |
| `YASEO_ALLOW_PRIVATE` | `1`: let the audit and readiness check reach internal addresses (localhost, `10.0.0.0/8`, `192.168.0.0/16` and similar); refused by default. `yaseo audit` and `yaseo plan` have the same switch as the `--allow-private` flag |
| `YASEO_RATES`, `YASEO_RATE_SEARCH_API`, `YASEO_RATE_WORDSTAT` | rates for the cost estimate |

## How to change or remove a key

- Redo it: `yaseo init`. Enter keeps the current value; a new value replaces the old one.
- By hand: open `~/.config/yaseo/.env` in a text editor and fix the line.
- Revoke a Yandex key: delete it in AI Studio, and the old value stops working everywhere.
- Remove everything: delete `~/.config/yaseo/`. Collected data lives separately, in `~/.local/share/yaseo/`.

After a change, call `whoami` for Yandex keys and `geo_providers` for AI provider keys. Both show where each key is coming from now.

## Security

- Don't paste keys into the chat with Claude. `yaseo init` asks for keys with hidden input.
- Don't commit a `.env` with keys to git.
- yaseo never prints a key in full; the output shows only its length and first characters.
