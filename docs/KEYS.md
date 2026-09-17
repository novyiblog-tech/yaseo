**Русский** · [English](KEYS.en.md)

# Ключи и где они живут

yaseo работает на ваших ключах, и все они нужны не сразу. Проверка сайта, готовность к ИИ-поиску и план правок обходятся без ключей. Для позиций, выдачи и Wordstat нужны два значения Яндекса, остальное добавляется по желанию.

Команды `yaseo …` ниже даны в коротком виде. Если вы не ставили yaseo через `uv tool install`, пишите вместо `yaseo` так: `uvx --from git+https://github.com/novyiblog-tech/yaseo@v0.1.3 yaseo …`.

## Где yaseo ищет ключи

Для каждого ключа берётся первый найденный источник:

1. переменные окружения процесса;
2. файл, путь к которому указан в `YASEO_ENV_FILE`;
3. `~/.config/yaseo/.env` (или `$XDG_CONFIG_HOME/yaseo/.env`).

`yaseo init` пишет в третий файл и открывает его только владельцу (права 0600). Когда yaseo сам дописывает ключ, например продлённый токен Яндекса, он снимает с файла права группы и остальных. Если это символическая ссылка, запись идёт в настоящий файл, а ссылка остаётся на месте.

Что стоит знать:

- Файл `.env` из текущего каталога yaseo не читает. MCP-сервер запускается из каталога любого вашего проекта, и `.env` этого проекта может хранить чужие ключи: OpenAI, Яндекс другого проекта. Свой файл вне `~/.config/yaseo/` подключается только явно, через `YASEO_ENV_FILE`.
- Ключи ИИ-провайдеров можно положить и в файл, и в переменную окружения. Окружение процесса проверяется первым для всех ключей, которые знает пакет: Яндекс, OAuth Яндекс ID, `PERPLEXITY_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` и все `YASEO_*`.
- Переменная окружения перекрывает файл. Если она задана, правка файла ничего не изменит, и `yaseo init` об этом предупредит.

Формат файла: строки `ИМЯ=значение`, без пробелов вокруг `=`:

```
YC_FOLDER_ID=b1g...
YANDEX_AI_STUDIO_API_KEY=AQVN...
YASEO_DOMAIN=example.ru
```

## Яндекс: выдача и Wordstat

| переменная | что это | без неё не работают |
|---|---|---|
| `YC_FOLDER_ID` | идентификатор каталога в Yandex AI Studio | Wordstat, выдача, конкурентность, позиции, брифы, замер статей, генеративный ответ Яндекса |
| `YANDEX_AI_STUDIO_API_KEY` | секретное значение API-ключа | то же |

Где взять (официальная инструкция: https://aistudio.yandex.ru/ru/docs/search-api/quickstart/):

1. Войти в AI Studio (https://aistudio.yandex.cloud/platform/) с Яндекс ID.
2. Создать организацию. Облако и каталог `default` появятся сами.
3. Привязать платёжный аккаунт с картой. Статус должен быть `ACTIVE` или `TRIAL_ACTIVE`.
4. «Создать API-ключ» → срок действия → «Создать». Скопировать **секретное значение**: после закрытия окна его больше не покажут. Это `YANDEX_AI_STUDIO_API_KEY`.
5. Навести указатель на название каталога вверху экрана и нажать значок копирования (https://aistudio.yandex.ru/ru/docs/ai-studio/quickstart/). Это `YC_FOLDER_ID`.

Вместе с ключом AI Studio создаёт сервисный аккаунт с ролью для Search API. Если Яндекс отвечает 403, проверьте, что ключ создан в том каталоге, чей идентификатор вы вписали, и что платёжный аккаунт активен. Для генеративного ответа сервисному аккаунту нужна роль `search-api.webSearch.user`. Если прошлая проверка упала на 403, `geo_providers` подскажет, что делать.

Цены в прайсе Яндекса: https://aistudio.yandex.ru/ru/docs/search-api/pricing. Лимиты: https://aistudio.yandex.ru/ru/docs/search-api/concepts/limits.

## Яндекс ID: Вебмастер и Метрика (по желанию)

| переменная | что это |
|---|---|
| `YANDEX_OAUTH_CLIENT_ID` | ClientID вашего приложения на oauth.yandex.ru |
| `YANDEX_OAUTH_CLIENT_SECRET` | Client secret того же приложения |
| `YANDEX_OAUTH_TOKEN` | OAuth-токен, записывается сам после обмена кода |
| `YANDEX_OAUTH_REFRESH` | токен продления, приходит вместе с OAuth-токеном |

Без них не работают `yaseo webmaster` и `yaseo metrika`. Ключ AI Studio Вебмастер и Метрика не принимают, у них свой доступ.

Подробно в [INSTALL.md](INSTALL.md), шаг 7. Коротко:

1. Создать приложение: https://oauth.yandex.ru/client/new. Платформа «Веб-сервисы», Redirect URI `https://oauth.yandex.ru/verification_code`. Доступы: `webmaster:hostinfo`, `webmaster:verify` (https://yandex.ru/dev/webmaster/doc/ru/tasks/how-to-get-oauth) и `metrika:read` (https://yandex.com/dev/metrika/en/intro/authorization).
2. Вписать ClientID и Client secret в `~/.config/yaseo/.env`.
3. Открыть `https://oauth.yandex.ru/authorize?response_type=code&client_id=<ClientID>`, разрешить доступ, скопировать код подтверждения.
4. Выполнить `yaseo yandex --exchange <код>`. Токен и токен продления запишутся сами.
5. Проверить: `yaseo yandex --check`.

Токен живёт около полугода. Когда Яндекс отвечает 401, yaseo продлевает токен сам.

## ИИ-провайдеры (по желанию)

Нужны только для `geo_check_visibility`. `geo_readiness` работает без ключей, а генеративный ответ Яндекса берёт ключи Яндекса из раздела выше.

| переменная | провайдер в yaseo | справка провайдера |
|---|---|---|
| `PERPLEXITY_API_KEY` | `perplexity` | https://docs.perplexity.ai/docs/getting-started/overview |
| `OPENAI_API_KEY` | `openai` | https://developers.openai.com/api/docs/guides/tools-web-search |
| `GEMINI_API_KEY` | `gemini` | https://ai.google.dev/gemini-api/docs/generate-content/google-search |
| `ANTHROPIC_API_KEY` | `anthropic` | https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool |

Ключ создаётся в личном кабинете провайдера. Свежий список со ссылками и ценами показывает `geo_providers`.

Если `ANTHROPIC_API_KEY` или другой такой ключ уже лежит в окружении для других целей, yaseo сочтёт провайдера настроенным. Денег это само по себе не тратит: платная проверка идёт только с `confirm=true` и явным списком `providers`. Откуда взят ключ, видно в `geo_providers`.

Провайдеру платите вы, по тарифу своего аккаунта. Цены зарубежных провайдеров yaseo не угадывает и в смете пишет «по тарифу вашего аккаунта».

## Настройки (по желанию)

| переменная | что задаёт |
|---|---|
| `YASEO_DOMAIN` | домен вашего сайта по умолчанию |
| `YASEO_DB` | путь к базе SQLite |
| `YASEO_ENV_FILE` | свой файл с ключами |
| `YASEO_USE_PROXY` | `1`: ходить к Яндексу через системный прокси (по умолчанию напрямую) |
| `YASEO_TRACKING_MAX_CALLS` | потолок обращений за прогон позиций, по умолчанию 1000 |
| `YASEO_TRACKING_MIN_INTERVAL_DAYS` | сколько суток выжидать между прогонами по одному домену, по умолчанию 13 (`yaseo track --run` и `run_tracking`); снять ограничение на один раз: `--сейчас` в терминале, `now=true` в MCP |
| `YASEO_TRACKING_FILE` | файл настроек трекера |
| `YASEO_ARTICLES_MAX_CALLS` | потолок обращений за прогон `check_articles` / `yaseo articles --check`, по умолчанию 100 |
| `YASEO_ALLOW_PRIVATE` | `1`: пускать аудит и проверку готовности на внутренние адреса (localhost, `10.0.0.0/8`, `192.168.0.0/16` и подобные); по умолчанию отказ. У `yaseo audit` и `yaseo plan` для этого есть флаг `--allow-private` |
| `YASEO_RATES`, `YASEO_RATE_SEARCH_API`, `YASEO_RATE_WORDSTAT` | тарифы для сметы |

## Как сменить или удалить ключ

- Заново: `yaseo init`. Enter оставляет текущее значение, новое значение заменяет старое.
- Вручную: открыть `~/.config/yaseo/.env` в текстовом редакторе и поправить строку.
- Отозвать ключ Яндекса: удалить его в AI Studio, и старое значение перестанет работать везде.
- Удалить всё: удалить `~/.config/yaseo/`. Накопленные данные лежат отдельно, в `~/.local/share/yaseo/`.

После смены вызовите `whoami` для ключей Яндекса и `geo_providers` для ключей ИИ-провайдеров. Оба покажут, откуда теперь взят каждый ключ.

## Безопасность

- Не присылайте ключи в чат с Claude. `yaseo init` спрашивает ключ со скрытым вводом.
- Не кладите `.env` с ключами в git.
- Целиком yaseo ключи не печатает, в выводе только длина и первые символы.
