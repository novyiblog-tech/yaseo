#!/usr/bin/env python3
"""
Технический аудит сайта — собственный краулер yaseo.

Ошибка либо доказана HTTP-ответом и процитированным тегом, либо её нет.

Ноль внешних зависимостей: HTML разбирается через html.parser, XML карты сайта —
через xml.etree.ElementTree, запреты — через urllib.robotparser.

Вежливость к чужому серверу: свой User-Agent, задержка между запросами,
таймаут, потолок страниц, уважение robots.txt.

Ходим НАПРЯМУЮ, мимо системного прокси. Если в окружении выставлены
http_proxy / https_proxy, urllib подхватил бы их молча: сайт мог бы отдать
контент для другой страны, а сервер увидел бы чужой адрес.
Через прокси — только явно: `use_proxy=True` или `--proxy`.

Ходим только по http и https — на входе и на каждом шаге перенаправления.
Внутренние адреса (localhost, 127/8, 10/8, 172.16/12, 192.168/16, 169.254/16,
::1, fc00::/7) — только с явного разрешения: `allow_private=True`,
`--allow-private` или YASEO_ALLOW_PRIVATE=1. Карты сайта читаются только
со своего регистрируемого домена, сжатые — с потолком распаковки.

Публичный API:
- audit_site(root_url, max_pages, delay, use_sitemap) -> SiteAudit
- audit_page(url, ...)                                -> PageAudit
- find_issues(pages, root)                            -> list[Issue]

Запуск как скрипт:
    yaseo audit --url https://example.ru --max-pages 30
    yaseo audit --url https://example.ru --json out.json
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
import zlib
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import net
from .geo.domains import registrable

USER_AGENT = net.user_agent("audit", "(+https://github.com/novyiblog-tech/yaseo)")

#: Токен для robotparser: он сверяет User-agent по первому слову без версии.
ROBOTS_TOKEN = "yaseo-audit"

#: Пороги проверок. Меняются осознанно — это проектное решение, не измерение.
TITLE_MIN = 30
THIN_WORDS = 300

#: Длина заголовка и границы описания. Если у сайта есть свой публикатор со
#: своими порогами, их надо передать сюда (`set_limits`): проверяющий, который
#: разрешает то, что публикующий запрещает, молчит о расхождении, потому что
#: каждый смотрит на своё число. Проверки читают значения в момент вызова.
TITLE_MAX = 60
DESC_MIN = 120
DESC_MAX = 170


def set_limits(title_max: int | None = None, desc_min: int | None = None,
               desc_max: int | None = None) -> tuple[int, int, int]:
    """Задать пороги заголовка и описания. Возвращает действующие значения."""
    global TITLE_MAX, DESC_MIN, DESC_MAX
    if title_max is not None:
        TITLE_MAX = int(title_max)
    if desc_min is not None:
        DESC_MIN = int(desc_min)
    if desc_max is not None:
        DESC_MAX = int(desc_max)
    if DESC_MIN > DESC_MAX:
        raise ValueError(f"нижняя граница описания {DESC_MIN} больше верхней {DESC_MAX}")
    return TITLE_MAX, DESC_MIN, DESC_MAX


#: Больше одного хопа — уже цепочка, вес теряется на каждом переходе.
MAX_REDIRECT_HOPS = 1

#: Сколько редиректов вообще готовы пройти, прежде чем признать петлю.
REDIRECT_LIMIT = 5

#: Потолок тела ответа — 3 МБ. Больше в HTML-странице нам не нужно.
BODY_LIMIT = 3 * 1024 * 1024

#: Потолок распаковки sitemap.xml.gz. 50 МБ — предел несжатого файла карты
#: по протоколу sitemaps.org. Без потолка 3 МБ сжатых нулей дают гигабайты.
SITEMAP_UNPACKED_LIMIT = 50 * 1024 * 1024

#: Содержимое этих тегов не текст страницы и в word_count не идёт.
BLOCK_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})

#: Не-HTML по расширению — в обход не берём.
SKIP_EXT = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".txt",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".ico", ".bmp",
    ".mp3", ".mp4", ".avi", ".mov", ".webm", ".wav", ".m4a",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".dmg", ".exe", ".apk",
    ".css", ".js", ".json", ".xml", ".rss", ".atom", ".woff", ".woff2", ".ttf", ".eot",
})

#: Схемы, по которым ходить нечего.
SKIP_SCHEMES = frozenset({"mailto", "tel", "javascript", "data", "sms", "ftp", "whatsapp"})

HTML_TYPES = ("text/html", "application/xhtml+xml", "application/xml", "text/xml")

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}


# ─────────────────────────────── модели ───────────────────────────────


@dataclass
class PageAudit:
    """Одна страница: что вернул сервер и что стоит в её разметке."""

    url: str
    status: int | None
    redirect_to: str | None
    title: str | None
    title_len: int
    description: str | None
    description_len: int
    h1: list[str]
    canonical: str | None
    robots_meta: str | None
    #: og:title, og:description, og:image и всё прочее og:*
    og: dict[str, str]
    #: типы из JSON-LD @type и из microdata itemtype
    schema_types: list[str]
    word_count: int
    images_total: int
    images_no_alt: int
    internal_links: list[str]
    external_links: list[str]
    error: str | None
    #: полный путь редиректов — чтобы длину цепочки можно было процитировать
    redirect_chain: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Issue:
    """Найденная проблема. Без доказательства проблема не создаётся."""

    severity: str      # critical | major | minor
    code: str          # kebab-case, напр. "missing-title"
    url: str
    evidence: str      # процитированный тег или число, не пересказ
    fix: str           # что конкретно сделать


@dataclass
class SiteAudit:
    root: str
    crawled_at: str
    pages_crawled: int
    robots_txt: bool
    sitemap_urls: int
    pages: list[PageAudit] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    error: str | None = None
    #: адреса, закрытые в robots.txt (Disallow) — обход их не открывал.
    #: Поле с default в конце списка — код, читающий SiteAudit позиционно,
    #: не ломается.
    robots_disallowed: list[str] = field(default_factory=list)
    #: карты сайта с чужих доменов, объявленные в robots.txt или в индексе
    #: карт, — не читались.
    sitemaps_skipped: list[str] = field(default_factory=list)
    #: карты, чтение которых остановлено (например, распаковка выше потолка).
    sitemap_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ──────────────────────────── работа с URL ────────────────────────────


def normalize_url(url: str) -> str:
    """
    Приводит адрес к запрашиваемому виду: убирает фрагмент, опускает хост в нижний
    регистр, снимает порт по умолчанию. Хвостовой слеш сохраняется — иначе на сайте
    со слешем каждый запрос платит лишним редиректом.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        # IPv6-литерал: `hostname` отдаёт его без скобок, а без скобок
        # `http://::1:8080/` уже не адрес — порт не отделить от адреса.
        host = f"[{host}]"
    port = parts.port
    if port and not (scheme == "http" and port == 80) and not (scheme == "https" and port == 443):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))


# Метки кампаний. Для поиска это одна и та же страница: разметка на ней та же,
# canonical указывает на адрес без параметров.
_TRACKING_PARAMS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "yclid", "ysclid", "fbclid", "_openstat", "from",
)


def dedup_key(url: str) -> str:
    """Ключ дедупликации: то же, что normalize_url, но без хвостового слеша.

    `/uslugi` и `/uslugi/` — одна страница, и в обход она должна попасть один раз.

    Метки кампаний из ключа выбрасываются. Иначе `/kontakty/` и
    `/kontakty?utm_source=blog&…` считались бы двумя страницами с одинаковыми
    title и description и давали «duplicate-title» и «duplicate-description» —
    при том что это один адрес, а canonical на UTM-версии ведёт на чистый. Правило дубля должно ловить разные страницы с
    одинаковой разметкой, а не один адрес, размеченный для аналитики.
    """
    norm = normalize_url(url)
    try:
        # `/о-компании/` и `/%D0%BE-…/` — один адрес: сравниваем в том виде,
        # в каком его отправит браузер.
        norm = net.safe_url(norm)
    except ValueError:
        pass
    parts = urlsplit(norm)
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query = "&".join(
        pair for pair in parts.query.split("&")
        if pair and pair.split("=", 1)[0].lower() not in _TRACKING_PARAMS
    )
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _same_site(url: str, root: str) -> bool:
    h, r = _host(url), _host(root)
    return bool(h) and (h == r or h.endswith("." + r) or r.endswith("." + h))


def _short(url: str) -> str:
    """Короткая запись URL для таблиц: путь с запросом."""
    p = urlsplit(url)
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def _crawlable(url: str) -> bool:
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme and (scheme in SKIP_SCHEMES or scheme not in ("http", "https")):
        return False
    path = (parts.path or "").lower()
    dot = path.rfind(".")
    if dot > -1 and path[dot:] in SKIP_EXT:
        return False
    return True


# ─────────────────────────────── сеть ─────────────────────────────────


#: Транспорт — общий, из `yaseo.net`: http/https без file/ftp/data, перенаправления
#: не выполняются (цепочку собираем сами, чтобы её посчитать), внутренние адреса —
#: по разрешению.
#:
#: По умолчанию — в обход прокси: сайты аудируем под Яндекс, с российского адреса.
#: Через зарубежный выход сайт может отдать другой контент, а сервер увидит чужую
#: страну — аудит перестанет отражать реальность.
def _opener(use_proxy: bool, allow_private: bool) -> urllib.request.OpenerDirector:
    return net.site_opener(proxy=use_proxy, follow=False, allow_private=allow_private)


@dataclass
class _Fetched:
    final_url: str
    status: int | None
    chain: list[str]
    content_type: str
    headers: dict[str, str]
    body: bytes
    error: str | None


def _request(
    url: str, timeout: float, use_proxy: bool = False, allow_private: bool = False,
) -> tuple[int | None, dict[str, str], bytes, str | None]:
    """Один HTTP-запрос без следования редиректам."""
    try:
        target = net.safe_url(url)
    except ValueError as e:
        return None, {}, b"", f"адрес не разобран: {e}"
    req = urllib.request.Request(
        target,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.8",
        },
    )
    try:
        with _opener(use_proxy, allow_private).open(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read(BODY_LIMIT), None
    except urllib.error.HTTPError as e:
        # 3xx приходит сюда же: автоматический редирект-хендлер выключен.
        try:
            body = e.read(BODY_LIMIT)
        except Exception:  # noqa: BLE001 — тело ошибки не критично
            body = b""
        finally:
            e.close()
        return e.code, dict(e.headers.items()) if e.headers else {}, body, None
    except urllib.error.URLError as e:
        return None, {}, b"", f"URLError: {e.reason}"
    except (socket.timeout, TimeoutError):
        return None, {}, b"", f"таймаут {timeout} с"
    except http.client.HTTPException as e:
        # Сервер ответил не по протоколу (оборванный ответ, кривые заголовки).
        return None, {}, b"", f"ответ не по протоколу HTTP: {type(e).__name__}: {e}"
    except OSError as e:
        return None, {}, b"", f"сетевая ошибка: {type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001 — причина уезжает в поле error
        return None, {}, b"", f"{type(e).__name__}: {e}"


def _fetch(url: str, timeout: float = 15.0, use_proxy: bool = False,
           allow_private: bool | None = None) -> _Fetched:
    """Запрос с ручным проходом по редиректам. Цепочка сохраняется целиком.

    Каждый адрес цепочки проверяется до запроса: только http/https, внутренние
    адреса — только при `allow_private` (по умолчанию — YASEO_ALLOW_PRIVATE).
    """
    chain: list[str] = []
    current = url
    allow = net.private_allowed(allow_private)

    for _ in range(REDIRECT_LIMIT + 1):
        try:
            net.check_url(net.safe_url(current), allow)
        except net.UnsafeURL as e:
            return _Fetched(current, None, chain, "", {}, b"", str(e.reason))
        except ValueError as e:
            return _Fetched(current, None, chain, "", {}, b"", f"адрес не разобран: {e}")
        status, headers, body, err = _request(current, timeout, use_proxy, allow)
        ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()

        if status in (301, 302, 303, 307, 308):
            location = headers.get("Location") or headers.get("location")
            if not location:
                return _Fetched(current, status, chain, ctype, headers, body,
                                f"HTTP {status} без заголовка Location")
            current = urljoin(current, location)
            chain.append(current)
            continue

        return _Fetched(current, status, chain, ctype, headers, body, err)

    return _Fetched(current, None, chain, "", {}, b"",
                    f"больше {REDIRECT_LIMIT} редиректов подряд, похоже на петлю")


def _decode(body: bytes, content_type: str) -> str:
    """Кодировка: сначала заголовок, потом meta charset, иначе utf-8 с заменой."""
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if m:
        enc = m.group(1)
    else:
        m2 = re.search(rb'charset=["\']?([\w\-]+)', body[:4096], re.I)
        enc = m2.group(1).decode("ascii", "ignore") if m2 else "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


# ───────────────────────────── разбор HTML ────────────────────────────


class _PageParser(HTMLParser):
    """Читает страницу за один проход: мета, заголовки, ссылки, картинки, разметку."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title: str | None = None
        self.description: str | None = None
        self.robots_meta: str | None = None
        self.canonical: str | None = None
        self.og: dict[str, str] = {}
        self.h1: list[str] = []
        self.hrefs: list[str] = []
        self.images_total = 0
        self.images_no_alt = 0
        self.schema_types: list[str] = []

        self._title_buf: list[str] = []
        self._h1_buf: list[str] | None = None
        self._text: list[str] = []
        self._skip = 0
        self._in_title = False
        self._in_ldjson = False
        self._ld_buf: list[str] = []

    # — служебное —

    @property
    def text(self) -> str:
        return " ".join(self._text)

    def _add_schema(self, value: str) -> None:
        name = (value or "").strip().rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        if name and name not in self.schema_types:
            self.schema_types.append(name)

    def _walk_ldjson(self, node) -> None:
        """Достаёт @type из JSON-LD любой вложенности, включая @graph."""
        if isinstance(node, dict):
            t = node.get("@type")
            if isinstance(t, str):
                self._add_schema(t)
            elif isinstance(t, list):
                for x in t:
                    if isinstance(x, str):
                        self._add_schema(x)
            for v in node.values():
                self._walk_ldjson(v)
        elif isinstance(node, list):
            for v in node:
                self._walk_ldjson(v)

    # — разбор —

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {(k or "").lower(): (v or "") for k, v in attrs}

        if "itemtype" in a:
            self._add_schema(a["itemtype"])

        if tag in BLOCK_TAGS:
            if tag == "script" and "ld+json" in a.get("type", "").lower():
                self._in_ldjson = True
                self._ld_buf = []
            self._skip += 1
            return

        if tag == "base" and a.get("href"):
            self.base_url = urljoin(self.base_url, a["href"])
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._h1_buf = []
        elif tag == "meta":
            self._meta(a)
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if "canonical" in rel and a.get("href"):
                self.canonical = urljoin(self.base_url, a["href"].strip())
        elif tag == "a":
            href = (a.get("href") or "").strip()
            if href and not href.startswith("#"):
                self.hrefs.append(urljoin(self.base_url, href))
        elif tag == "img":
            self.images_total += 1
            # Дефект — ОТСУТСТВИЕ атрибута, а не пустое значение. `alt=""` это
            # осознанная пометка «картинка декоративная, читать вслух нечего» —
            # так требует WCAG, и так стоит у пикселя Яндекс.Метрики. Если
            # считать пустое значение дефектом, «images-no-alt» ложится почти
            # на весь сайт, и всюду это один и тот же служебный пиксель.
            if "alt" not in a:
                self.images_no_alt += 1

    def _meta(self, a: dict[str, str]) -> None:
        key = (a.get("property") or a.get("name") or "").strip().lower()
        content = (a.get("content") or "").strip()
        if not key or not content:
            return
        if key == "description":
            if self.description is None:
                self.description = content
        elif key in ("robots", "yandex", "googlebot"):
            self.robots_meta = content if self.robots_meta is None else f"{self.robots_meta}; {content}"
        elif key.startswith("og:"):
            self.og.setdefault(key, content)

    def handle_endtag(self, tag: str) -> None:
        if tag in BLOCK_TAGS:
            if tag == "script" and self._in_ldjson:
                self._in_ldjson = False
                raw = "".join(self._ld_buf).strip()
                if raw:
                    try:
                        self._walk_ldjson(json.loads(raw))
                    except (ValueError, TypeError):
                        pass  # битый JSON-LD разметкой не считаем
            self._skip = max(0, self._skip - 1)
            return

        if tag == "title" and self._in_title:
            self._in_title = False
            if self.title is None:
                joined = " ".join("".join(self._title_buf).split())
                self.title = joined or None
        elif tag == "h1" and self._h1_buf is not None:
            # Текстовые узлы разных элементов склеиваются через пробел: заголовок
            # вида <span>Маркетинг</span><span>застройщика</span> человек видит
            # двумя словами, и цитата «Маркетингзастройщика» была бы неверной.
            joined = " ".join(" ".join(self._h1_buf).split())
            self.h1.append(joined)
            self._h1_buf = None

    def handle_data(self, data: str) -> None:
        if self._in_ldjson:
            self._ld_buf.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self._title_buf.append(data)
            return
        if self._h1_buf is not None:
            self._h1_buf.append(data)
        self._text.append(data)


_WORD = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)


def _count_words(text: str) -> int:
    return len(_WORD.findall(text))


# ─────────────────────────── аудит страницы ───────────────────────────


def _empty_page(url: str, status: int | None, chain: list[str], error: str | None) -> PageAudit:
    """Страница, которую не удалось разобрать: остаётся только код ответа."""
    return PageAudit(
        url=url, status=status,
        redirect_to=chain[-1] if chain else None,
        title=None, title_len=0, description=None, description_len=0,
        h1=[], canonical=None, robots_meta=None, og={}, schema_types=[],
        word_count=0, images_total=0, images_no_alt=0,
        internal_links=[], external_links=[], error=error, redirect_chain=list(chain),
    )


def audit_page(
    url: str, root: str | None = None, timeout: float = 15.0, use_proxy: bool = False,
    allow_private: bool | None = None,
) -> PageAudit:
    """
    Забирает одну страницу и разбирает её разметку.

    Ошибки не бросаются: причина уезжает в поле error, объект возвращается всегда.
    `root` нужен, чтобы отличить внутренние ссылки от внешних; без него берётся
    хост самой страницы.
    """
    url = normalize_url(url)
    root = root or url

    try:
        got = _fetch(url, timeout=timeout, use_proxy=use_proxy, allow_private=allow_private)
    except Exception as e:  # noqa: BLE001 — наружу из публичной функции не бросаем
        return _empty_page(url, None, [], f"{type(e).__name__}: {e}")

    if got.error:
        return _empty_page(url, got.status, got.chain, got.error)

    if got.status is None or got.status >= 400:
        return _empty_page(url, got.status, got.chain, None)

    if got.content_type and not any(t in got.content_type for t in HTML_TYPES):
        ctype = got.content_type.split(";")[0].strip()
        return _empty_page(url, got.status, got.chain, f"Content-Type: {ctype}, не HTML")

    html = _decode(got.body, got.content_type)
    parser = _PageParser(got.final_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception as e:  # noqa: BLE001 — кривой HTML не повод падать
        return _empty_page(url, got.status, got.chain,
                           f"разбор HTML: {type(e).__name__}: {e}")

    internal: list[str] = []
    external: list[str] = []
    seen_int: set[str] = set()
    seen_ext: set[str] = set()
    for href in parser.hrefs:
        if not _crawlable(href):
            continue
        norm = normalize_url(href)
        if _same_site(norm, root):
            if norm not in seen_int:
                seen_int.add(norm)
                internal.append(norm)
        elif norm not in seen_ext:
            seen_ext.add(norm)
            external.append(norm)

    robots_meta = parser.robots_meta
    header_robots = got.headers.get("X-Robots-Tag") or got.headers.get("x-robots-tag")
    if header_robots:
        tail = f"X-Robots-Tag: {header_robots}"
        robots_meta = tail if not robots_meta else f"{robots_meta}; {tail}"

    title = parser.title
    description = parser.description

    return PageAudit(
        url=url,
        status=got.status,
        redirect_to=got.chain[-1] if got.chain else None,
        title=title,
        title_len=len(title or ""),
        description=description,
        description_len=len(description or ""),
        h1=parser.h1,
        canonical=parser.canonical,
        robots_meta=robots_meta,
        og=parser.og,
        schema_types=parser.schema_types,
        word_count=_count_words(parser.text),
        images_total=parser.images_total,
        images_no_alt=parser.images_no_alt,
        internal_links=internal,
        external_links=external,
        error=None,
        redirect_chain=list(got.chain),
    )


# ──────────────────────── robots.txt и sitemap ────────────────────────


def _read_robots(
    root: str, timeout: float, use_proxy: bool = False, allow_private: bool | None = None,
) -> tuple[bool, urllib.robotparser.RobotFileParser, list[str]]:
    """Возвращает: есть ли robots.txt, разборщик запретов, объявленные карты сайта."""
    rp = urllib.robotparser.RobotFileParser()
    got = _fetch(urljoin(root, "/robots.txt"), timeout=timeout, use_proxy=use_proxy,
                 allow_private=allow_private)

    if got.error or got.status != 200 or not got.body:
        rp.parse([])                      # без robots.txt разрешено всё
        return False, rp, []

    text = _decode(got.body, got.content_type)
    rp.parse(text.splitlines())
    maps = [
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.strip().lower().startswith("sitemap:")
    ]
    return True, rp, [m for m in maps if m]


def _xml_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


class SitemapTooLarge(ValueError):
    """Карта после распаковки больше SITEMAP_UNPACKED_LIMIT."""


def _gunzip_limited(data: bytes, limit: int | None = None) -> bytes:
    """
    Распаковывает gzip потоком и останавливается на пределе.

    `gzip.decompress` отдал бы всё разом: 3 МБ сжатых нулей — это гигабайты
    в памяти. Здесь выход читается кусками, и первый байт сверх предела
    поднимает SitemapTooLarge. Битые данные — zlib.error, как и раньше.
    """
    limit = SITEMAP_UNPACKED_LIMIT if limit is None else limit
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    buf = data
    while not d.eof:
        chunk = d.decompress(buf, 64 * 1024)
        buf = d.unconsumed_tail
        out += chunk
        if len(out) > limit:
            raise SitemapTooLarge(
                f"после распаковки больше {limit // (1024 * 1024) or limit} "
                f"{'МБ' if limit >= 1024 * 1024 else 'байт'} — чтение остановлено"
            )
        if not chunk and not buf:
            break
    return bytes(out)


def _own_domain(url: str, root: str) -> bool:
    """Тот же регистрируемый домен: карта с blog.example.ru годится для example.ru."""
    a, b = urlsplit(url), urlsplit(root)
    if (a.scheme or "").lower() not in ("http", "https") or not a.hostname or not b.hostname:
        return False
    return registrable(a.hostname) == registrable(b.hostname)


def _read_sitemap(
    url: str, timeout: float, delay: float, use_proxy: bool = False,
    seen: set[str] | None = None, depth: int = 0,
    root: str | None = None, allow_private: bool | None = None,
    notes: dict[str, list[str]] | None = None,
) -> list[str]:
    """
    Читает sitemap.xml. Индекс карт разворачивается рекурсивно, глубина ограничена.

    `root` — сайт аудита: карты с других регистрируемых доменов не читаются
    и попадают в `notes["skipped"]`. Остановленные карты (распаковка выше
    потолка) — в `notes["errors"]` строкой «адрес: причина».
    """
    seen = seen if seen is not None else set()
    notes = notes if notes is not None else {"skipped": [], "errors": []}
    if depth > 3 or url in seen or len(seen) > 50:
        return []
    if root is not None and not _own_domain(url, root):
        if url not in notes["skipped"]:
            notes["skipped"].append(url)
        return []
    seen.add(url)

    got = _fetch(url, timeout=timeout, use_proxy=use_proxy, allow_private=allow_private)
    if got.error or got.status != 200 or not got.body:
        return []

    body = got.body
    if body[:2] == b"\x1f\x8b":                      # .xml.gz
        try:
            body = _gunzip_limited(body)
        except SitemapTooLarge as e:
            notes["errors"].append(f"{url}: {e}")
            return []
        except (zlib.error, OSError):
            return []

    try:
        root_el = ET.fromstring(body)
    except ET.ParseError:
        return []

    locs: list[str] = []
    for child in root_el:
        for sub in child:
            if _xml_tag(sub.tag) == "loc" and (sub.text or "").strip():
                locs.append(sub.text.strip())
                break

    if _xml_tag(root_el.tag) == "sitemapindex":
        out: list[str] = []
        for nested in locs:
            if root is not None and not _own_domain(nested, root):
                if nested not in notes["skipped"]:
                    notes["skipped"].append(nested)
                continue
            time.sleep(delay)
            out.extend(_read_sitemap(nested, timeout, delay, use_proxy, seen, depth + 1,
                                     root=root, allow_private=allow_private, notes=notes))
        return out

    return locs


# ──────────────────────────── обход сайта ─────────────────────────────


def audit_site(
    root_url: str,
    max_pages: int = 50,
    delay: float = 0.3,
    use_sitemap: bool = True,
    timeout: float = 15.0,
    check_links: int = 30,
    use_proxy: bool = False,
    allow_private: bool | None = None,
) -> SiteAudit:
    """
    Обходит сайт и собирает технический аудит.

    Порядок: robots.txt → sitemap.xml (если разрешено) → обход в ширину по
    внутренним ссылкам. За пределы домена не выходим, не-HTML пропускаем,
    URL дедуплицируем по нормализованному виду.

    `check_links` — сколько внутренних ссылок, не попавших в обход, дополнительно
    проверить на битость. Живые в отчёт не попадают, битые попадают.

    `allow_private` — разрешить внутренние адреса (localhost, 10/8, 192.168/16 …);
    по умолчанию решает YASEO_ALLOW_PRIVATE. Только http и https.

    Ошибки наружу не бросаются: причина уезжает в поле error.
    """
    started = datetime.now(timezone.utc).isoformat()
    allow = net.private_allowed(allow_private)

    try:
        root = normalize_url(root_url)
    except Exception as e:  # noqa: BLE001
        return SiteAudit(root=root_url, crawled_at=started, pages_crawled=0,
                         robots_txt=False, sitemap_urls=0,
                         error=f"не разобрал адрес: {type(e).__name__}: {e}")

    if not _host(root):
        return SiteAudit(root=root_url, crawled_at=started, pages_crawled=0,
                         robots_txt=False, sitemap_urls=0,
                         error="в адресе нет домена, ожидается вида https://example.ru")

    try:
        net.check_url(net.safe_url(root), allow)
    except net.UnsafeURL as e:
        return SiteAudit(root=root, crawled_at=started, pages_crawled=0,
                         robots_txt=False, sitemap_urls=0, error=str(e.reason))
    except ValueError as e:
        return SiteAudit(root=root, crawled_at=started, pages_crawled=0,
                         robots_txt=False, sitemap_urls=0, error=f"не разобрал адрес: {e}")

    try:
        has_robots, rp, declared_maps = _read_robots(root, timeout, use_proxy, allow)

        sitemap_all: list[str] = []
        notes: dict[str, list[str]] = {"skipped": [], "errors": []}
        if use_sitemap:
            seen_maps: set[str] = set()
            for m in (declared_maps or [urljoin(root, "/sitemap.xml")]):
                if not _own_domain(m, root):
                    if m not in notes["skipped"]:
                        notes["skipped"].append(m)
                    continue
                time.sleep(delay)
                sitemap_all.extend(_read_sitemap(m, timeout, delay, use_proxy, seen_maps,
                                                 root=root, allow_private=allow, notes=notes))

        seeds: list[str] = []
        seen: set[str] = set()
        for loc in sitemap_all:
            norm = normalize_url(loc)
            key = dedup_key(norm)
            if key not in seen and _same_site(norm, root) and _crawlable(norm):
                seen.add(key)
                seeds.append(norm)

        root_key = dedup_key(root)
        queue: list[str] = [root] + [s for s in seeds if dedup_key(s) != root_key]
        queued: set[str] = {dedup_key(u) for u in queue}
        visited: set[str] = set()
        pages: list[PageAudit] = []
        skipped_by_robots: list[str] = []
        first = True

        while queue and len(pages) < max_pages:
            url = queue.pop(0)
            key = dedup_key(url)
            if key in visited:
                continue
            visited.add(key)

            if has_robots and not rp.can_fetch(ROBOTS_TOKEN, url):
                skipped_by_robots.append(url)
                continue

            if not first:
                time.sleep(delay)
            first = False

            page = audit_page(url, root=root, timeout=timeout, use_proxy=use_proxy,
                              allow_private=allow)
            pages.append(page)

            for link in page.internal_links:
                link_key = dedup_key(link)
                if link_key not in visited and link_key not in queued and _crawlable(link):
                    queued.add(link_key)
                    queue.append(link)

        crawled = len(pages)

        # Внутренние ссылки, до которых обход не дошёл: проверяем на битость.
        known = {dedup_key(p.url) for p in pages}
        pending: list[str] = []
        for p in pages:
            for link in p.internal_links:
                link_key = dedup_key(link)
                if link_key not in known and _crawlable(link):
                    known.add(link_key)
                    pending.append(link)

        for link in pending[: max(0, check_links)]:
            if has_robots and not rp.can_fetch(ROBOTS_TOKEN, link):
                continue
            time.sleep(delay)
            got = _fetch(link, timeout=timeout, use_proxy=use_proxy, allow_private=allow)
            if got.error or got.status is None or got.status >= 400:
                pages.append(_empty_page(link, got.status, got.chain, got.error))

        issues = find_issues(pages, root, sitemap=seeds, robots_disallowed=skipped_by_robots)
        for line in notes["errors"]:
            map_url, _, why = line.partition(": ")
            issues.append(Issue(
                "major", "sitemap-too-large", map_url, why,
                "Разбейте карту на несколько файлов и объявите их индексом карт: "
                "протокол sitemaps.org ограничивает файл 50 МБ без сжатия и 50 000 адресов."))
        for map_url in notes["skipped"]:
            issues.append(Issue(
                "minor", "sitemap-foreign-host", map_url,
                f"карта на другом домене, сайт аудита — {_host(root)}; не читалась",
                "Держите карты сайта на своём домене: адреса из чужой карты поиск "
                "принимает только при подтверждённых правах на оба сайта."))
        issues.sort(key=lambda i: SEVERITY_ORDER.get(i.severity, 9))

        return SiteAudit(
            root=root,
            crawled_at=started,
            pages_crawled=crawled,
            robots_txt=has_robots,
            sitemap_urls=len(seeds),
            pages=pages,
            issues=issues,
            robots_disallowed=skipped_by_robots,
            sitemaps_skipped=list(notes["skipped"]),
            sitemap_errors=list(notes["errors"]),
        )

    except Exception as e:  # noqa: BLE001 — наружу из публичной функции не бросаем
        return SiteAudit(root=root, crawled_at=started, pages_crawled=0,
                         robots_txt=False, sitemap_urls=0,
                         error=f"{type(e).__name__}: {e}")


# ───────────────────────────── проверки ───────────────────────────────


def _auditable(p: PageAudit) -> bool:
    """Разметку есть смысл проверять, только если страница отдалась как HTML."""
    return p.error is None and p.status is not None and 200 <= p.status < 300


def find_issues(pages: list[PageAudit], root: str,
                sitemap: list[str] | None = None,
                robots_disallowed: list[str] | None = None) -> list[Issue]:
    """
    Ищет проблемы по собранным страницам.

    В evidence кладётся реальное значение — код ответа, длина, процитированный
    тег, адрес дубля. Формулировок вроде «не оптимизировано» здесь нет.

    Страница под `noindex` судится не по правилам витрины. Если гнать все
    проверки по всем страницам сразу, больше половины находок оказывается
    шумом: «critical noindex» на страницах, закрытых намеренно (тег с одной
    статьёй — дубль самой статьи, пустая рубрика), плюс придирки к заголовкам,
    описаниям и объёму тех же закрытых страниц.

    Отсюда два решения:

    1. `noindex` сам по себе не дефект — дефект противоречие. Страница закрыта
       и при этом заявлена в sitemap: одной рукой отдаём на индексацию, другой
       запрещаем. Только это остаётся critical. Закрыта и в карте не заявлена —
       решение автора сайта, чекеру возражать нечем.
    2. К закрытой странице не применяются проверки витрины: title, description,
       объём текста, og, schema, дубли и сиротство. В выдачу она не идёт, мерить
       её длиной сниппета бессмысленно. Остаются проверки, верные независимо от
       индекса: код ответа, битые ссылки, цепочки редиректов, canonical, h1.

    Молча пропускать нельзя: закрытые страницы называются одной строкой
    `noindex-not-checked` на корне — чтобы «проверок нет» было видно в отчёте,
    а не только в этом комментарии.

    То же самое правило — для `robots.txt`. Страница, закрытая `Disallow`,
    не должна выпадать из обхода молча: противоречие «Disallow + sitemap»
    обязано показываться. Логика та же,
    что у noindex: сама по себе блокировка в robots.txt не дефект (это может
    быть решение автора сайта), дефект — когда та же страница одновременно
    заявлена в sitemap.xml. Остальные закрытые роботом адреса называются
    одной строкой `robots-disallow-not-checked`, чтобы молчание не читалось
    как «всё чисто»: эти страницы не обходились вовсе, по ним нет даже
    базовых проверок (title, h1, canonical) — обход в них не заходил.
    """
    issues: list[Issue] = []
    add = issues.append

    ok = [p for p in pages if _auditable(p)]
    root_key = dedup_key(root)
    in_sitemap = {dedup_key(u) for u in (sitemap or [])}

    def _closed(p: PageAudit) -> bool:
        return bool(p.robots_meta) and "noindex" in p.robots_meta.lower()

    closed = {dedup_key(p.url) for p in ok if _closed(p)}
    # Витрина проверяется только у страниц, которые в эту витрину идут.
    shown = [p for p in ok if dedup_key(p.url) not in closed]

    # Кто на кого ссылается — нужно для битых ссылок и страниц-сирот.
    # Ключ — dedup_key, иначе `/uslugi` и `/uslugi/` посчитаются разными страницами.
    incoming: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        page_key = dedup_key(p.url)
        for link in p.internal_links:
            link_key = dedup_key(link)
            if link_key != page_key:
                incoming[link_key].append(p.url)

    # ── critical ──

    for p in pages:
        if p.status is None:
            add(Issue("critical", "page-unavailable", p.url,
                      p.error or "ответа нет",
                      "Проверить доступность страницы и код ответа сервера"))
        elif p.status >= 500:
            add(Issue("critical", "page-unavailable", p.url, f"HTTP {p.status}",
                      "Починить ответ сервера: 5xx выбрасывает страницу из индекса"))
        elif 400 <= p.status < 500:
            sources = incoming.get(dedup_key(p.url), [])
            if sources:
                where = ", ".join(_short(s) for s in sources[:3])
                add(Issue("critical", "broken-internal-link", p.url,
                          f"HTTP {p.status}, ссылка со страниц: {where}",
                          "Поправить или снять ссылку — она ведёт в никуда"))
            else:
                add(Issue("critical", "page-unavailable", p.url, f"HTTP {p.status}",
                          "Вернуть страницу или отдать 410, если она удалена навсегда"))

        if len(p.redirect_chain) > MAX_REDIRECT_HOPS:
            path = " → ".join([_short(p.url)] + [_short(u) for u in p.redirect_chain])
            add(Issue("critical", "redirect-chain", p.url,
                      f"{len(p.redirect_chain)} хопа: {path}",
                      "Свести цепочку к одному редиректу — сразу на конечный адрес"))

    # Закрытая страница — дефект только тогда, когда она же заявлена в sitemap.
    for p in ok:
        if _closed(p) and dedup_key(p.url) in in_sitemap:
            add(Issue("critical", "noindex-в-карте", p.url,
                      f'<meta name="robots" content="{p.robots_meta}"> '
                      "— и при этом адрес заявлен в sitemap.xml",
                      "Свести две команды к одной: убрать адрес из карты "
                      "либо снять noindex, если страница должна быть в поиске"))

    if closed:
        names = ", ".join(sorted(_short(p.url) for p in ok if _closed(p))[:5])
        tail = "" if len(closed) <= 5 else f" и ещё {len(closed) - 5}"
        add(Issue("minor", "noindex-not-checked", root,
                  f"{len(closed)} страниц закрыты от индекса: {names}{tail}",
                  "Ничего чинить не нужно: заголовки, описания, объём текста "
                  "и og у них не проверялись — в выдачу они не идут"))

    # Закрытый в robots.txt адрес — дефект только тогда, когда он же заявлен
    # в sitemap.xml. Страница не обходилась вовсе, поэтому PageAudit по ней
    # нет — работаем с самим списком адресов, а не с `ok`/`shown`.
    for url in sorted(robots_disallowed or [], key=_short):
        if dedup_key(url) in in_sitemap:
            add(Issue("critical", "robots-disallow-в-карте", url,
                      "закрыт в robots.txt (Disallow) — и при этом адрес "
                      "заявлен в sitemap.xml",
                      "Свести две команды к одной: убрать адрес из карты "
                      "либо снять запрет в robots.txt, если страница должна "
                      "быть в поиске"))

    if robots_disallowed:
        names = ", ".join(sorted(_short(u) for u in robots_disallowed)[:5])
        tail = "" if len(robots_disallowed) <= 5 else f" и ещё {len(robots_disallowed) - 5}"
        add(Issue("minor", "robots-disallow-not-checked", root,
                  f"{len(robots_disallowed)} адресов закрыты в robots.txt "
                  f"и обходом не открывались: {names}{tail}",
                  "Ничего чинить не нужно, если так и задумано: title, h1, "
                  "canonical и прочая разметка этих страниц не проверялись — "
                  "обход в них не заходил"))

    # ── major: дубли между страницами ──

    def _dupes(getter, code: str, label: str, fix: str) -> None:
        groups: dict[str, list[PageAudit]] = defaultdict(list)
        for p in shown:
            value = (getter(p) or "").strip().lower()
            if value:
                groups[value].append(p)
        for group in groups.values():
            if len(group) < 2:
                continue
            for p in group:
                others = ", ".join(_short(x.url) for x in group if x.url != p.url)
                add(Issue("major", code, p.url,
                          f'дубль с {others} — {label} "{(getter(p) or "")[:60]}"', fix))

    _dupes(lambda p: p.title, "duplicate-title", "title",
           "Переписать title под задачу конкретной страницы")
    _dupes(lambda p: p.description, "duplicate-description", "description",
           "Написать своё описание под содержимое страницы")

    # ── major: по странице ──

    # Сниппет и входящие ссылки — только у страниц, которые идут в выдачу.
    for p in shown:
        if not p.title:
            add(Issue("major", "missing-title", p.url, "тега <title> в <head> нет",
                      f"Добавить <title> на {TITLE_MIN}–{TITLE_MAX} символов с главным запросом"))
        elif p.title_len > TITLE_MAX:
            add(Issue("major", "title-too-long", p.url,
                      f'title {p.title_len} симв: "{p.title}"',
                      f"Сократить до {TITLE_MAX} символов, главное вынести в первые 50"))

        if not p.description:
            add(Issue("major", "missing-description", p.url,
                      'тега <meta name="description"> нет',
                      f"Добавить description на {DESC_MIN}–{DESC_MAX} символов"))

        if dedup_key(p.url) != root_key and not incoming.get(dedup_key(p.url)):
            add(Issue("major", "orphan-page", p.url,
                      f"нет входящих внутренних ссылок среди {len(pages)} проверенных страниц",
                      "Поставить ссылку из меню, листинга или связанного материала"))

    # Разметка страницы верна независимо от индекса — проверяем у всех.
    for p in ok:
        if not p.h1:
            add(Issue("major", "missing-h1", p.url, "тега <h1> на странице нет",
                      "Добавить один <h1> — заголовок страницы, не логотип"))
        elif len(p.h1) > 1:
            quoted = " | ".join(f'"{h[:40]}"' for h in p.h1[:3])
            add(Issue("major", "multiple-h1", p.url,
                      f"{len(p.h1)} тегов <h1>: {quoted}",
                      "Оставить один <h1>, остальные понизить до <h2>"))

        if not p.canonical:
            add(Issue("major", "missing-canonical", p.url, 'нет <link rel="canonical">',
                      "Добавить canonical на сам адрес страницы"))
        elif not _same_site(p.canonical, root):
            add(Issue("major", "canonical-external", p.url,
                      f'<link rel="canonical" href="{p.canonical}"> — чужой домен',
                      "Указать canonical в пределах своего домена"))
        elif dedup_key(p.canonical) != dedup_key(p.url):
            # Свой домен, но другой адрес. `dedup_key` — та же мера, что для
            # дублей title/description: хвостовой слеш и метки кампаний —
            # не различие (см. dedup_key и _TRACKING_PARAMS). Отличается путь —
            # значит canonical действительно ведёт на другую страницу сайта,
            # например страница статьи с canonical на главную.
            add(Issue("major", "canonical-mismatch", p.url,
                      f'<link rel="canonical" href="{p.canonical}"> '
                      f"— страница {_short(p.url)}, а canonical ведёт на другой адрес",
                      "Проверить: если это действительно другая страница (не "
                      "тот же адрес с иным слешем/меткой), решить — либо это "
                      "осознанный дубль и контент можно свести к одной странице, "
                      "либо canonical поставлен по ошибке и должен указывать "
                      "на саму страницу"))

    # ── minor ──

    for p in shown:
        if p.title and p.title_len < TITLE_MIN:
            add(Issue("minor", "title-too-short", p.url,
                      f'title {p.title_len} симв: "{p.title}"',
                      f"Расширить до {TITLE_MIN}–{TITLE_MAX} символов, добавить уточнение"))

        if p.description:
            if p.description_len < DESC_MIN:
                add(Issue("minor", "description-too-short", p.url,
                          f'description {p.description_len} симв: "{p.description}"',
                          f"Расширить до {DESC_MIN}–{DESC_MAX} символов"))
            elif p.description_len > DESC_MAX:
                add(Issue("minor", "description-too-long", p.url,
                          f'description {p.description_len} симв: "{p.description[:80]}…"',
                          f"Сократить до {DESC_MAX} символов — остальное обрежет выдача"))

        if p.images_no_alt:
            add(Issue("minor", "images-no-alt", p.url,
                      f"{p.images_no_alt} из {p.images_total} <img> без alt",
                      "Прописать alt: что на картинке, без перечисления ключей"))

        if p.word_count < THIN_WORDS:
            add(Issue("minor", "thin-content", p.url,
                      f"{p.word_count} слов в тексте страницы",
                      f"Дописать до {THIN_WORDS}+ слов по существу запроса"))

        missing_og = [k for k in ("og:title", "og:description", "og:image") if k not in p.og]
        if missing_og:
            add(Issue("minor", "missing-og", p.url, f"нет {', '.join(missing_og)}",
                      "Добавить og:title, og:description и og:image 1200×630"))

        if not p.schema_types:
            add(Issue("minor", "missing-schema", p.url, "нет JSON-LD и microdata",
                      "Разметить страницу schema.org: Organization, Article, Service"))

    issues.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.code, i.url))
    return issues


# ─────────────────────────────── вывод ────────────────────────────────


def _print_report(a: SiteAudit) -> None:
    print(f"\nАудит: {a.root}")
    print(f"  Дата обхода:      {a.crawled_at}")
    print(f"  Страниц обойдено: {a.pages_crawled}")
    print(f"  robots.txt:       {'есть' if a.robots_txt else 'нет'}")
    print(f"  URL в sitemap:    {a.sitemap_urls if a.sitemap_urls else 'нет данных'}")
    for m in a.sitemaps_skipped:
        print(f"  карта пропущена:  {m} — другой домен")
    for line in a.sitemap_errors:
        print(f"  карта остановлена: {line}")
    print(f"  Найдено проблем:  {len(a.issues)}")

    if not a.issues:
        print("\nПо нашим проверкам проблем нет.")
        return

    print(f"\n{'тяжесть':<9} {'код':<24} {'страница':<30} доказательство")
    print("-" * 116)
    for i in a.issues:
        print(f"{i.severity:<9} {i.code:<24} {_short(i.url)[:30]:<30} {i.evidence[:50]}")

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for i in a.issues:
        counts[(i.severity, i.code)] += 1

    print("\nСчётчики по кодам:")
    print(f"  {'тяжесть':<9} {'код':<24} {'шт':>4}")
    for (sev, code), n in sorted(
        counts.items(), key=lambda kv: (SEVERITY_ORDER.get(kv[0][0], 9), -kv[1], kv[0][1])
    ):
        print(f"  {sev:<9} {code:<24} {n:>4}")

    by_sev: dict[str, int] = defaultdict(int)
    for i in a.issues:
        by_sev[i.severity] += 1
    print("\nИтого — " + "  ".join(f"{s}: {by_sev.get(s, 0)}" for s in SEVERITY_ORDER))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Технический аудит сайта своим краулером")
    p.add_argument("--url", required=True, help="адрес сайта, напр. https://example.ru")
    p.add_argument("--max-pages", type=int, default=50, help="потолок страниц в обходе")
    p.add_argument("--delay", type=float, default=0.3, help="пауза между запросами, с")
    p.add_argument("--timeout", type=float, default=15.0, help="таймаут запроса, с")
    p.add_argument("--no-sitemap", action="store_true",
                   help="не читать sitemap.xml, идти по внутренним ссылкам")
    p.add_argument("--proxy", action="store_true",
                   help="ходить через системный прокси (http_proxy/https_proxy); по умолчанию напрямую")
    p.add_argument("--allow-private", action="store_true",
                   help="разрешить внутренние адреса (localhost, 10.x, 192.168.x …); "
                        "по умолчанию отклоняются")
    p.add_argument("--json", help="сохранить полный результат в JSON")
    p.add_argument("--title-max", type=int, default=None, help=f"потолок title, по умолчанию {TITLE_MAX}")
    p.add_argument("--desc-min", type=int, default=None, help=f"нижняя граница description, по умолчанию {DESC_MIN}")
    p.add_argument("--desc-max", type=int, default=None, help=f"верхняя граница description, по умолчанию {DESC_MAX}")
    args = p.parse_args(argv)
    set_limits(args.title_max, args.desc_min, args.desc_max)

    audit = audit_site(
        args.url,
        max_pages=args.max_pages,
        delay=args.delay,
        use_sitemap=not args.no_sitemap,
        timeout=args.timeout,
        use_proxy=args.proxy,
        allow_private=True if args.allow_private else None,
    )

    if audit.error:
        print(f"ОШИБКА: {audit.error}", file=sys.stderr)
        return 1

    _print_report(audit)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(audit.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"\nСохранено: {args.json}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
