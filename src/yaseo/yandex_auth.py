#!/usr/bin/env python3
"""
Доступ к сервисам на Яндекс ID: токен, продление, запрос с повтором при 401.

Метрика и Вебмастер живут на Яндекс ID. Wordstat и Search API — на
сервисном ключе Яндекс Облака (`YANDEX_AI_STUDIO_API_KEY`). Это разные
семейства доступа: облачный ключ Метрика и Вебмастер не принимают и отвечают
401/403. Это не недонастройка, и подбирать заголовки бесполезно.

Токен живёт около полугода. Продление вшито в сам запрос: получили 401 —
один раз обновились по refresh и повторили. Человек с браузером нужен
однажды, на `exchange`.

Секреты читаются из окружения и файлов `.env` (см. `yaseo.env`), обновлённый
токен пишется в старший найденный файл, а если ключи пришли только из
окружения — в `~/.config/yaseo/.env` с правами 0600. Целиком секреты не
печатаются никогда: в вывод идут только длина и первые символы.

Публичный API:
- token()                    — текущий access-токен
- refresh()                  — продлить по refresh_token и записать
- exchange(code)             — обменять код авторизации, записать оба токена
- api_json(url, ...)         — запрос с авто-продлением при 401
- probe()                    — что именно открывает токен, по каждой службе

Запуск как скрипт:
    yaseo yandex --check
    yaseo yandex --refresh
    yaseo yandex --exchange <код-из-браузера>

Код авторизации: https://oauth.yandex.ru/authorize?response_type=code&client_id=<ClientID>
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import env as _env
from . import net

TOKEN_URL = "https://oauth.yandex.ru/token"


class YandexAuthError(RuntimeError):
    """Токен не принят, прав не хватает или служба ответила ошибкой."""


# ──────────────────────────── ключи ────────────────────────────

def read_env() -> dict[str, str]:
    """Читает ключи каждый раз заново: после refresh кэш врал бы."""
    return _env.load_env(required=())


def write_env(key: str, value: str, quiet: bool = False) -> None:
    """Кладёт или обновляет один ключ, не трогая остальной файл."""
    path = _env.write_env_value(key, value)
    if not quiet:
        print(f"  {key}: сохранён в {path} (длина {len(value)}, начало {value[:6]}…)")


def _need(e: dict[str, str], *keys: str) -> None:
    missing = [k for k in keys if not e.get(k)]
    if missing:
        raise YandexAuthError(_env.missing_keys_message(missing))


def token() -> str:
    tok = read_env().get("YANDEX_OAUTH_TOKEN")
    if not tok:
        raise YandexAuthError(
            "YANDEX_OAUTH_TOKEN не задан. Он нужен только Вебмастеру и Метрике. "
            "Получить: заведите приложение на https://oauth.yandex.ru, впишите "
            "YANDEX_OAUTH_CLIENT_ID и YANDEX_OAUTH_CLIENT_SECRET, затем "
            "yaseo yandex --exchange <код>"
        )
    return tok


# ──────────────────────────── транспорт ────────────────────────────

def opener() -> urllib.request.OpenerDirector:
    """
    Мимо прокси. У Метрики и Вебмастера российская инфраструктура, и через
    зарубежный прокси эти службы могут не отвечать. Вернуть прокси:
    YASEO_USE_PROXY=1.

    Транспорт общий, из `yaseo.net`: токен и client_secret уходят только по
    https и только тому адресу, который назван; перенаправление — ошибка.
    """
    return net.keyed_opener(proxy=_env.use_proxy())


#: Куда токену можно уходить. Токен Яндекс ID открывает Метрику, Вебмастер и
#: Директ; постороннему адресу он не нужен ни при каком вызове.
YANDEX_HOST_SUFFIXES = ("yandex.net", "yandex.ru", "yandex.com")


def _yandex_host(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in YANDEX_HOST_SUFFIXES)


def _post_token(payload: dict[str, str]) -> dict:
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        with opener().open(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise YandexAuthError(f"обмен не удался, HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise YandexAuthError(f"обмен не удался: {exc.reason}") from exc


def _store(payload: dict, quiet: bool = False) -> str:
    access = payload.get("access_token")
    if not access:
        raise YandexAuthError(f"в ответе нет access_token: {str(payload)[:200]}")
    if not quiet:
        print("Получено:")
    write_env("YANDEX_OAUTH_TOKEN", access, quiet=quiet)
    if payload.get("refresh_token"):
        write_env("YANDEX_OAUTH_REFRESH", payload["refresh_token"], quiet=quiet)
    if payload.get("expires_in") and not quiet:
        days = int(payload["expires_in"]) // 86400
        print(f"  живёт: {payload['expires_in']} сек (~{days} дней)")
    return access


def refresh(quiet: bool = False) -> str:
    """Продлевает токен по refresh_token. Возвращает новый access-токен."""
    e = read_env()
    if not e.get("YANDEX_OAUTH_REFRESH"):
        raise YandexAuthError("YANDEX_OAUTH_REFRESH не задан — нужен yaseo yandex --exchange <код>")
    _need(e, "YANDEX_OAUTH_CLIENT_ID", "YANDEX_OAUTH_CLIENT_SECRET")
    return _store(_post_token({
        "grant_type": "refresh_token",
        "refresh_token": e["YANDEX_OAUTH_REFRESH"],
        "client_id": e["YANDEX_OAUTH_CLIENT_ID"],
        "client_secret": e["YANDEX_OAUTH_CLIENT_SECRET"],
    }), quiet=quiet)


def exchange(code: str) -> str:
    """Обменивает код из браузера на пару токенов. Разовая операция."""
    e = read_env()
    _need(e, "YANDEX_OAUTH_CLIENT_ID", "YANDEX_OAUTH_CLIENT_SECRET")
    return _store(_post_token({
        "grant_type": "authorization_code",
        "code": code.strip(),
        "client_id": e["YANDEX_OAUTH_CLIENT_ID"],
        "client_secret": e["YANDEX_OAUTH_CLIENT_SECRET"],
    }))


def api_json(url: str, method: str = "GET", body: dict | None = None,
             timeout: int = 60, scheme: str = "OAuth",
             _tok: str | None = None, _retried: bool = False) -> dict:
    """
    Запрос к API на Яндекс ID. При 401 один раз продлевает токен и повторяет.

    Продление разрешено ровно одно на вызов: иначе протухший refresh загонит
    в цикл обращений к чужому сервису.

    Ошибку не проглатывает: 403 — прав не хватает, 404 — адрес не тот, и оба
    случая должны быть видны вызывающему, а не превращаться в пустой ответ.

    scheme: Метрика и Вебмастер принимают «OAuth <токен>», а Директ v5 требует
    «Bearer <токен>» — с «OAuth» он отвечает 8000 «OAuth token is missing»,
    и проба показывает отсутствие доступа при любом состоянии прав.
    """
    if not _yandex_host(url):
        raise YandexAuthError(f"адрес {url} не принадлежит Яндексу — OAuth-токен туда не отправляется")
    tok = _tok or token()
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"{scheme} {tok}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    try:
        with opener().open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401 and not _retried:
            fresh = refresh(quiet=True)
            return api_json(url, method, body, timeout, scheme,
                            _tok=fresh, _retried=True)
        raise YandexAuthError(f"HTTP {exc.code} на {url}: {payload[:300]}") from exc
    except net.RedirectRefused as exc:
        raise YandexAuthError(str(exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise YandexAuthError(f"сеть недоступна для {url}: {exc.reason}") from exc


# ──────────────────────────── проверка доступа ────────────────────────────
# Раньше вердикт ставился по коду ответа, и служба, вернувшая HTTP 200 с телом
# «Unknown api request», проходила как [OK]. Код ответа — не то же самое, что
# работающий доступ, поэтому каждая проба сама читает тело и говорит, что в нём.

def _ok_if(key: str):
    def check(payload: dict) -> tuple[bool, str]:
        if isinstance(payload, dict) and key in payload:
            return True, "доступ есть"
        return False, f"в ответе нет поля «{key}» — служба не открылась"
    return check


def _direct_verdict(payload: dict) -> tuple[bool, str]:
    err = (payload or {}).get("error") or {}
    if err:
        return False, f"доступ к API Директа не выдан: {err.get('error_string', err)}"
    return True, "доступ есть"


#: (название, метод, адрес, схема Authorization, проверка тела, зачем нужна, влияет ли на SEO)
PROBES = (
    ("Метрика — счётчики", "GET",
     "https://api-metrika.yandex.net/management/v1/counters", "OAuth",
     _ok_if("counters"), "поведение на посадочных страницах", True),
    ("Вебмастер — пользователь", "GET",
     "https://api.webmaster.yandex.net/v4/user", "OAuth",
     _ok_if("user_id"), "показы, клики и средняя позиция от владельца сайта", True),
    ("Директ — кампании", "POST",
     "https://api.direct.yandex.com/json/v5/campaigns", "Bearer",
     _direct_verdict, "токен должен быть выдан со scope direct:api", False),
)


def probe() -> list[dict]:
    """Прогоняет пробы и возвращает разбор: что открылось, что нет и почему."""
    out = []
    for name, method, url, scheme, verdict, note, required in PROBES:
        row = {"name": name, "note": note, "required": required,
               "ok": False, "verdict": "", "body": ""}
        try:
            payload = {"method": "get",
                       "params": {"SelectionCriteria": {}, "FieldNames": ["Id"]}}
            body = api_json(url, method=method, scheme=scheme,
                            body=payload if method == "POST" else None, timeout=25)
            row["ok"], row["verdict"] = verdict(body)
            row["body"] = json.dumps(body, ensure_ascii=False)[:200]
        except YandexAuthError as exc:
            row["verdict"] = str(exc)[:220]
        out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Доступ к Метрике и Вебмастеру")
    p.add_argument("--check", action="store_true", help="что открывает токен")
    p.add_argument("--refresh", action="store_true", help="продлить по refresh_token")
    p.add_argument("--exchange", metavar="КОД", help="обменять код авторизации из браузера")
    args = p.parse_args(argv)

    if args.exchange:
        exchange(args.exchange)
        args.check = True

    if args.refresh and not args.check:
        refresh()
        return 0

    if args.check:
        tok = token()
        print(f"\nПроверяю токен (длина {len(tok)}, начало {tok[:6]}…)\n")
        broken = 0
        for row in probe():
            mark = "[OK]  " if row["ok"] else "[FAIL]"
            if not row["ok"] and row["required"]:
                broken += 1
            print(f"  {mark} {row['name']}: {row['verdict']}")
            print(f"         {row['note']}")
            if row["body"]:
                print(f"         {row['body']}")
            print()
        # Директ для SEO не нужен: его отсутствие проверку не валит,
        # но и за успех не выдаётся.
        return 1 if broken else 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
