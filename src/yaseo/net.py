"""
Общий сетевой слой yaseo: один способ ходить в сеть с ключом и один — без.

Зачем отдельный модуль. Стандартный `urllib` при ответе 3xx сам идёт по новому
адресу и переносит туда заголовки запроса, включая `Authorization`,
`x-api-key` и `x-goog-api-key`, причём разрешает переход с https на http.
Ключ Яндекса, OpenAI или Anthropic уходил бы тому, кого назвал `Location`.
Поэтому:

- `keyed_opener(proxy)` — для запросов с ключом или токеном. Только https,
  перенаправления не выполняются: любой 3xx поднимает `RedirectRefused`
  с понятным текстом, ключ остаётся у адресата, которому предназначен.
- `site_opener(proxy, follow, allow_private)` — для чужих сайтов без ключей
  (аудит, готовность к ИИ-поиску, раскрытие ссылок). Только http и https —
  `file://`, `ftp://`, `data:` не открываются ни на входе, ни на шаге
  перенаправления. Внутренние адреса (localhost, 127/8, 10/8, 172.16/12,
  192.168/16, 169.254/16, ::1, fc00::/7 …) открываются только с явного
  разрешения: `allow_private=True` или `YASEO_ALLOW_PRIVATE=1`.

Прокси. Яндекс — напрямую, если не выставлен `YASEO_USE_PROXY=1`; зарубежные
провайдеры — через системный прокси. Решение принимает вызывающий и передаёт
флагом `proxy`, модуль только строит транспорт.
"""
from __future__ import annotations

import functools
import ipaddress
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import __version__

#: Схемы, по которым вообще ходим.
ALLOWED_SCHEMES = ("http", "https")

#: Сети, которые считаются внутренними. Список явный, а не `is_private`:
#: `is_private` относит к частным и 198.18.0.0/15, а в этот диапазон
#: локальный прокси в режиме fake-ip отображает любые внешние имена —
#: аудит обычного сайта тогда отказывал бы как «внутреннему».
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "192.0.0.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
))

ALLOW_PRIVATE_ENV = "YASEO_ALLOW_PRIVATE"


def user_agent(component: str, extra: str = "") -> str:
    """User-Agent пакета. Версия берётся из `yaseo.__version__` — одно место."""
    ua = f"yaseo-{component}/{__version__}"
    return f"{ua} {extra}".strip() if extra else ua


# ─────────────────────────────── ошибки ───────────────────────────────


class RedirectRefused(urllib.error.URLError):
    """Служба с ключом ответила перенаправлением — за ним не идём."""

    def __init__(self, code: int, from_url: str, to_url: str):
        self.code = code
        self.from_url = from_url
        self.to_url = to_url
        src = urlsplit(from_url).hostname or from_url
        super().__init__(
            f"{src} ответил перенаправлением HTTP {code} на {to_url or '(адрес не указан)'}. "
            "Запрос с ключом по перенаправлениям не ходит, чтобы ключ не ушёл на другой "
            "адрес. Проверьте адрес службы и настройки прокси."
        )


class UnsafeURL(urllib.error.URLError):
    """Адрес с неподдерживаемой схемой или внутренний адрес без разрешения."""


# ─────────────────────────── нормализация адреса ───────────────────────────
#
# Одна функция на весь пакет: по ней ходят аудит, проверка готовности, план
# и транспорт сайтов (`site_opener` пропускает через неё каждый запрос,
# включая шаги перенаправления), и ей же адрес печатается в отчёт.
#
# Зачем. Страница сайта может сослаться на `/o kompanii/` или `/о-компании/`.
# Браузер откроет обе — он сам кодирует пробел и кириллицу. `http.client`
# на первой бросает `InvalidURL`, на второй — `UnicodeEncodeError`, и живая
# страница становится «недоступной».

_HEXDIGITS = frozenset("0123456789abcdefABCDEF")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
#: Путь по RFC 3986: pchar = unreserved / sub-delims / ":" / "@", плюс "/".
_PATH_SAFE = _UNRESERVED | frozenset("!$&'()*+,;=:@/")
#: Запрос: то же и "?".
_QUERY_SAFE = _PATH_SAFE | frozenset("?")
#: userinfo: unreserved / sub-delims / ":".
_USERINFO_SAFE = _UNRESERVED | frozenset("!$&'()*+,;=:")
#: Для отчёта кодируются ещё `*` (оформление Markdown) и `~` (зачёркивание).
#: `~` — unreserved, `%7E` ему равносилен (RFC 3986, 6.2.2.2); `*` серверы
#: декодируют так же, но в запрос он уходит как есть — как у браузера.
_DISPLAY_EXTRA = frozenset("*~")
_HOST_OK = re.compile(r"[a-z0-9._-]+")


def _pct(text: str, safe: frozenset[str]) -> str:
    """Percent-кодирование без повторного: готовое `%XX` остаётся (hex — в
    верхнем регистре, RFC 3986, 6.2.2.1), одиночный `%` становится `%25`."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "%" and i + 2 < n and text[i + 1] in _HEXDIGITS and text[i + 2] in _HEXDIGITS:
            out.append("%" + text[i + 1:i + 3].upper())
            i += 3
            continue
        if ch in safe:
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8", "surrogatepass"))
        i += 1
    return "".join(out)


def _ascii_host(host: str) -> str:
    """Домен в ASCII: кириллица — через IDNA, IPv6 — в скобках. ValueError, если
    домен собрать нельзя (пробел, пустая метка, запрещённый символ)."""
    host = host.lower()
    if ":" in host:
        ipaddress.ip_address(host.split("%", 1)[0])   # ValueError, если не IPv6
        return f"[{host}]"
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as e:
            raise ValueError(f"домен не переводится в IDNA: {e}") from None
    if not _HOST_OK.fullmatch(host):
        raise ValueError("в домене недопустимые символы")
    return host


def safe_url(url: str, display: bool = False) -> str:
    """
    Адрес в том виде, в каком его отправит браузер (RFC 3986).

    - схема и домен — в нижнем регистре, кириллический домен — в IDNA
      (`xn--…`), порт сохраняется;
    - путь и запрос — percent-кодированы: пробел, кириллица, `` ` ``, `|`,
      `"`, `<>`, `{}`, `\\`, `^`, переводы строк; уже закодированное `%XX`
      повторно не кодируется;
    - якорь (`#…`) отбрасывается: на сервер он не уходит.

    `display=True` — вид для отчёта: дополнительно кодируются `*` и `~`, и
    функция не бросает исключений (домен, который не собрать, кодируется
    как есть). Такой адрес не содержит пробелов, переводов строк, `` ` ``,
    `|` и `*` — вставленный в Markdown, он остаётся адресом и не становится
    разметкой. Без `display` на несобираемом домене — ValueError.
    """
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        if not display:
            raise
        return _pct(text, (_UNRESERVED - _DISPLAY_EXTRA) | frozenset("/:?=&"))
    extra = _DISPLAY_EXTRA if display else frozenset()
    path_safe = _PATH_SAFE - extra
    query_safe = _QUERY_SAFE - extra

    netloc = ""
    if parts.netloc:
        try:
            host = _ascii_host(parts.hostname or "")
            port = parts.port
            netloc = host + (f":{port}" if port is not None else "")
            if parts.username is not None:
                user = _pct(parts.username, _USERINFO_SAFE - frozenset(":") - extra)
                if parts.password is not None:
                    user += ":" + _pct(parts.password, _USERINFO_SAFE - extra)
                netloc = f"{user}@{netloc}"
        except ValueError:
            if not display:
                raise
            netloc = _pct(parts.netloc, (_USERINFO_SAFE | frozenset("@[]")) - extra)
    return urlunsplit((
        (parts.scheme or "").lower(),
        netloc,
        _pct(parts.path, path_safe),
        _pct(parts.query, query_safe),
        "",
    ))


# ──────────────────────────── проверка адреса ────────────────────────────


def private_allowed(flag: bool | None = None) -> bool:
    """Разрешены ли внутренние адреса: явный флаг, иначе YASEO_ALLOW_PRIVATE."""
    if flag is not None:
        return bool(flag)
    return os.environ.get(ALLOW_PRIVATE_ENV, "").strip().lower() in ("1", "true", "yes")


#: IPv6-диапазоны, внутри которых зашит IPv4-адрес: NAT64 (RFC 6052, 8215)
#: и устаревшая IPv4-совместимая запись `::a.b.c.d`. Решает зашитый адрес.
_EMBEDDED_V4 = tuple(ipaddress.ip_network(n) for n in (
    "64:ff9b::/96",
    "64:ff9b:1::/48",
    "::/96",
))


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if any(ip in net for net in _EMBEDDED_V4) and ip not in (
            ipaddress.IPv6Address("::"), ipaddress.IPv6Address("::1")):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6:
        if ip.is_multicast:
            return True
        inner = _embedded_v4(ip)
        if inner is not None:
            return _ip_is_private(inner)
    return any(ip.version == net.version and ip in net for net in PRIVATE_NETWORKS)


#: Сколько ждать ответа DNS при проверке адреса, секунд. `socket.getaddrinfo`
#: своего таймаута не имеет: зависший резолвер держал бы аудит и MCP-вызов
#: без предела.
DNS_TIMEOUT = 5.0


def _resolve(host: str) -> list:
    """`getaddrinfo` с пределом по времени. Не ответил — UnsafeURL.

    Молча пропустить проверку на таймауте нельзя: запрос потом разрешит имя
    сам, и медленный DNS стал бы обходом запрета на внутренние адреса.
    """
    box: dict = {}

    def run() -> None:
        try:
            box["infos"] = socket.getaddrinfo(host, None)
        except BaseException as e:  # noqa: BLE001 — передаётся вызывающему
            box["error"] = e

    worker = threading.Thread(target=run, name="yaseo-dns", daemon=True)
    worker.start()
    worker.join(DNS_TIMEOUT)
    if worker.is_alive():
        raise UnsafeURL(
            f"DNS не ответил за {DNS_TIMEOUT:g} с на имя {host}: не удалось проверить, "
            "что адрес не внутренний. Проверьте сеть и повторите"
        )
    if "error" in box:
        raise box["error"]
    return box["infos"]


def private_address(host: str) -> str | None:
    """
    Внутренний ли хост. Возвращает найденный внутренний адрес или None.

    Имя разрешается через DNS: `evil.example` может указывать на 127.0.0.1.
    Если имя не разрешается, решение оставляется самому запросу — он упадёт
    с понятной сетевой ошибкой.
    """
    host = (host or "").strip().strip("[]").lower().rstrip(".")
    if not host:
        return None
    if host == "localhost" or host.endswith(".localhost"):
        return host
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        ip = None
    if ip is not None:
        return str(ip) if _ip_is_private(ip) else None
    try:
        infos = _resolve(host)
    except UnsafeURL:
        # UnsafeURL — наследник OSError: без этой строки таймаут DNS
        # проглатывался бы ниже и проверка молча пропускалась.
        raise
    except (socket.gaierror, UnicodeError, OSError):
        return None
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            continue
        if _ip_is_private(addr):
            return str(addr)
    return None


def check_url(url: str, allow_private: bool | None = None) -> None:
    """Бросает UnsafeURL, если по адресу ходить нельзя."""
    parts = urlsplit(url or "")
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURL(
            f"адрес {url!r}: схема «{scheme or 'нет'}» не поддерживается — только http и https"
        )
    if not parts.hostname:
        raise UnsafeURL(f"адрес {url!r}: не указан домен")
    if not private_allowed(allow_private):
        found = private_address(parts.hostname)
        if found:
            raise UnsafeURL(
                f"адрес {url} ведёт на внутренний адрес ({found}). Такие адреса "
                f"проверяются только с явным разрешением: переменная окружения "
                f"{ALLOW_PRIVATE_ENV}=1 (для MCP-сервера — в его настройках env), "
                "флаг --allow-private у команд yaseo audit и yaseo plan, "
                "в Python — allow_private=True"
            )


# ─────────────────────────────── транспорт ───────────────────────────────


class _HttpsOnly(urllib.request.BaseHandler):
    """Запрос с ключом уходит только по https."""

    handler_order = 100

    def default_open(self, req):  # noqa: D102
        if (req.type or "").lower() != "https":
            raise UnsafeURL(
                f"адрес {req.full_url!r}: ключ отправляется только по https"
            )
        return None


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Любой 3xx на запросе с ключом — ошибка, а не переход."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        try:
            fp.close()
        except Exception:  # noqa: BLE001 — закрытие тела не критично
            pass
        raise RedirectRefused(code, req.full_url, newurl)


class _SiteGuard(urllib.request.BaseHandler):
    """Проверка схемы и внутреннего адреса на каждом запросе, включая шаги перенаправления."""

    handler_order = 100

    def __init__(self, allow_private: bool | None):
        self.allow_private = allow_private

    def default_open(self, req):  # noqa: D102
        check_url(req.full_url, self.allow_private)
        return None

    def _normalize(self, req):
        # Пробел, кириллица и прочее, чего `http.client` не пропустит, — в
        # percent-кодировку до того, как по адресу соберётся строка запроса и
        # заголовок Host. Шаг перенаправления приходит сюда же новым запросом.
        fixed = safe_url(req.full_url)
        if fixed != req.full_url:
            req.full_url = fixed
            # Host ставит HTTPHandler из req.host; явный заголовок со старым
            # (кириллическим) доменом остался бы как есть.
            req.remove_header("Host")
        return req

    http_request = https_request = _normalize


class _NoFollow(urllib.request.HTTPRedirectHandler):
    """Перенаправление не выполняется: 3xx приходит вызывающему как HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class _SafeFollow(urllib.request.HTTPRedirectHandler):
    """Идёт по перенаправлениям, но каждый новый адрес проходит ту же проверку."""

    def __init__(self, allow_private: bool | None):
        super().__init__()
        self.allow_private = allow_private

    def http_error_302(self, req, fp, code, msg, headers):  # noqa: D102
        # Проверяем адрес до стандартной обработки: она сама отклоняет file://,
        # но ответом «HTTP 302», по которому не понять, что случилось.
        location = headers.get("location") or headers.get("uri")
        if location:
            try:
                check_url(urljoin(req.full_url, location), self.allow_private)
            except UnsafeURL:
                fp.close()
                raise
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        check_url(newurl, self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _director(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    """
    Собирает opener из явного набора обработчиков.

    `urllib.request.build_opener` всегда добавляет FileHandler, FTPHandler
    и DataHandler; здесь их нет, и неизвестная схема падает в UnknownHandler.
    """
    od = urllib.request.OpenerDirector()
    for h in handlers:
        od.add_handler(h)
    for h in (
        urllib.request.UnknownHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        od.add_handler(h)
    return od


def _proxy_handler(proxy: bool) -> urllib.request.ProxyHandler:
    # ProxyHandler() читает системные настройки; ProxyHandler({}) — напрямую.
    return urllib.request.ProxyHandler() if proxy else urllib.request.ProxyHandler({})


@functools.lru_cache(maxsize=None)
def keyed_opener(proxy: bool = False) -> urllib.request.OpenerDirector:
    """Opener для запросов с ключом: только https, без перенаправлений."""
    return _director(
        _proxy_handler(proxy),
        _HttpsOnly(),
        _RefuseRedirect(),
        urllib.request.HTTPSHandler(),
    )


@functools.lru_cache(maxsize=None)
def site_opener(proxy: bool = False, follow: bool = False,
                allow_private: bool | None = None) -> urllib.request.OpenerDirector:
    """
    Opener для сайтов без ключей: http/https, внутренние адреса по разрешению.

    `follow=False` — перенаправление приходит вызывающему как HTTPError с кодом
    3xx (аудит собирает цепочку сам). `follow=True` — переходит, проверяя каждый
    шаг. `allow_private=None` — решение по YASEO_ALLOW_PRIVATE в момент запроса.
    """
    return _director(
        _proxy_handler(proxy),
        _SiteGuard(allow_private),
        _SafeFollow(allow_private) if follow else _NoFollow(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
    )
