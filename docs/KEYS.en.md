[Русский](KEYS.md) · **English**

# Keys and where they live

yaseo runs on your own keys. Two Yandex values are required; the rest are optional.

The `yaseo …` commands below are given in short form. If you did not install yaseo with `uv tool install`, write this instead of `yaseo`: `uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.2 yaseo …`.

## Where yaseo looks for keys

For each key, the first source that has it wins:

1. the process environment variables;
2. the file whose path is set in `YASEO_ENV_FILE`;
3. `~/.config/yaseo/.env` (or `$XDG_CONFIG_HOME/yaseo/.env`).

`yaseo init` writes to the third file and restricts access to the owner only (0600). When yaseo writes a key itself (for example, a refreshed Yandex token), it removes group and other permissions from the file and writes through a symbolic link into the real file, without replacing the link itself.

Things to keep in mind:

- **The `.env` in the current directory is not read.** The MCP server starts from the directory of whatever project you are working in, and that project's `.env` file (with unrelated keys: OpenAI, Yandex for another project) must not silently become a key source for yaseo. A file of your own outside `~/.config/yaseo/` is used only when set explicitly, via `YASEO_ENV_FILE`.
- **AI provider keys can go either in the file or in an environment variable.** The process environment is the first source for every key the package knows about (Yandex, Yandex ID OAuth, `PERPLEXITY_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`), the same as for `YASEO_*`.
- If an environment variable is set, it overrides the file. Editing the file then changes nothing, and `yaseo init` warns you about it.

File format: `NAME=value` lines, with no spaces around `=`:

```
YC_FOLDER_ID=b1g...
YANDEX_AI_STUDIO_API_KEY=AQVN...
YASEO_DOMAIN=example.ru
```

## Yandex: search results and Wordstat (required)

| variable | what it is | what does not work without it |
|---|---|---|
| `YC_FOLDER_ID` | folder ID in Yandex AI Studio | Wordstat, search results, competition, positions, briefs, article checks, Yandex generative answer |
| `YANDEX_AI_STUDIO_API_KEY` | secret value of the API key | same |

Where to get them. Official guide: https://aistudio.yandex.ru/ru/docs/search-api/quickstart/ (in Russian)

1. Sign in to AI Studio (https://aistudio.yandex.cloud/platform/) with your Yandex ID.
2. Create an organization. The cloud and the `default` folder are created automatically.
3. Link a billing account with a card. Its status must be `ACTIVE` or `TRIAL_ACTIVE`.
4. "Create API key" («Создать API-ключ») → validity period → "Create" («Создать»). Copy the **secret value**: once the window is closed, it cannot be shown again. This is `YANDEX_AI_STUDIO_API_KEY`.
5. Hover over the folder name at the top of the screen and click the copy icon (https://aistudio.yandex.ru/ru/docs/ai-studio/quickstart/ (in Russian)). This is `YC_FOLDER_ID`.

Along with the key, AI Studio creates a service account with a role for Search API. If Yandex returns 403, check that the key was created in the same folder whose ID you entered, and that the billing account is active. For the generative answer, the service account needs the `search-api.webSearch.user` role. If the previous check failed with 403, `geo_providers` shows a hint.

Prices follow Yandex's price list: https://aistudio.yandex.ru/ru/docs/search-api/pricing (in Russian). Limits: https://aistudio.yandex.ru/ru/docs/search-api/concepts/limits (in Russian).

## Yandex ID: Webmaster and Metrica (optional)

| variable | what it is |
|---|---|
| `YANDEX_OAUTH_CLIENT_ID` | ClientID of your app on oauth.yandex.ru |
| `YANDEX_OAUTH_CLIENT_SECRET` | Client secret of the same app |
| `YANDEX_OAUTH_TOKEN` | OAuth token; written automatically after the code exchange |
| `YANDEX_OAUTH_REFRESH` | refresh token; issued together with the OAuth token |

Without them, `yaseo webmaster` and `yaseo metrika` do not work. Webmaster and Metrica do not accept the AI Studio key: it belongs to a different access family.

The procedure is described in [INSTALL.en.md](INSTALL.en.md), step 7. In short:

1. Create an app: https://oauth.yandex.ru/client/new. Platform: "Web services" («Веб-сервисы»); Redirect URI: `https://oauth.yandex.ru/verification_code`. Permissions: `webmaster:hostinfo`, `webmaster:verify` (https://yandex.ru/dev/webmaster/doc/ru/tasks/how-to-get-oauth (in Russian)) and `metrika:read` (https://yandex.com/dev/metrika/en/intro/authorization).
2. Put the ClientID and Client secret into `~/.config/yaseo/.env`.
3. Open `https://oauth.yandex.ru/authorize?response_type=code&client_id=<ClientID>`, grant access, and copy the confirmation code.
4. Run `yaseo yandex --exchange <code>`. The token and the refresh token are written automatically.
5. Check: `yaseo yandex --check`.

The token lives for about six months. When Yandex returns 401, yaseo refreshes the token itself.

## AI providers (optional)

Needed only for `geo_check_visibility`. `geo_readiness` needs no keys. The Yandex generative answer uses the Yandex keys from the first section.

| variable | provider in yaseo | provider docs |
|---|---|---|
| `PERPLEXITY_API_KEY` | `perplexity` | https://docs.perplexity.ai/docs/getting-started/overview |
| `OPENAI_API_KEY` | `openai` | https://developers.openai.com/api/docs/guides/tools-web-search |
| `GEMINI_API_KEY` | `gemini` | https://ai.google.dev/gemini-api/docs/generate-content/google-search |
| `ANTHROPIC_API_KEY` | `anthropic` | https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool |

You create the key in the provider's account dashboard. `geo_providers` shows the current list with links and prices.

If `ANTHROPIC_API_KEY` or another such key is already in your environment for some other reason, yaseo will treat that provider as configured. That alone spends nothing: a paid check runs only with `confirm=true` and an explicit `providers` list. `geo_providers` shows where each key was taken from.

You pay the provider yourself, at your account's rate. yaseo does not make up prices for foreign providers; in the cost estimate it writes «по тарифу вашего аккаунта у провайдера» ("at your account's rate with the provider").

## Settings (optional)

| variable | what it sets |
|---|---|
| `YASEO_DOMAIN` | your site's default domain |
| `YASEO_DB` | path to the SQLite database |
| `YASEO_ENV_FILE` | your own key file |
| `YASEO_USE_PROXY` | `1`: reach Yandex through the system proxy (direct by default) |
| `YASEO_TRACKING_MAX_CALLS` | cap on requests per position-tracking run, 1000 by default |
| `YASEO_TRACKING_MIN_INTERVAL_DAYS` | minimum interval between runs for the same domain, 13 days by default; enforced by both `yaseo track --run` and MCP `run_tracking` (to bypass it once: `--now` in the CLI, `now=true` in MCP) |
| `YASEO_TRACKING_FILE` | tracker settings file |
| `YASEO_ARTICLES_MAX_CALLS` | cap on requests per `check_articles`/`yaseo articles --check` run, 100 by default |
| `YASEO_ALLOW_PRIVATE` | `1`: allow the audit and the AI-search readiness check to hit internal addresses (localhost, `10.0.0.0/8`, `192.168.0.0/16` and the like); refused by default. `yaseo audit` and `yaseo plan` have the same switch as the `--allow-private` flag |
| `YASEO_RATES`, `YASEO_RATE_SEARCH_API`, `YASEO_RATE_WORDSTAT` | rates for the cost estimate |

## How to change or remove a key

- Re-run: `yaseo init`. Pressing Enter keeps the current value; a new value replaces the old one.
- By hand: open `~/.config/yaseo/.env` in a text editor and edit the line.
- Revoke a Yandex key: delete it in AI Studio. The old value then stops working everywhere.
- Remove everything: delete `~/.config/yaseo/`. Accumulated data is stored separately, in `~/.local/share/yaseo/`.

After a change, call `whoami` for Yandex keys and `geo_providers` for AI provider keys. Both show where each key is now taken from.

## Security

- Do not paste keys into the chat with Claude. `yaseo init` asks for keys with hidden input.
- Do not commit a `.env` with keys to git.
- yaseo never prints keys in full: the output shows only the length and the first characters.
