"""
Сравнение доменов без внешних библиотек.

Регистрируемый домен (eTLD+1) считается по короткому встроенному списку
многосоставных публичных суффиксов. Полного Public Suffix List здесь нет —
для редких зон (например, ``*.gov.xx``) регистрируемый домен может
оказаться на уровень короче. Для сверки «наш ли это сайт» этого хватает:
сравниваются оба адреса одним и тем же правилом.
"""
from __future__ import annotations

from urllib.parse import urlsplit

#: Многосоставные суффиксы, под которыми регистрируют домены.
MULTI_SUFFIXES = frozenset({
    # Россия и СНГ
    "com.ru", "net.ru", "org.ru", "pp.ru", "msk.ru", "spb.ru", "msk.su",
    "spb.su", "com.ua", "org.ua", "net.ua", "kiev.ua", "com.kz", "org.kz",
    "com.by", "org.by", "co.uz", "com.uz",
    # мир
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "co.kr", "or.kr",
    "com.br", "net.br", "org.br", "com.cn", "net.cn", "org.cn",
    "com.tr", "org.tr", "com.mx", "com.ar", "co.nz", "org.nz",
    "co.in", "net.in", "org.in", "co.za", "com.sg", "com.hk", "co.il",
    "com.pl", "com.es", "co.id", "com.my", "com.vn", "com.tw",
    # хостинги, где каждый поддомен — отдельный владелец
    "github.io", "gitlab.io", "pages.dev", "netlify.app", "vercel.app",
    "herokuapp.com", "web.app", "firebaseapp.com", "blogspot.com",
    "tilda.ws", "narod.ru", "ucoz.ru", "wixsite.com", "livejournal.com",
})

MATCH_MODES = ("site", "host")


def _idna_to_unicode(host: str) -> str:
    labels = []
    for label in host.split("."):
        if label.startswith("xn--"):
            try:
                label = label.encode("ascii").decode("idna")
            except UnicodeError:
                pass
        labels.append(label)
    return ".".join(labels)


def host_of(value: str) -> str:
    """Хост из адреса или голого домена: нижний регистр, без ``www.``,
    порта, точки в конце; IDN — в юникоде. Пусто, если хоста нет."""
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value.lstrip("/")
    try:
        host = urlsplit(value).hostname or ""
    except ValueError:
        return ""
    host = host.strip(".").lower()
    host = _idna_to_unicode(host)
    while host.startswith("www."):
        host = host[4:]
    return host


def registrable(host: str) -> str:
    """Регистрируемый домен: ``shop.example.co.uk`` → ``example.co.uk``."""
    host = host_of(host)
    if not host or _is_ip(host):
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last2 = ".".join(parts[-2:])
    if last2 in MULTI_SUFFIXES:
        return ".".join(parts[-3:])
    return last2


def _is_ip(host: str) -> bool:
    if ":" in host:
        return True
    return all(p.isdigit() for p in host.split(".")) and host.count(".") == 3


def matches(url_or_host: str, domain: str, mode: str = "site") -> bool:
    """Относится ли адрес к нашему домену.

    ``mode="site"`` — весь сайт: совпадает регистрируемый домен, поддомены
    считаются нашими (``blog.example.ru`` ~ ``example.ru``).
    ``mode="host"`` — строго тот же хост (``www.`` не в счёт).
    Если ``domain`` сам задан поддоменом (``blog.example.ru``), в режиме
    ``site`` нашими считаются он и его поддомены, но не соседние.
    """
    if mode not in MATCH_MODES:
        raise ValueError(f"match: ожидается одно из {', '.join(MATCH_MODES)}, получено «{mode}»")
    h = host_of(url_or_host)
    d = host_of(domain)
    if not h or not d:
        return False
    if mode == "host":
        return h == d
    if d != registrable(d):
        return h == d or h.endswith("." + d)
    return registrable(h) == d


def looks_like_domain(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t or " " in t or "." not in t or "/" in t:
        return False
    return all(part for part in t.split("."))
