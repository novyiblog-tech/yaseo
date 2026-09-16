[Русский](INSTALL.md) · **English**

# How to install yaseo: step by step

yaseo is an open-source SEO tool for Yandex. It works inside Claude Code: you write in plain words, "check my site", and Claude calls the right tools and replies with tables backed by evidence.

This guide is written for people who have never used a terminal. Every step ends with a check. If a check fails, don't move on: see "If something goes wrong" at the end.

## 0. What you need

- **Claude Code** and a Claude subscription that includes it: Pro, Max, Team, Enterprise, or a Console account. The free plan does not include Claude Code.
- **A bank card for Yandex Cloud.** Yandex charges for search and Wordstat requests according to its own price list: https://aistudio.yandex.ru/ru/docs/search-api/pricing (in Russian). yaseo itself is free. Before any paid step, Claude tells you how many requests it will make and waits for your "yes".
- **About 15 minutes.**

**Before you start, good to know:**

- The Yandex key (step 3) is needed for search positions, Wordstat and the Yandex generative answer. Without it, the site audit and the AI-readiness check still work, and the AI providers from step 8 (Perplexity, OpenAI, Gemini, Claude) work with their own keys.
- The tools currently produce their output in Russian. Claude answers you in your language.

A terminal is a window where you type commands. On a Mac: the Terminal app, which you can find with Spotlight (Cmd + Space, type "Terminal"). On Windows: PowerShell, found through the Start menu. Copy the whole command, paste it, and press Enter.

## 1. Install Claude Code

Official guide: https://code.claude.com/docs/en/setup

**Mac and Linux**, in the terminal:

```
curl -fsSL https://claude.ai/install.sh | bash
```

**Windows**, in PowerShell:

```
irm https://claude.ai/install.ps1 | iex
```

Then close the terminal, open a new one, and sign in:

```
claude
```

A browser window opens; sign in to your Claude account. You can leave Claude Code with the `/exit` command.

**Check.** In a new terminal window:

```
claude --version
```

You should see a version number, for example `2.1.211 (Claude Code)`. If the terminal says `command not found`, see item 1 in "If something goes wrong".

## 2. Install uv

uv is a small program that downloads and runs yaseo. If your computer doesn't have a suitable Python, uv downloads one by itself. Official guide: https://docs.astral.sh/uv/getting-started/installation/

**Mac and Linux:**

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows** (PowerShell):

```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close the terminal and open a new one.

**Check:**

```
uv --version
git --version
```

Both commands should print a version number. `git` is needed to download yaseo from GitHub.

- On a Mac without `git`, the system will offer to install the developer tools by itself. Accept, wait for the installation to finish, and repeat the check.
- On Windows, get `git` here: https://git-scm.com/downloads/win

## 3. Get a Yandex key

The key is issued by Yandex AI Studio. Official guide: https://aistudio.yandex.ru/ru/docs/search-api/quickstart/ (in Russian)

1. Open https://aistudio.yandex.cloud/platform/ and sign in with Yandex ID. This is an ordinary Yandex account, the same one you'd use for Yandex Mail.
2. **Create an organization.** Enter an organization name and a cloud name, then click "Open AI Studio" («Открыть AI Studio»). A folder named `default` is created automatically.
3. **Link a card.** Click "Link billing account" («Привязать платежный аккаунт») in the top right → "Add card" («Добавить карту») → card number, expiry date, CVV → "Link" («Привязать»). The billing account status should become `ACTIVE` or `TRIAL_ACTIVE`.
4. **Create an API key.** Click "Create API key" («Создать API-ключ») in the top right → choose how long it's valid → "Create" («Создать»). Copy the **secret key** and keep it somewhere safe, for example in a password manager. Once you close the window, it will not be shown again.
5. **Separately, copy the folder ID.** Hover over the folder name at the top of the screen and click the copy icon (as described in https://aistudio.yandex.ru/ru/docs/ai-studio/quickstart/, in Russian). Save it next to the key.

**Check.** You have two different values written down:
- the folder ID: a short string, usually starting with `b1g`;
- the secret key: a long string.

These are different things. The ID of the key itself, which AI Studio also shows, is not needed here.

## 4. Install the plugin in Claude Code

Start Claude Code:

```
claude
```

Inside Claude Code, enter these two commands one after the other:

```
/plugin marketplace add novyiblog-tech/yaseo
```

```
/plugin install yaseo@yaseo
```

The plugin card opens. Choose to install it for yourself across all projects (**User scope**) and confirm. If Claude Code says `Run /reload-plugins to activate.`, enter:

```
/reload-plugins
```

**Check.** Enter `/mcp`. The list should include a `yaseo` server, marked as coming from a plugin, with its connection status. The first start can take a minute: during that time uv downloads yaseo and, if needed, Python. If the server shows an error, see item 2 in "If something goes wrong".

## 5. Enter your keys

Open a **separate** terminal window (not inside Claude Code) and run:

```
uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.2 yaseo init
```

The program will ask, one at a time, for:

1. `YC_FOLDER_ID`: paste the folder ID from step 3.
2. `YANDEX_AI_STUDIO_API_KEY`: paste the secret key. Input is hidden, so no characters appear on screen; that's expected. Paste it and press Enter.
3. `YASEO_DOMAIN`: your site's address without `https://`, for example `example.ru`. You can skip this by pressing Enter.

The keys are saved to `~/.config/yaseo/.env`, a file only your user account can open. At the end, the program offers one live check: a single paid Wordstat request, and it tells you the price itself. You can accept (`y`) or decline (Enter).

**Check.** The output contains this line ("Search results and Wordstat: ready"):

```
Выдача и Wordstat: готово
```

If you accepted the live check, the last line will be «Wordstat отвечает: … Всё готово.» ("Wordstat responds: … All set.").

## 6. Your first conversation

Go back to Claude Code. If it was open during step 5, restart it: `/exit`, then `claude`.

Write:

> Check whether yaseo is set up

Claude will call the `whoami` tool and summarize the answer. It should show «Выдача и Wordstat: готово» ("Search results and Wordstat: ready"), the path to the database, and your domain. On the first call, Claude Code may ask for permission to use the yaseo tool. Allow it.

Then:

> Check my site example.ru

Replace `example.ru` with your own site. Here's what should happen:

- a technical audit: a table of issues with severity, page, evidence, and advice on what to do. The audit is free; it only reads your site's pages;
- AI-search readiness: whether your site is open to AI bots, whether it has `llms.txt` and structured markup. Also free;
- for positions, Claude will ask which queries people should find you by, and **before measuring, it will tell you how many Yandex requests it will make**. The measurement starts only after your "yes".

More examples:

> Build a keyword set for the "apartment renovation" section
> Prepare an article brief for the query "how to choose laminate flooring"
> What are our positions and what has changed?
> Does AI search cite example.ru for the query "where to order an apartment renovation"?
> Give me a plan to improve example.ru's positions

For the last one, Claude will put together a plan of fixes: what is blocking indexing, which pages are already close to the top of the results, where you get impressions but no clicks. Every item comes with evidence and ready-to-follow instructions. The plan is free: it reads your site and whatever has already been collected in the database. The more has been collected (positions, the Webmaster export from step 7), the more complete the plan.

## 7. Optional: Yandex Webmaster and Yandex Metrica

You need this if you want to see impressions and clicks from Webmaster and visitor behavior from Metrica. For now, these reports are started from the terminal, not from the conversation with Claude. Claude can run them for you if you ask.

The commands below start with `uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.2 yaseo`. If you'd like to type less, run `uv tool install git+https://github.com/novyiblog-tech/yaseo@v0.1.2` once, and from then on plain `yaseo` is enough.

1. **Create an app** at https://oauth.yandex.ru/client/new (steps as in the Webmaster help: https://yandex.ru/dev/webmaster/doc/ru/tasks/how-to-get-oauth, in Russian):
   - name: anything you like;
   - platform: "Web services" («Веб-сервисы»);
   - Redirect URI: `https://oauth.yandex.ru/verification_code`;
   - data access: `webmaster:hostinfo`, `webmaster:verify` and `metrika:read` (Metrica permissions: https://yandex.com/dev/metrika/en/intro/authorization).
2. **Copy the app's ClientID and Client secret** and add them to the keys file. Mac: `open -e ~/.config/yaseo/.env`. Windows: `notepad $HOME\.config\yaseo\.env`. Add two lines and save:
   ```
   YANDEX_OAUTH_CLIENT_ID=your_ClientID
   YANDEX_OAUTH_CLIENT_SECRET=your_Client_secret
   ```
3. **Get a code.** Open this address in your browser, with your own ClientID filled in:
   ```
   https://oauth.yandex.ru/authorize?response_type=code&client_id=your_ClientID
   ```
   Allow access. Yandex will show a confirmation code. Copy it.
4. **Exchange the code for a token:**
   ```
   uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.2 yaseo yandex --exchange CODE
   ```
   The token and the refresh token are saved to the same file automatically. The token lasts about six months and is renewed automatically.

**Check:**

```
uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.2 yaseo yandex --check
```

The lines «Метрика — счётчики» (Metrica: counters) and «Вебмастер — пользователь» (Webmaster: user) should be marked `[OK]`. The line about Direct isn't needed for SEO; you can ignore its `[FAIL]`.

## 8. Optional: AI provider keys

The "does AI search cite my site" check works with the Yandex generative answer using the same keys as in step 3. Perplexity, OpenAI, Gemini and Claude can be added with their own keys. Each of them is optional.

Open the keys file as in step 7 and add the lines you need:

```
PERPLEXITY_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...
ANTHROPIC_API_KEY=...
```

Instead of the file, you can set environment variables; yaseo reads those too.

**Check.** Ask Claude:

> Which AI providers are configured in yaseo?

Claude will call `geo_providers` and show a table: "provider · configured · missing keys · price per call".

About money:

- the visibility check always shows a cost estimate first and spends nothing;
- it starts spending only after you have named the providers yourself and agreed;
- the Yandex generative answer is expensive: as of 16.09.2026 it costs 5,080 RUB per 1,000 requests, about 5 RUB each;
- for providers outside Russia, the price depends on your plan with them.

AI answers change from one run to the next. A single run is a snapshot, not a verdict.

## If something goes wrong

**1. `command not found: claude` or `command not found: uv`.**
Close the terminal and open a new one: installers add the program's path, and the old window doesn't know about it. If that doesn't help, on Mac and Linux run `ls ~/.local/bin`. If `claude` or `uv` is there, repeat the installation from steps 1–2 and read what the installer prints at the end: it usually names the command you need to run. Claude Code has a dedicated page: https://code.claude.com/docs/en/troubleshoot-install

**2. In `/mcp` the `yaseo` server shows an error, or Claude says there are no yaseo tools.**
First check that yaseo downloads and runs. In a regular terminal:
```
uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.2 yaseo --version
```
- If the answer is `yaseo 0.1.2`, yaseo itself is fine. Most likely Claude Code can't see `uv`: this happens when Claude Code wasn't started from a terminal. Close Claude Code, open a new terminal, and start `claude` from it.
- An error about `git`: go back to step 2 and install `git`.
- A network error: check your internet connection and try again.

After fixing it, restart Claude Code or enter `/reload-plugins`.

**3. Claude says «Не заданы ключи: YC_FOLDER_ID, YANDEX_AI_STUDIO_API_KEY» ("Keys not set: YC_FOLDER_ID, YANDEX_AI_STUDIO_API_KEY").**
Repeat step 5. If after that `whoami` shows the key's source as «окружение процесса» ("process environment"), a variable with that name is already set somewhere in your system settings, and it overrides the file. Remove it or put the correct value there.

**4. Yandex responds with error 401 or 403.**
Most often it's one of three things:
- the key's ID was entered instead of the folder ID;
- the key was created in a different folder;
- the billing account is not active.

Check all three in AI Studio and repeat step 5: pressing Enter keeps the previous value, a new value replaces the old one.

**5. Wordstat returns nothing or an error after a series of requests.**
Wordstat has a quota: 100 requests per hour (https://aistudio.yandex.ru/ru/docs/search-api/concepts/limits, in Russian). Wait an hour or split your list. If the errors look like `SSL: UNEXPECTED_EOF_WHILE_READING`, the connection to Yandex is being dropped along the way. yaseo connects to Yandex directly, bypassing the system proxy, but a VPN intercepts all traffic: turn it off and try again. And remember: a zero in Wordstat doesn't prove nobody searches for that query. Wordstat simply doesn't show rare phrasings.

---

More on keys, files and settings: [KEYS.en.md](KEYS.en.md). What yaseo can do and which tools cost money: [README](../README.en.md).
