[Русский](INSTALL.md) · **English**

# How to install yaseo: step by step

yaseo works inside Claude Code. You write in plain words, something like "check example.ru," and Claude calls the right tools itself and replies with a plan of fixes backed by evidence.

This guide is for people who've never used a terminal before. Every step ends with a check. If a check fails, stop and look at "If something goes wrong" at the end.

## 0. What you need

- Claude Code, and a Claude subscription that includes it: Pro, Max, Team, Enterprise, or a Console account. The free plan doesn't include Claude Code.
- About ten minutes for the first site check.
- A card for Yandex Cloud, needed later, only if you want positions, search results and Wordstat (step 5). The first site check is free.

A terminal is a window for typing commands. On a Mac, it's the Terminal app: press Cmd + Space and type "Terminal". On Windows, it's PowerShell, found in the Start menu. Copy the whole command, paste it, and press Enter.

## 1. Install Claude Code

Official guide: https://code.claude.com/docs/en/setup

Mac and Linux, in the terminal:

```
curl -fsSL https://claude.ai/install.sh | bash
```

Windows, in PowerShell:

```
irm https://claude.ai/install.ps1 | iex
```

Close the terminal, open a new one, and sign in:

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

uv is a small program that downloads and runs yaseo. If your computer doesn't have a suitable Python, uv downloads one itself. Official guide: https://docs.astral.sh/uv/getting-started/installation/

Mac and Linux:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):

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

- On a Mac without `git`, the system offers to install the developer tools by itself. Accept, wait for it to finish, and repeat the check.
- On Windows, get `git` here: https://git-scm.com/downloads/win

## 3. Install the plugin

Start Claude Code:

```
claude
```

Inside Claude Code, enter these two commands one after another:

```
/plugin marketplace add https://github.com/novyiblog-tech/yaseo.git
```

```
/plugin install yaseo@yaseo
```

The plugin card opens. Choose to install it for yourself across all projects (**User scope**) and confirm. If Claude Code says `Run /reload-plugins to activate.`, enter:

```
/reload-plugins
```

**Check.** Enter `/mcp`. The list should include a `yaseo` server, marked as coming from a plugin, with its connection status. The first start can take a minute: uv is downloading yaseo, and Python too if needed. If the server shows an error, see item 2 in "If something goes wrong".

## 4. First site check

No keys are needed for this step. Write in Claude Code:

```text
Check example.ru
```

Replace `example.ru` with your own site. On the first call, Claude Code will ask permission to use yaseo's tools; allow it.

What happens:

- Claude crawls up to 30 pages of the site, with pauses and following robots.txt rules;
- it checks whether the site lets AI search bots in, whether `llms.txt` exists, and whether structured markup is there;
- it puts together a plan of fixes: what to do first, on which page, and why;
- it tells you nothing was spent: yaseo only talked to your site.

**Check.** The reply contains a plan with items and a line saying nothing was spent. Claude might add that positions and demand data will show up once you connect Yandex: that's expected, it's the next step.

If Claude has access to the site's code, you can ask it to act on a plan item. It shows the changes first and saves them only after you agree.

## 5. Connect Yandex

This step is for positions, live search results, Wordstat and the Yandex generative answer. Yandex charges for these calls at its own price list: https://aistudio.yandex.ru/ru/docs/search-api/pricing (in Russian). Before every paid step, Claude tells you how many calls it will make and waits for your yes.

The key comes from Yandex AI Studio. Official guide: https://aistudio.yandex.ru/ru/docs/search-api/quickstart/ (in Russian)

1. Open https://aistudio.yandex.cloud/platform/ and sign in with Yandex ID. This is an ordinary Yandex account, the same one you'd use for Yandex Mail.
2. Create an organization: enter a name for it and a cloud name, then click "Open AI Studio" («Открыть AI Studio»). A folder named `default` is created automatically.
3. Link a card: click "Link billing account" («Привязать платежный аккаунт») in the top right, then "Add card" («Добавить карту»), then enter the card number, expiry date and CVV, then "Link" («Привязать»). The billing account status should become `ACTIVE` or `TRIAL_ACTIVE`.
4. Create an API key: click "Create API key" («Создать API-ключ») in the top right, choose how long it's valid, then "Create" («Создать»). Copy the **secret key** and keep it somewhere safe, like a password manager. Once you close the window, it won't be shown again.
5. Copy the folder ID: hover over the folder name at the top of the screen and click the copy icon (as described at https://aistudio.yandex.ru/ru/docs/ai-studio/quickstart/, in Russian). Save it next to the key.

**Check.** You should have two different values written down:
- the folder ID: a short string, usually starting with `b1g`;
- the secret key: a long string.

AI Studio also shows the ID of the key itself. You don't need that one here, so don't mix them up.

## 6. Enter your keys

Open a **separate** terminal window, not inside Claude Code, and run:

```
uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.3 yaseo init
```

The program asks, one at a time, for:

1. `YC_FOLDER_ID`: paste the folder ID from step 5.
2. `YANDEX_AI_STUDIO_API_KEY`: paste the secret key. Input is hidden, so nothing appears on screen; that's expected. Paste it and press Enter.
3. `YASEO_DOMAIN`: your site's address without `https://`, for example `example.ru`. You can skip this with Enter.

The keys are saved to `~/.config/yaseo/.env`, a file only your user account can open. At the end, the program offers one live check: a single paid Wordstat request, and it names the price itself. Accept with `y`, decline with Enter.

**Check.** The output contains this line:

```
Выдача и Wordstat: готово
```

("Search results and Wordstat: ready.") If you accepted the live check, the last line will be «Wordstat отвечает: … Всё готово.» ("Wordstat responds: … all set.")

Go back to Claude Code. If it was already open, restart it: `/exit`, then `claude`. Write:

```text
Check whether yaseo is set up
```

Claude will call the `whoami` tool. The reply should show «Выдача и Wordstat: готово», the path to the database, and your domain.

Now you can ask for more:

```text
Check example.ru and get positions for "apartment renovation" and "full renovation"
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
Does AI search cite example.ru for the query "where to order an apartment renovation"?
```

The more you've already collected, positions, the Webmaster export from step 7, the fuller the plan of fixes.

## 7. Optional: Yandex Webmaster and Yandex Metrica

You need this if you want to see impressions and clicks from Webmaster and visitor behavior from Metrica. For now these reports run from the terminal. Claude can run them for you if you ask.

The commands below start with `uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.3 yaseo`. To type less, run `uv tool install git+https://github.com/novyiblog-tech/yaseo@v0.1.3` once, and after that plain `yaseo` is enough.

1. Create an app at https://oauth.yandex.ru/client/new (steps are in the Webmaster help: https://yandex.ru/dev/webmaster/doc/ru/tasks/how-to-get-oauth, in Russian):
   - name: anything you like;
   - platform: "Web services" («Веб-сервисы»);
   - Redirect URI: `https://oauth.yandex.ru/verification_code`;
   - data access: `webmaster:hostinfo`, `webmaster:verify` and `metrika:read` (Metrica permissions: https://yandex.com/dev/metrika/en/intro/authorization).
2. Copy the app's ClientID and Client secret and add them to the keys file. Mac: `open -e ~/.config/yaseo/.env`. Windows: `notepad $HOME\.config\yaseo\.env`. Add two lines and save:
   ```
   YANDEX_OAUTH_CLIENT_ID=your_ClientID
   YANDEX_OAUTH_CLIENT_SECRET=your_Client_secret
   ```
3. Get a code. Open this address in your browser, with your own ClientID:
   ```
   https://oauth.yandex.ru/authorize?response_type=code&client_id=your_ClientID
   ```
   Allow access. Yandex shows a confirmation code; copy it.
4. Exchange the code for a token:
   ```
   uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.3 yaseo yandex --exchange CODE
   ```
   The token and the refresh token are saved to the same file automatically. The token lasts about six months and renews itself.

**Check:**

```
uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.3 yaseo yandex --check
```

The lines `Метрика — счётчики` and `Вебмастер — пользователь` (printed exactly like this) should be marked `[OK]`. The line about Direct isn't needed for SEO; you can ignore its `[FAIL]`.

## 8. Optional: AI provider keys

The "does AI search cite my site" check uses the Yandex generative answer with the keys from step 6. Perplexity, OpenAI, Gemini and Claude are added with their own keys, each optional.

Open the keys file as in step 7 and add the lines you need:

```
PERPLEXITY_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...
ANTHROPIC_API_KEY=...
```

Instead of the file, you can set environment variables; yaseo reads those too.

**Check.** Ask Claude:

```text
Which AI providers are configured in yaseo?
```

Claude will call `geo_providers` and show a table: provider, configured or not, which keys are missing, price per call.

About money:

- the visibility check always shows an estimate first and spends nothing;
- it starts spending only once you've named the providers yourself and agreed;
- the Yandex generative answer is expensive: as of 2026-09-16 it costs 5,080 RUB per 1,000 requests, about 5 RUB each;
- for providers outside Russia, the price depends on your plan with them.

AI answers change from one run to the next. A single run is a snapshot, not a verdict.

## If something goes wrong

**1. `command not found: claude` or `command not found: uv`.**
Close the terminal and open a new one: the installer adds the program's path, and the old window doesn't know about it. If that doesn't help, on Mac and Linux run `ls ~/.local/bin`. If `claude` or `uv` is there, repeat the install from steps 1–2 and read what the installer prints at the end: it usually names the command to run. Claude Code has its own page: https://code.claude.com/docs/en/troubleshoot-install

**2. In `/mcp` the `yaseo` server shows an error, or Claude says there are no yaseo tools.**
First check that yaseo downloads and runs. In a regular terminal:
```
uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.3 yaseo --version
```
- The answer `yaseo 0.1.3` means yaseo itself is fine. Most likely Claude Code can't see `uv`; this happens when Claude Code wasn't started from a terminal. Close it, open a new terminal, and start `claude` from there.
- An error about `git`: go back to step 2 and install `git`.
- A network error: check your connection and try again.
- `/plugin marketplace add` or `/plugin install` says `Failed to clone`, `SSL_ERROR_SYSCALL` or `Host key verification failed`: the connection to GitHub dropped, or the address went over SSH. Make sure the command has the full address `https://github.com/novyiblog-tech/yaseo.git`, and try again.

After fixing it, restart Claude Code or enter `/reload-plugins`.

**3. Claude says «Не заданы ключи: YC_FOLDER_ID, YANDEX_AI_STUDIO_API_KEY» ("Keys not set").**
That's normal for the site check and the plan; they work without keys. For positions, search results and Wordstat, do steps 5 and 6. If after step 6 `whoami` shows the key's source as "process environment", a variable with that name is already set somewhere on your system, and it overrides the file. Remove it, or put the right value there instead.

**4. Yandex answers with error 401 or 403.**
Usually it's one of three things:
- the key's own ID was entered instead of the folder ID;
- the key was created in a different folder;
- the billing account isn't active.

Check all three in AI Studio and repeat step 6. Enter keeps the previous value; a new value replaces the old one.

**5. Wordstat returns nothing, or an error, after a series of requests.**
Wordstat has a quota: 100 requests an hour (https://aistudio.yandex.ru/ru/docs/search-api/concepts/limits, in Russian). Wait an hour, or split your list. Errors like `SSL: UNEXPECTED_EOF_WHILE_READING` mean the connection to Yandex is dropping along the way. yaseo reaches Yandex directly, bypassing the system proxy, but a VPN intercepts all traffic: turn it off and try again. And remember, a zero in Wordstat doesn't mean nobody searches for that query; Wordstat just doesn't show rare phrasings.

---

More on keys, files and settings: [KEYS.en.md](KEYS.en.md). What yaseo can do and which tools cost money: [README](../README.en.md).
