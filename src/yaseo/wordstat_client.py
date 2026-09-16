#!/usr/bin/env python3
"""
Клиент Yandex Wordstat API v2 (GetTop).

Адрес:   https://searchapi.api.cloud.yandex.net/v2/wordstat/topRequests
Доступ:  Api-Key сервисного аккаунта Yandex AI Studio
Тариф:   см. rates.json (за 1000 обращений)
Справка: https://yandex.cloud/ru/docs/search-api/operations/wordstat/get-top

Ключи (см. `yaseo.env`):
- YC_FOLDER_ID
- YANDEX_AI_STUDIO_API_KEY

Публичный API:
- top_requests(phrase, regions=["225"], devices=["DEVICE_ALL"], num=200) -> dict
- sweep(seeds_path, out_path, regions, devices, num, delay) -> int
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import net
from .env import SEARCH_KEYS, use_proxy
from .env import load_env as _load_env

ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/wordstat/topRequests"


def load_env() -> dict[str, str]:
    return _load_env(required=SEARCH_KEYS)


#: Прокси по умолчанию снимается, как у выдачи: `urllib` молча подхватывает
#: HTTPS_PROXY, и если прокси выводит трафик за рубеж, Яндекс может рвать
#: соединение на рукопожатии — в снимке остаётся
#: `SSL: UNEXPECTED_EOF_WHILE_READING`, а частота выходит нулевой у всех фраз.
#: Выключатель один на оба пути к Яндексу: YASEO_USE_PROXY=1 вернёт прокси.
#:
#: Транспорт общий, из `yaseo.net`: запрос несёт ключ, поэтому только https
#: и без перехода по перенаправлениям.
def opener() -> urllib.request.OpenerDirector:
    return net.keyed_opener(proxy=use_proxy())


def top_requests(
    phrase: str,
    regions: list[str] | None = None,
    devices: list[str] | None = None,
    num: int = 200,
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict:
    env = env or load_env()
    body = {
        "phrase": phrase,
        "folderId": env["YC_FOLDER_ID"],
        "numPhrases": num,
        "regions": regions or ["225"],
        "devices": devices or ["DEVICE_ALL"],
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Api-Key {env['YANDEX_AI_STUDIO_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with opener().open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")
        return {"_error": {"status": e.code, "reason": e.reason, "body": body_txt}}
    except urllib.error.URLError as e:
        return {"_error": {"reason": str(e.reason)}}
    except Exception as e:  # pragma: no cover
        return {"_error": {"reason": repr(e)}}


def sweep(
    seeds_path: Path,
    out_path: Path,
    regions: list[str],
    devices: list[str],
    num: int,
    delay: float,
) -> int:
    """
    Прогон по списку сидов: `{"keywords": [{"phrase": ...}, ...]}` → JSONL,
    по строке на сид. Возвращает число ошибок.
    """
    env = load_env()
    seeds = json.loads(seeds_path.read_text(encoding="utf-8"))["keywords"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok = err = 0
    with out_path.open("w", encoding="utf-8") as f:
        for i, seed in enumerate(seeds, 1):
            phrase = seed["phrase"]
            res = top_requests(phrase, regions=regions, devices=devices, num=num, env=env)
            row = {
                "seed": phrase,
                "cluster": seed.get("cluster"),
                "priority": seed.get("priority"),
                "intent": seed.get("intent"),
                "regions": regions,
                "devices": devices,
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "response": res,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if "_error" in res:
                err += 1
                print(f"[{i:02}/{len(seeds)}] ERR  {phrase!r}: {res['_error']}", file=sys.stderr)
            else:
                ok += 1
                total = (res.get("totalCount") or 0)
                top_n = len(res.get("results") or [])
                assoc_n = len(res.get("associations") or [])
                print(f"[{i:02}/{len(seeds)}] OK   {phrase!r:60} total={total:>7}  top={top_n:>3}  assoc={assoc_n:>3}")
            time.sleep(delay)
    print(f"\nитог: ok={ok} err={err} out={out_path}", file=sys.stderr)
    return err
