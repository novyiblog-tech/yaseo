"""
Провайдеры ИИ-ответов с источниками: цитирует ли ИИ-поиск наш сайт.

Каждый провайдер — один HTTP-вызов и чистая функция разбора ответа.
Разбор отделён от сети, чтобы его можно было сверить на примерах из
официальной справки без ключей и без трат.

Сверено по справке 16.09.2026:

- Яндекс, генеративный ответ — ``POST https://searchapi.api.cloud.yandex.net/v2/gen/search``;
  ``message.content``, ``sources[].url/title/used``, ``searchQueries[]``,
  ``isAnswerRejected``. Роль сервисного аккаунта ``search-api.webSearch.user``.
  Лимит по умолчанию — 1 синхронный запрос в секунду.
  https://aistudio.yandex.ru/ru/docs/search-api/api-ref/GenSearch/search
  https://aistudio.yandex.ru/ru/docs/search-api/concepts/generative-response
  Это ответ YandexGPT поверх результатов Поиска, а не сама «Нейро» или
  Алиса: отдельного API для них Яндекс не публикует.
- Perplexity Agent API — ``POST https://api.perplexity.ai/v1/agent``;
  ``output[]``: элемент ``type="search_results"`` с ``results[].id/url/title``
  и элемент ``type="message"`` с ``content[].text``. Пресет ``fast`` уже
  включает ``web_search``. Sonar Chat Completions поддерживается до
  27.09.2026; его форма ответа (``citations``, ``search_results``) тоже
  разбирается. https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/overview
  https://docs.perplexity.ai/docs/agent-api/tools/web-search
- OpenAI Responses API — ``POST https://api.openai.com/v1/responses`` с
  ``tools=[{"type": "web_search"}]``; цитаты — ``annotations[]`` типа
  ``url_citation``; полный список просмотренного — ``web_search_call.action.sources``
  (по ``include``). https://developers.openai.com/api/docs/guides/tools-web-search
- Gemini generateContent с ``tools=[{"google_search": {}}]``;
  ``groundingMetadata.groundingChunks[].web.uri/title``, ``groundingSupports[]``.
  ``uri`` бывает адресом-перенаправлением ``vertexaisearch.cloud.google.com``,
  ``title`` в этом случае — домен сайта.
  https://ai.google.dev/gemini-api/docs/generate-content/google-search
- Anthropic Messages API с ``{"type": "web_search_20250305", "name": "web_search"}``;
  ``web_search_tool_result.content[]`` (``web_search_result``: url, title) и
  ``citations[]`` типа ``web_search_result_location`` у текстовых блоков.
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .. import net
from . import _core
from .domains import host_of, looks_like_domain, matches, registrable

#: Сколько символов ответа держать в таблице и в базе.
EXCERPT_CHARS = 1200

#: Тариф Яндекса на генеративный ответ: 5 080 ₽ за 1 000 запросов, с НДС.
#: Источник: https://aistudio.yandex.ru/ru/docs/search-api/pricing, снято 16.09.2026.
YANDEX_GEN_RUB_PER_1000 = 5080.0
YANDEX_GEN_RUB_PER_REQUEST = YANDEX_GEN_RUB_PER_1000 / 1000
YANDEX_GEN_PRICE_SOURCE = "aistudio.yandex.ru/ru/docs/search-api/pricing, снято 16.09.2026"

FOREIGN_PRICE_NOTE = "по тарифу вашего аккаунта у провайдера"

USER_AGENT = net.user_agent("geo")


class ProviderError(RuntimeError):
    """Провайдер ответил отказом. Текст — дословно, с подсказкой, если она есть."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Source:
    url: str
    title: str
    domain: str
    #: Использован ли источник в ответе: True/False, None — провайдер не говорит.
    used: bool | None
    #: Место среди источников в порядке, в каком их отдал провайдер (с 1).
    position: int


@dataclass
class Answer:
    provider: str
    query: str
    domain: str
    text: str
    sources: list[Source] = field(default_factory=list)
    #: Наш сайт среди использованных источников (used не False).
    cited: bool = False
    cited_url: str | None = None
    cited_position: int | None = None
    #: Наш сайт есть среди источников вообще, даже неиспользованных.
    in_sources: bool = False
    #: Домен назван в самом тексте ответа.
    mentioned_in_text: bool = False
    rejected: bool = False
    raw: dict | list | None = None
    error: str | None = None
    checked_at: str = ""

    @property
    def used_count(self) -> int:
        return sum(1 for s in self.sources if s.used is not False)

    def rivals(self, n: int = 3) -> list[tuple[str, int]]:
        """Кого цитируют вместо нас: домены использованных источников."""
        c = Counter(s.domain for s in self.sources
                    if s.used is not False and s.domain and s.domain != self.domain
                    and not matches(s.domain, self.domain))
        return c.most_common(n)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["used_count"] = self.used_count
        return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _excerpt(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS].rstrip() + "…"


def finish(provider: str, query: str, domain: str, text: str,
           sources: list[tuple[str, str, bool | None, str | None]],
           raw, mode: str = "site", rejected: bool = False) -> Answer:
    """Собирает Answer из списка (url, title, used, домен-подсказка)."""
    dom = host_of(domain)
    out: list[Source] = []
    seen: dict[str, int] = {}
    for url, title, used, hint in sources:
        # Адреса-перенаправления сами по себе не различают сайты: ключ дубля —
        # адрес вместе с доменом-подсказкой.
        key = (url or "") + "|" + (hint or ("" if url else title or ""))
        if key in seen:  # дубль: used=True сильнее
            s = out[seen[key]]
            if used and not s.used:
                s.used = True
            continue
        d = host_of(hint) if hint else ""
        if not d:
            d = registrable(url) if url else ""
        else:
            d = registrable(d)
        seen[key] = len(out)
        out.append(Source(url=url or "", title=(title or "").strip(), domain=d,
                          used=used, position=len(out) + 1))

    a = Answer(provider=provider, query=query, domain=dom, text=_excerpt(text),
               sources=out, raw=raw, rejected=rejected, checked_at=_now())
    for s in out:
        ours = matches(s.url, dom, mode) if s.url and not _is_redirect(s.url) else False
        if not ours and s.domain:
            ours = matches(s.domain, dom, "site" if mode == "site" else "host")
        if not ours:
            continue
        a.in_sources = True
        if s.used is not False and not a.cited:
            a.cited = True
            a.cited_url = s.url
            a.cited_position = s.position
    if dom and dom in (text or "").lower():
        a.mentioned_in_text = True
    return a


# ─────────────────────────── разбор ответов ───────────────────────────

def parse_yandex(raw, query: str, domain: str, mode: str = "site") -> Answer:
    # В справке пример ответа завёрнут в массив; живой синхронный ответ —
    # один объект. В потоковом режиме последний объект полный.
    obj = raw[-1] if isinstance(raw, list) and raw else raw
    obj = obj if isinstance(obj, dict) else {}
    text = ((obj.get("message") or {}).get("content")) or ""
    srcs = []
    for s in obj.get("sources") or []:
        used = s.get("used")
        srcs.append((s.get("url") or "", s.get("title") or "",
                     bool(used) if used is not None else False, None))
    # Поле used может отсутствовать: «ответ не содержит обязательных полей»,
    # отсутствие булева поля в JSON-представлении protobuf означает false.
    return finish("yandex", query, domain, text, srcs, raw, mode,
                  rejected=bool(obj.get("isAnswerRejected")))


_MARKER = re.compile(r"\[(?:web:)?(\d+)\]")


def parse_perplexity(raw, query: str, domain: str, mode: str = "site") -> Answer:
    obj = raw if isinstance(raw, dict) else {}
    texts: list[str] = []
    results: list[dict] = []
    if "output" in obj:  # Agent API
        for item in obj.get("output") or []:
            t = item.get("type")
            if t == "search_results":
                results.extend(item.get("results") or [])
            elif t == "message":
                for c in item.get("content") or []:
                    if c.get("text"):
                        texts.append(c["text"])
        if not texts and obj.get("output_text"):
            texts.append(obj["output_text"])
    else:  # Sonar Chat Completions
        for ch in obj.get("choices") or []:
            content = (ch.get("message") or {}).get("content")
            if isinstance(content, str):
                texts.append(content)
        results = list(obj.get("search_results") or [])
        known = {r.get("url") for r in results}
        for i, url in enumerate(obj.get("citations") or [], 1):
            if url not in known:
                results.append({"id": i, "url": url, "title": ""})
        for i, r in enumerate(results, 1):
            r.setdefault("id", i)

    text = "\n".join(texts)
    markers = {int(m) for m in _MARKER.findall(text)}
    srcs = []
    for r in results:
        rid = r.get("id")
        # Справка: «treat the id and url fields of each search_results entry as
        # the source of truth for citations». Если в тексте есть маркеры [n],
        # used = упомянут ли id; маркеров нет — провайдер не различает.
        used = (rid in markers) if markers else None
        srcs.append((r.get("url") or "", r.get("title") or "", used, None))
    return finish("perplexity", query, domain, text, srcs, raw, mode)


def parse_openai(raw, query: str, domain: str, mode: str = "site") -> Answer:
    obj = raw if isinstance(raw, dict) else {}
    items = obj.get("output") if "output" in obj else (raw if isinstance(raw, list) else [])
    texts: list[str] = []
    cited: list[tuple[str, str]] = []
    consulted: list[tuple[str, str]] = []
    for item in items or []:
        t = item.get("type")
        if t == "message":
            for c in item.get("content") or []:
                if c.get("text"):
                    texts.append(c["text"])
                for an in c.get("annotations") or []:
                    if an.get("type") != "url_citation":
                        continue
                    # Responses API кладёт url прямо в аннотацию; Chat Completions —
                    # во вложенный объект url_citation.
                    body = an.get("url_citation") if isinstance(an.get("url_citation"), dict) else an
                    cited.append((body.get("url") or "", body.get("title") or ""))
        elif t == "web_search_call":
            for s in ((item.get("action") or {}).get("sources") or []):
                if isinstance(s, dict) and s.get("url"):
                    consulted.append((s["url"], s.get("title") or ""))
    srcs = [(u, ti, True, None) for u, ti in cited]
    cited_urls = {u for u, _ in cited}
    srcs += [(u, ti, False, None) for u, ti in consulted if u not in cited_urls]
    return finish("openai", query, domain, "\n".join(texts), srcs, raw, mode)


REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com",)


def _is_redirect(url: str) -> bool:
    h = host_of(url)
    return any(h == r or h.endswith("." + r) for r in REDIRECT_HOSTS)


def parse_gemini(raw, query: str, domain: str, mode: str = "site",
                 resolve: Callable[[str], str | None] | None = None) -> Answer:
    obj = raw if isinstance(raw, dict) else {}
    cands = obj.get("candidates") or []
    cand = cands[0] if cands else {}
    texts = [p.get("text") for p in ((cand.get("content") or {}).get("parts") or [])
             if p.get("text")]
    gm = cand.get("groundingMetadata") or {}
    chunks = gm.get("groundingChunks") or []
    supports = gm.get("groundingSupports")
    used_idx: set[int] = set()
    for sup in supports or []:
        used_idx.update(i for i in sup.get("groundingChunkIndices") or [] if isinstance(i, int))
    srcs = []
    for i, ch in enumerate(chunks):
        web = ch.get("web") or {}
        uri = web.get("uri") or ""
        title = web.get("title") or ""
        hint = web.get("domain") or None
        if _is_redirect(uri):
            real = resolve(uri) if resolve else None
            if real:
                uri = real
            elif not hint and looks_like_domain(title):
                # Справка: для адресов-перенаправлений title — это домен сайта.
                hint = title
        used = (i in used_idx) if supports is not None else None
        srcs.append((uri, title, used, hint))
    return finish("gemini", query, domain, "\n".join(texts), srcs, raw, mode)


def parse_anthropic(raw, query: str, domain: str, mode: str = "site") -> Answer:
    obj = raw if isinstance(raw, dict) else {}
    texts: list[str] = []
    results: list[tuple[str, str]] = []
    cited: list[tuple[str, str]] = []
    errors: list[str] = []
    for block in obj.get("content") or []:
        t = block.get("type")
        if t == "text":
            texts.append(block.get("text") or "")
            for c in block.get("citations") or []:
                if c.get("type") == "web_search_result_location":
                    cited.append((c.get("url") or "", c.get("title") or ""))
        elif t == "web_search_tool_result":
            content = block.get("content")
            if isinstance(content, dict):
                errors.append(content.get("error_code") or "ошибка поиска")
                continue
            for r in content or []:
                if r.get("type") == "web_search_result":
                    results.append((r.get("url") or "", r.get("title") or ""))
    cited_urls = {u for u, _ in cited}
    srcs = [(u, ti, u in cited_urls, None) for u, ti in results]
    known = {u for u, _ in results}
    srcs += [(u, ti, True, None) for u, ti in cited if u not in known]
    a = finish("anthropic", query, domain, "".join(texts), srcs, raw, mode)
    if errors and not a.sources:
        a.error = "поиск Anthropic вернул ошибку: " + ", ".join(errors)
    return a


# ─────────────────────────────── сеть ───────────────────────────────

#: Транспорт общий, из `yaseo.net`: запрос несёт ключ, поэтому только https
#: и без перехода по перенаправлениям — иначе `Authorization`, `x-api-key`
#: и `x-goog-api-key` ушли бы по адресу из Location.
#: Яндекс — напрямую (если не YASEO_USE_PROXY=1), зарубежные — через прокси
#: из окружения (HTTPS_PROXY).
def _opener(direct: bool) -> urllib.request.OpenerDirector:
    return net.keyed_opener(proxy=not direct)


def _post_json(url: str, body: dict, headers: dict, *, direct: bool, timeout: int,
               hint_403: str = "") -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with _opener(direct).open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")[:2000]
        msg = f"HTTP {e.code}: {body_text.strip() or e.reason}"
        if e.code in (401, 403) and hint_403:
            msg += f"\n{hint_403}"
        raise ProviderError(msg, status=e.code, body=body_text) from None
    except net.RedirectRefused as e:
        raise ProviderError(str(e.reason), status=e.code) from None
    except urllib.error.URLError as e:
        raise ProviderError(f"сеть: {e.reason}") from None
    except (TimeoutError, OSError) as e:
        raise ProviderError(f"сеть: {e}") from None
    except json.JSONDecodeError as e:
        raise ProviderError(f"ответ не JSON: {e}") from None


def resolve_redirect(url: str, timeout: int = 10) -> str | None:
    """
    Куда ведёт адрес-перенаправление grounding. Тело не читается.

    Адрес приходит из ответа модели, поэтому идёт через общий opener сайтов:
    только http/https, внутренние адреса не открываются, за перенаправлением
    не идём — нужен сам Location. Ключей в запросе нет.
    """
    try:
        net.check_url(url, allow_private=False)
    except net.UnsafeURL:
        return None
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with net.site_opener(proxy=True, follow=False, allow_private=False).open(
                req, timeout=timeout) as r:
            return r.headers.get("Location")
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return e.headers.get("Location")
        return None
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ───────────────────────────── провайдеры ─────────────────────────────

class Provider:
    name: str = ""
    title: str = ""
    required_env: tuple[str, ...] = ()
    price_note: str = FOREIGN_PRICE_NOTE
    #: Сколько рублей стоит один вызов, если это известно из тарифа; иначе None.
    rub_per_request: float | None = None
    #: Минимальная пауза между вызовами, секунд.
    min_interval: float = 0.0
    where_to_get: str = ""
    honest_note: str = ""

    _lock = threading.Lock()
    _last_call = 0.0

    def configured(self) -> tuple[bool, list[str]]:
        return _core.configured(self.required_env)

    def _pace(self) -> None:
        if not self.min_interval:
            return
        with self._lock:
            wait = type(self)._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            type(self)._last_call = time.monotonic()

    def ask(self, query: str, domain: str, *, mode: str = "site", **opts) -> Answer:
        env = _core.load_env(self.required_env)
        self._pace()
        raw = self.call(query, env, **opts)
        return self.parse(raw, query, domain, mode)

    def call(self, query: str, env: dict, **opts):  # pragma: no cover — сеть
        raise NotImplementedError

    def parse(self, raw, query: str, domain: str, mode: str = "site") -> Answer:
        raise NotImplementedError


class YandexGen(Provider):
    name = "yandex"
    title = "Яндекс — генеративный ответ (YandexGPT по результатам Поиска)"
    required_env = ("YC_FOLDER_ID", "YANDEX_AI_STUDIO_API_KEY")
    rub_per_request = YANDEX_GEN_RUB_PER_REQUEST
    price_note = (f"{YANDEX_GEN_RUB_PER_REQUEST:.2f} ₽ за запрос".replace(".", ",")
                  + f" ({YANDEX_GEN_PRICE_SOURCE})")
    min_interval = 1.05
    endpoint = "https://searchapi.api.cloud.yandex.net/v2/gen/search"
    role = "search-api.webSearch.user"
    where_to_get = ("Yandex AI Studio: каталог (YC_FOLDER_ID) и API-ключ сервисного "
                    "аккаунта (YANDEX_AI_STUDIO_API_KEY) — "
                    "https://aistudio.yandex.ru/ru/docs/ai-studio/operations/get-api-key. "
                    "Сервисному аккаунту нужна роль search-api.webSearch.user.")
    honest_note = ("Это ответ YandexGPT поверх результатов Поиска через Search API, "
                   "а не сама «Нейро» или Алиса: отдельного API для них Яндекс не публикует.")

    def hint_403(self) -> str:
        return (f"Подсказка: у сервисного аккаунта, чей API-ключ указан, должна быть роль "
                f"{self.role} на каталог YC_FOLDER_ID. Назначьте её в консоли Облака "
                f"(каталог → Права доступа) и повторите.")

    def call(self, query: str, env: dict, site: list[str] | None = None, **_):
        body: dict = {
            "messages": [{"content": query, "role": "ROLE_USER"}],
            "folderId": env["YC_FOLDER_ID"],
            "searchType": "SEARCH_TYPE_RU",
        }
        if site:
            body["site"] = {"site": list(site)[:5]}
        return _post_json(self.endpoint, body,
                          {"Authorization": f"Api-Key {env['YANDEX_AI_STUDIO_API_KEY']}"},
                          direct=not _core.use_proxy(), timeout=90,
                          hint_403=self.hint_403())

    def parse(self, raw, query, domain, mode="site"):
        return parse_yandex(raw, query, domain, mode)


class Perplexity(Provider):
    name = "perplexity"
    title = "Perplexity — Agent API, пресет fast с web_search"
    required_env = ("PERPLEXITY_API_KEY",)
    endpoint = "https://api.perplexity.ai/v1/agent"
    preset = "fast"
    where_to_get = ("Ключ API Perplexity — в кабинете API Platform, справка: "
                    "https://docs.perplexity.ai/docs/getting-started/overview")
    honest_note = ("Sonar Chat Completions поддерживается до 27.09.2026, дальше — Agent API; "
                   "здесь используется Agent API.")

    def call(self, query, env, preset: str | None = None, **_):
        return _post_json(self.endpoint, {"preset": preset or self.preset, "input": query},
                          {"Authorization": f"Bearer {env['PERPLEXITY_API_KEY']}"},
                          direct=False, timeout=120)

    def parse(self, raw, query, domain, mode="site"):
        return parse_perplexity(raw, query, domain, mode)


class OpenAIWeb(Provider):
    name = "openai"
    title = "OpenAI — Responses API с инструментом web_search"
    required_env = ("OPENAI_API_KEY",)
    endpoint = "https://api.openai.com/v1/responses"
    #: Справка, таблица «Choose an integration»: новая интеграция — gpt-5.5.
    model = "gpt-5.5"
    where_to_get = ("Ключ OpenAI API — в кабинете platform.openai.com (API keys). "
                    "Справка по инструменту: https://developers.openai.com/api/docs/guides/tools-web-search")
    honest_note = ("Это поиск модели через API, а не интерфейс ChatGPT: результаты близки, "
                   "но не обязаны совпадать.")

    def call(self, query, env, model: str | None = None, **_):
        body = {"model": model or self.model, "tools": [{"type": "web_search"}],
                "tool_choice": "auto", "include": ["web_search_call.action.sources"],
                "input": query}
        return _post_json(self.endpoint, body,
                          {"Authorization": f"Bearer {env['OPENAI_API_KEY']}"},
                          direct=False, timeout=180)

    def parse(self, raw, query, domain, mode="site"):
        return parse_openai(raw, query, domain, mode)


class GeminiGrounding(Provider):
    name = "gemini"
    title = "Google Gemini — generateContent с google_search (grounding)"
    required_env = ("GEMINI_API_KEY",)
    model = "gemini-3.8-flash"
    where_to_get = ("Ключ Gemini API — в Google AI Studio (aistudio.google.com, Get API key). "
                    "Справка: https://ai.google.dev/gemini-api/docs/generate-content/google-search")
    honest_note = ("Это Gemini API с поиском Google, а не AI Overviews в выдаче Google. "
                   "Адреса источников приходят перенаправлениями vertexaisearch — "
                   "yaseo раскрывает их запросом HEAD, а если не вышло, берёт домен из title.")
    resolve_redirects = True

    def call(self, query, env, model: str | None = None, **_):
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model or self.model}:generateContent")
        body = {"contents": [{"parts": [{"text": query}]}],
                "tools": [{"google_search": {}}]}
        return _post_json(url, body, {"x-goog-api-key": env["GEMINI_API_KEY"]},
                          direct=False, timeout=120)

    def parse(self, raw, query, domain, mode="site"):
        return parse_gemini(raw, query, domain, mode,
                            resolve=resolve_redirect if self.resolve_redirects else None)


class AnthropicWeb(Provider):
    name = "anthropic"
    title = "Anthropic Claude — Messages API с web_search"
    required_env = ("ANTHROPIC_API_KEY",)
    endpoint = "https://api.anthropic.com/v1/messages"
    model = "claude-opus-5"
    tool_type = "web_search_20250305"
    where_to_get = ("Ключ Claude API — в консоли platform.claude.com. Справка по инструменту: "
                    "https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool")
    honest_note = ("Базовая версия инструмента web_search_20250305: все результаты поиска "
                   "видны в ответе. Версии 2026 года с динамической фильтрацией могут "
                   "скрывать часть результатов.")

    def call(self, query, env, model: str | None = None, **_):
        body = {"model": model or self.model, "max_tokens": 2048,
                "messages": [{"role": "user", "content": query}],
                "tools": [{"type": self.tool_type, "name": "web_search", "max_uses": 5}]}
        return _post_json(self.endpoint, body,
                          {"x-api-key": env["ANTHROPIC_API_KEY"],
                           "anthropic-version": "2023-06-01"},
                          direct=False, timeout=180)

    def parse(self, raw, query, domain, mode="site"):
        return parse_anthropic(raw, query, domain, mode)


REGISTRY: dict[str, Provider] = {p.name: p for p in (
    YandexGen(), Perplexity(), OpenAIWeb(), GeminiGrounding(), AnthropicWeb())}

PARSERS = {
    "yandex": parse_yandex,
    "perplexity": parse_perplexity,
    "openai": parse_openai,
    "gemini": parse_gemini,
    "anthropic": parse_anthropic,
}


def get(name: str) -> Provider:
    p = REGISTRY.get((name or "").strip().lower())
    if p is None:
        raise ValueError(f"неизвестный провайдер «{name}». Есть: {', '.join(REGISTRY)}")
    return p


def configured_names() -> list[str]:
    return [n for n, p in REGISTRY.items() if p.configured()[0]]


def ask(name: str, query: str, domain: str, *, mode: str = "site", **opts) -> Answer:
    """Спросить провайдера. Отказ сети или API не бросается, а кладётся в Answer.error."""
    p = get(name)
    try:
        return p.ask(query, domain, mode=mode, **opts)
    except _core.КлючейНет as e:
        return Answer(provider=name, query=query, domain=host_of(domain), text="",
                      error=str(e), checked_at=_now())
    except ProviderError as e:
        return Answer(provider=name, query=query, domain=host_of(domain), text="",
                      error=str(e), raw={"status": e.status, "body": e.body},
                      checked_at=_now())
