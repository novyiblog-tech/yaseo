#!/usr/bin/env python3
"""
Провайдер выдачи Яндекса — Yandex Search API v2.

Endpoint: https://searchapi.api.cloud.yandex.net/v2/web/searchAsync
Операции: https://operation.api.cloud.yandex.net/operations/<id>
Auth:     Api-Key сервисного аккаунта Яндекс Облака (тот же, что у Wordstat)
Docs:     https://yandex.cloud/ru/docs/search-api/operations/web-search

Тарифицируется за запрос (см. rates.json). Ходим только отложенными
запросами: синхронные в прайсе Яндекса в разы дороже.

Публичный API:
- serp(query, region, n)          -> SerpResult   (поиск + ожидание + разбор,
                                                   глубина n набирается страницами по 10)
- pages_for(n)                    -> int          (сколько платных обращений стоит глубина n)
- serp_batch(queries, ...)        -> list[SerpResult]
- search_async(...)               -> operation_id
- wait_operation(operation_id)    -> xml
- parse_serp(xml, query)          -> SerpResult

Запуск как скрипт:
    yaseo serp --query "пластиковые окна" --n 20
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import net
from .env import SEARCH_KEYS, use_proxy
from .env import load_env as _load_env


def load_env() -> dict[str, str]:
    return _load_env(required=SEARCH_KEYS)


SEARCH_ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATION_ENDPOINT = "https://operation.api.cloud.yandex.net/operations"

RUSSIA = 225

#: Сколько результатов Яндекс показывает на одной странице живому пользователю.
#: Глубина набирается страницами, а не раздуванием groupsOnPage: при groupsOnPage>10
#: Яндекс отдаёт другой набор и другой порядок, и нумерация перестаёт совпадать с реальностью.
PAGE_SIZE = 10

#: Потолок глубины за один снимок. Каждая страница — отдельное платное обращение,
#: 50 = 5 страниц. Справка допускает до 250 результатов на запрос, но позиции
#: глубже 50-й для задач пакета ничего не решают, а цена растёт линейно.
MAX_DEPTH = 50


def pages_for(n: int) -> int:
    """Сколько страниц (= платных обращений к Search API) стоит глубина n.

    n=10 → 1, n=20 → 2, n=30 → 3, n=50 → 5. Глубина ограничивается MAX_DEPTH.
    """
    depth = max(1, min(int(n), MAX_DEPTH))
    return -(-depth // PAGE_SIZE)

#: Блоки-колдунщики: картинки, видео, карты, товары. Занимают место в выдаче,
#: но конкурентами не являются и в органической нумерации не участвуют.
WIZARD_TITLE = re.compile(r"^(картинки|видео|карты|товары|новости)\s+по запросу", re.I)

#: Если в окружении выставлены HTTP_PROXY/HTTPS_PROXY, urllib подхватывает их
#: молча, и запросы к Яндексу могут уйти за рубеж. Для инструмента под Яндекс
#: это лишняя зависимость и лишняя задержка, поэтому по умолчанию ходим
#: напрямую. Включить прокси: YASEO_USE_PROXY=1.
#:
#: Запрос несёт ключ, поэтому транспорт общий, из `yaseo.net`: только https,
#: перенаправление — ошибка, а не переход (иначе ключ ушёл бы по Location).
def opener() -> urllib.request.OpenerDirector:
    return net.keyed_opener(proxy=use_proxy())


@dataclass
class SerpDoc:
    """Один документ выдачи."""

    #: Место в выдаче как её видит пользователь, вместе с блоками Яндекса.
    position: int
    #: Место среди сайтов, без блоков-колдунщиков. По нему меряются позиции.
    organic_position: int | None
    #: Блок Яндекса (картинки, видео, карты), а не сайт.
    is_wizard: bool
    url: str
    domain: str
    title: str
    #: сколько слов запроса подсвечено в title — прямой сигнал заточенности страницы
    title_hlwords: int
    #: глубина пути URL: 0 = главная страница домена
    url_depth: int
    #: сколько страниц этого домена Яндекс нашёл по запросу
    domain_doccount: int
    modtime: str | None
    passage: str | None


@dataclass
class SerpResult:
    query: str
    region: int
    fetched_at: str
    #: сколько всего документов Яндекс нашёл по запросу
    found_all: int
    docs: list[SerpDoc] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _post(url: str, body: dict, api_key: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Api-Key {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url: str, api_key: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Api-Key {api_key}"})
    with opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def search_async(
    query: str,
    region: int = RUSSIA,
    n: int = PAGE_SIZE,
    page: int = 0,
    env: dict[str, str] | None = None,
) -> str:
    """Ставит поисковую задачу на одну страницу выдачи. Возвращает id операции.

    `n` — сколько групп на странице (groupsOnPage), `page` — номер страницы
    с нуля (query.page). Справка: aistudio.yandex.ru/ru/docs/search-api/concepts/web-search.
    Глубину больше страницы набирает `serp`, а не раздутый `n`.
    """
    env = env or load_env()
    body = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": query,
            "page": str(page),
            "fixTypoMode": "FIX_TYPO_MODE_OFF",
        },
        "groupSpec": {
            "groupMode": "GROUP_MODE_DEEP",
            "groupsOnPage": str(n),
            "docsInGroup": "1",
        },
        "maxPassages": "3",
        "region": str(region),
        "l10N": "LOCALIZATION_RU",
        "folderId": env["YC_FOLDER_ID"],
        "responseFormat": "FORMAT_XML",
    }
    res = _post(SEARCH_ENDPOINT, body, env["YANDEX_AI_STUDIO_API_KEY"])
    op_id = res.get("id")
    if not op_id:
        raise RuntimeError(f"Search API не вернул id операции: {res}")
    return op_id


def wait_operation(
    operation_id: str,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
    poll: float = 1.5,
) -> str:
    """Ждёт готовности операции. Возвращает XML выдачи."""
    env = env or load_env()
    key = env["YANDEX_AI_STUDIO_API_KEY"]
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        op = _get(f"{OPERATION_ENDPOINT}/{operation_id}", key)
        if op.get("done"):
            if "error" in op:
                raise RuntimeError(f"операция завершилась ошибкой: {op['error']}")
            raw = (op.get("response") or {}).get("rawData")
            if not raw:
                raise RuntimeError(f"в ответе нет rawData: {op}")
            return base64.b64decode(raw).decode("utf-8", errors="replace")
        time.sleep(poll)

    raise TimeoutError(f"операция {operation_id} не завершилась за {timeout} с")


def _text(node: ET.Element | None) -> str:
    """Собирает текст узла вместе с содержимым вложенных тегов (hlword)."""
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def _url_depth(url: str) -> int:
    path = urlsplit(url).path.strip("/")
    return 0 if not path else len([p for p in path.split("/") if p])


def parse_serp(xml: str, query: str, region: int = RUSSIA) -> SerpResult:
    fetched_at = datetime.now(timezone.utc).isoformat()
    root = ET.fromstring(xml)

    err = root.find(".//response/error")
    if err is not None:
        return SerpResult(
            query=query,
            region=region,
            fetched_at=fetched_at,
            found_all=0,
            error=_text(err) or err.get("code") or "unknown error",
        )

    found_all = 0
    for f in root.findall(".//response/found"):
        if f.get("priority") == "all":
            found_all = int((f.text or "0").strip() or 0)
            break

    docs: list[SerpDoc] = []
    position = 0
    organic = 0
    for group in root.findall(".//results/grouping/group"):
        categ = group.find("categ")
        group_domain = categ.get("name") if categ is not None else ""
        doccount_node = group.find("doccount")
        doccount = (
            int((doccount_node.text or "0").strip() or 0) if doccount_node is not None else 0
        )

        for doc in group.findall("doc"):
            position += 1
            title_node = doc.find("title")
            passages = doc.find("passages")
            passage_node = passages.find("passage") if passages is not None else None
            url = _text(doc.find("url"))
            title = _text(title_node)

            is_wizard = bool(WIZARD_TITLE.match(title))
            if not is_wizard:
                organic += 1

            docs.append(
                SerpDoc(
                    position=position,
                    organic_position=None if is_wizard else organic,
                    is_wizard=is_wizard,
                    url=url,
                    domain=_text(doc.find("domain")) or group_domain,
                    title=_text(title_node),
                    title_hlwords=len(title_node.findall("hlword")) if title_node is not None else 0,
                    url_depth=_url_depth(url),
                    domain_doccount=doccount,
                    modtime=_text(doc.find("modtime")) or None,
                    passage=_text(passage_node) or None,
                )
            )

    return SerpResult(
        query=query, region=region, fetched_at=fetched_at, found_all=found_all, docs=docs
    )


def _failed(query: str, region: int, reason: str) -> SerpResult:
    return SerpResult(
        query=query,
        region=region,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        found_all=0,
        error=reason,
    )


def merge_pages(pages: list[SerpResult], n: int) -> SerpResult:
    """
    Склеивает страницы выдачи в один результат глубины n.

    Позиции сквозные: документ второй страницы продолжает нумерацию первой.
    Колдунщик занимает место (`position`), но органическую нумерацию не сдвигает.

    Документ, который повторился на следующей странице (выдача сдвинулась
    между обращениями — на живом замере 16.09.2026 так вышло с 1 адресом из 10),
    в список второй раз не попадает, но своё место занимает: нумерация
    остаётся той, что отдал Яндекс, и хвост не съезжает на единицу вверх.
    """
    first = pages[0]
    docs: list[SerpDoc] = []
    seen: set[str] = set()
    position = organic = 0
    for page in pages:
        for d in page.docs:
            position += 1
            if position > n:
                break
            if not d.is_wizard:
                organic += 1
            key = d.url.strip().lower()
            if key and key in seen:
                continue
            seen.add(key)
            docs.append(SerpDoc(**{**asdict(d), "position": position,
                                   "organic_position": None if d.is_wizard else organic}))
    found = max((p.found_all for p in pages), default=0)
    return SerpResult(query=first.query, region=first.region, fetched_at=first.fetched_at,
                      found_all=found, docs=docs)


def _describe(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001 — тело ошибки не критично
            detail = ""
        return f"HTTPError {e.code}: {detail}"
    if isinstance(e, urllib.error.URLError):
        return f"{type(e).__name__}: {e.reason}"
    return f"{type(e).__name__}: {e}"


def _collect(
    tasks: list[tuple[int, str | None, str | None]],
    query: str, region: int, n: int, env: dict[str, str], timeout: float,
) -> SerpResult:
    """
    Дожидается операций страниц одного запроса и склеивает их.

    Если хоть одна страница не пришла, результат помечается ошибкой: неполная
    глубина молча выдала бы «сайта нет в топ-30», хотя страницы 21–30 никто
    не видел. Собранные документы остаются в результате для справки.
    """
    total = pages_for(n)
    pages: list[SerpResult] = []
    problem: str | None = None
    for page_no, op_id, err in tasks:
        if err or not op_id:
            problem = err or "операция не создана"
        else:
            try:
                res = parse_serp(wait_operation(op_id, env=env, timeout=timeout), query, region)
            except Exception as e:  # noqa: BLE001 — причина уезжает в поле error
                problem = _describe(e)
            else:
                problem = res.error
                if not problem:
                    pages.append(res)
        if problem:
            where = f"страница {page_no + 1} из {total}: " if total > 1 else ""
            problem = where + problem
            break
    if len(tasks) < total and not problem:
        problem = f"поставлено {len(tasks)} страниц из {total}"
    if not pages:
        return _failed(query, region, problem or "операция не создана")
    merged = merge_pages(pages, n)
    merged.error = problem
    return merged


def _submit(query: str, region: int, n: int, env: dict[str, str],
            delay: float = 0.0) -> list[tuple[int, str | None, str | None]]:
    """Ставит задачи на все страницы глубины n. Ошибка страницы обрывает постановку."""
    tasks: list[tuple[int, str | None, str | None]] = []
    for page_no in range(pages_for(n)):
        if page_no and delay:
            time.sleep(delay)
        try:
            op_id = search_async(query, region=region, n=PAGE_SIZE, page=page_no, env=env)
            tasks.append((page_no, op_id, None))
        except Exception as e:  # noqa: BLE001 — причина уезжает в поле error
            tasks.append((page_no, None, _describe(e)))
            break
    return tasks


def serp(
    query: str,
    region: int = RUSSIA,
    n: int = PAGE_SIZE,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> SerpResult:
    """
    Поиск + ожидание + разбор. Ошибки возвращаются в поле error, не бросаются.

    Глубина n (не больше MAX_DEPTH) набирается страницами по PAGE_SIZE:
    n=30 — три страницы, то есть три платных обращения (`pages_for(n)`).
    """
    env = env or load_env()
    depth = max(1, min(int(n), MAX_DEPTH))
    return _collect(_submit(query, region, depth, env), query, region, depth, env, timeout)


def serp_batch(
    queries: list[str],
    region: int = RUSSIA,
    n: int = PAGE_SIZE,
    delay: float = 0.3,
    progress: bool = True,
) -> list[SerpResult]:
    """
    Пакетный прогон. Задачи ставятся все сразу, потом собираются результаты —
    так пакет из 96 запросов ждёт секунды вместо минут.

    Обращений — `len(queries) × pages_for(n)`: каждая страница платная.
    """
    env = load_env()
    depth = max(1, min(int(n), MAX_DEPTH))
    pending: list[tuple[str, list[tuple[int, str | None, str | None]]]] = []

    for q in queries:
        pending.append((q, _submit(q, region, depth, env, delay=delay)))
        time.sleep(delay)

    out: list[SerpResult] = []
    for i, (q, tasks) in enumerate(pending, 1):
        try:
            out.append(_collect(tasks, q, region, depth, env, 60.0))
        except Exception as e:  # noqa: BLE001 — причина уезжает в результат
            out.append(_failed(q, region, f"{type(e).__name__}: {e}"))
        if progress:
            r = out[-1]
            mark = "ERR " if r.error else "OK  "
            print(
                f"[{i:03}/{len(pending)}] {mark}{q!r:55} docs={len(r.docs):>3} found={r.found_all}",
                file=sys.stderr,
            )

    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Выдача Яндекса через Search API v2")
    p.add_argument("--query", required=True)
    p.add_argument("--region", type=int, default=RUSSIA)
    p.add_argument("--n", type=int, default=PAGE_SIZE,
                   help=f"глубина выдачи, не больше {MAX_DEPTH}; каждые {PAGE_SIZE} — "
                        "отдельное платное обращение")
    p.add_argument("--json", action="store_true", help="выдать JSON вместо таблицы")
    args = p.parse_args(argv)

    res = serp(args.query, region=args.region, n=args.n)
    if res.error:
        print(f"ОШИБКА: {res.error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"Запрос: {res.query!r}  регион {res.region}  найдено {res.found_all}")
    print(f"{'#':>3}  {'домен':<32} {'глуб':>4} {'hl':>3} {'стр':>4}  title")
    for d in res.docs:
        print(
            f"{d.position:>3}  {d.domain[:32]:<32} {d.url_depth:>4} "
            f"{d.title_hlwords:>3} {d.domain_doccount:>4}  {d.title[:60]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
