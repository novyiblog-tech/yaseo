#!/usr/bin/env python3
"""
Яндекс.Вебмастер: показы, клики и средняя позиция от владельца сайта.

Замер выдачи через Search API отвечает на вопрос «на каком месте мы стоим».
Вебмастер отвечает на другой: «сколько раз нас показали и сколько раз по нам
кликнули». Второго замер не видит принципиально, и подменить одно другим
нельзя: страница может стоять в первой десятке и не собирать ни одного клика —
позиции в порядке, кликов нет.

Две позиции — две разные метрики:
- `avg_show_position` — средняя по показам за окно, считает Яндекс;
- позиция из `yaseo.tracker` — медиана снимков выдачи, считаем мы.
Они не сверяются между собой и не заменяют друг друга; в отчётах стоят
рядом и названы источником.

CTR считается в одном месте — `ctr()`, из показов и кликов той же выгрузки.
Это арифметика над двумя измеренными числами, а не третий источник.

Публичный API:
- hosts() / resolve_host(domain)          — какие сайты открыты токену
- popular_queries(host_id, ...)           — запросы с показами и кликами
- query_pages(host_id, ...)               — какая страница собирает показы
- pull(project, days)                     — выгрузить и положить в базу
- sync_pool(project_id, ...)              — пул отслеживания по реальным запросам

Запуск как скрипт:
    yaseo webmaster --hosts
    yaseo webmaster --pull --project 1 --days 28
    yaseo webmaster --indexing --project 1
    yaseo webmaster --sync-pool --project 1            # показать
    yaseo webmaster --sync-pool --project 1 --apply    # записать
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from . import pool, storage
from .yandex_auth import YandexAuthError, api_json

BASE = "https://api.webmaster.yandex.net/v4"

#: Показатели, которые просим у `search-queries/popular`.
INDICATORS = ("TOTAL_SHOWS", "TOTAL_CLICKS", "AVG_SHOW_POSITION", "AVG_CLICK_POSITION")

#: Предел выдачи Вебмастера на страницу. Больше он не отдаёт, поэтому листаем.
PAGE = 500

#: Вебмастер отстаёт от сегодняшнего дня: данные за последние сутки-двое ещё
#: не сведены. Окно всегда заканчивается на этом отступе, иначе последний день
#: приходит недосчитанным и портит сумму.
LAG_DAYS = 2


@dataclass
class QueryStat:
    query: str
    query_id: str | None
    shows: int
    clicks: int
    avg_show_position: float | None
    avg_click_position: float | None

    @property
    def ctr(self) -> float | None:
        return ctr(self.clicks, self.shows)


@dataclass
class PageStat:
    """Запрос, страница, которая по нему выходит, и её показатели за окно."""
    query: str
    url: str | None
    shows: int
    clicks: int
    demand: int
    avg_position: float | None
    date_from: str | None
    date_to: str | None


def ctr(clicks: int | None, shows: int | None) -> float | None:
    """
    Кликабельность в процентах. Единственное место расчёта.

    Показов нет — нет и величины: делить не на что, а ноль здесь означал бы
    «показывали и не кликали», чего не было.
    """
    if not shows:
        return None
    return round(100.0 * (clicks or 0) / shows, 2)


def window(days: int = 28) -> tuple[str, str]:
    """Окно выгрузки с поправкой на отставание Вебмастера."""
    end = date.today() - timedelta(days=LAG_DAYS)
    return str(end - timedelta(days=max(days, 1) - 1)), str(end)


# ──────────────────────────── доступ к сайтам ────────────────────────────

def user_id() -> int:
    payload = api_json(f"{BASE}/user")
    uid = payload.get("user_id")
    if not uid:
        raise YandexAuthError(f"Вебмастер не вернул user_id: {str(payload)[:200]}")
    return int(uid)


def hosts() -> list[dict]:
    return list(api_json(f"{BASE}/user/{user_id()}/hosts/").get("hosts") or [])


def main_mirror_of(host: dict) -> str:
    """
    Адрес, по которому Вебмастер ведёт учёт этого сайта.

    У сайта бывает несколько подтверждённых зеркал, а данные лежат только у
    главного. Неглавное отвечает на запрос успешно и отдаёт пустой список:
    отказа нет, есть тишина, — а тишину человек читает как «нас не показывают».
    """
    mirror = host.get("main_mirror") or {}
    return mirror.get("host_id") or host.get("host_id") or ""


def resolve_host(domain: str) -> str | None:
    """
    host_id по домену. Вид `https:example.ru:443` собирается не по шаблону, а
    берётся из ответа: угаданный адрес дал бы 404 на ровном месте.

    У сайта бывает два подтверждённых зеркала — `http:example.ru:80` и
    `https:example.ru:443`. По неглавному `search-queries/popular` отвечает
    успешно и пусто, и пустой ответ неотличим от честного «показов нет».
    Поэтому домен разрешается в ГЛАВНОЕ зеркало, а не в первое совпавшее.
    """
    want = domain.strip().lower().removeprefix("www.")
    for h in hosts():
        url = (h.get("ascii_host_url") or "").lower()
        host = url.split("//")[-1].strip("/").removeprefix("www.")
        if host == want:
            return main_mirror_of(h) or None
    return None


def is_main_mirror(host_id: str) -> bool:
    """
    Ведёт ли Вебмастер учёт по этому адресу.

    Спрашивается у самого Вебмастера, а не выводится из вида строки: `https` в
    адресе главного зеркала не гарантирует, и наоборот. Хоста нет в списке
    токена — не главное: мерить по нему нельзя тем более.
    """
    for h in hosts():
        if h.get("host_id") == host_id:
            return not (h.get("main_mirror") or {}).get("host_id")
    return False


def _host_path(host_id: str) -> str:
    return urllib.parse.quote(host_id, safe="")


# ──────────────────────────── индексация ────────────────────────────
#
# Вопрос «сколько наших страниц вообще в поиске» идёт раньше позиций и
# показов: страница вне индекса не даёт ни позиции, ни показа, и все прочие
# числа по ней — числа ни о чём.

def in_search(host_id: str, days: int = 28) -> dict:
    """
    Сколько страниц сайта Яндекс держит в поиске — на последний день и месяц
    назад, чтобы видеть движение, а не одну точку.

    Ответ API — история событий добавления и удаления. Берём последнюю запись
    как «сейчас» и первую в окне как «было»: разница и есть то, что человек
    называет «сайт индексируется или выпадает».
    """
    начало, конец = window(days)
    путь = (f"{BASE}/user/{user_id()}/hosts/{_host_path(host_id)}"
            f"/search-urls/in-search/history/?date_from={начало}&date_to={конец}")
    данные = api_json(путь)
    история = список_истории(данные, "in_search_url_count")
    if not история:
        return {"есть данные": False,
                "почему": "Вебмастер не вернул историю страниц в поиске за окно"}
    сейчас, было = история[-1], история[0]
    return {
        "есть данные": True,
        "в поиске": сейчас["значение"],
        "на дату": сейчас["дата"],
        "было": было["значение"],
        "с даты": было["дата"],
        "изменение": сейчас["значение"] - было["значение"],
        "точек": len(история),
    }


def crawl_stats(host_id: str, days: int = 28) -> dict:
    """
    Что робот обошёл и что из обхода выбросил.

    Два числа рядом, потому что порознь они врут: «обошёл 120» без «выбросил
    40» читается как «120 страниц в поиске», а это разные предметы.
    """
    начало, конец = window(days)
    основа = f"{BASE}/user/{user_id()}/hosts/{_host_path(host_id)}"
    итог: dict = {"есть данные": False}
    try:
        данные = api_json(f"{основа}/indexing/history/?date_from={начало}"
                          f"&date_to={конец}&indexing_indicator=SEARCHABLE"
                          "&indexing_indicator=DOWNLOADED"
                          "&indexing_indicator=HTTP_2XX"
                          "&indexing_indicator=HTTP_4XX"
                          "&indexing_indicator=HTTP_5XX")
    except Exception as сбой:  # noqa: BLE001 — важен факт недоступности
        return {"есть данные": False, "почему": f"Вебмастер не ответил: {сбой}"}
    # Берётся ПОСЛЕДНЯЯ точка ряда, а не сумма и не итог за всё время: у
    # Вебмастера это история по дням, и складывать дни нельзя — одна и та же
    # страница считается в каждом. Отчёт обязан назвать дату, иначе число
    # «12 страниц с ошибкой» читается как «за всё время», а это «на тот день».
    показатели = данные.get("indicators") or {}
    итог["на дату"] = конец
    for имя, точки in показатели.items():
        ряд = [т for т in (точки or []) if т.get("value") is not None]
        if ряд:
            итог[_ПО_РУССКИ.get(имя, имя)] = ряд[-1]["value"]
            итог["есть данные"] = True
    if not итог["есть данные"]:
        итог["почему"] = "Вебмастер вернул историю обхода без единого значения"
    return итог


#: Имена показателей Вебмастера по-русски. Английский ключ в отчёте — это
#: машинный жаргон наружу.
_ПО_РУССКИ = {
    "SEARCHABLE": "в поиске",
    "DOWNLOADED": "робот скачал",
    "HTTP_2XX": "отвечают нормально",
    "HTTP_3XX": "переадресуют",
    "HTTP_4XX": "отвечают ошибкой 4xx",
    "HTTP_5XX": "отвечают ошибкой 5xx",
}


def список_истории(данные: dict, ключ: str) -> list[dict]:
    """Ряд «дата → значение» из ответа Вебмастера, каким бы полем он ни звался.

    Вебмастер возвращает историю то полем с именем показателя, то общим
    `history`. Разбор один на оба случая: два разбора одного ответа разъедутся
    в первый же день, когда API вернёт второй вид.
    """
    ряд = данные.get(ключ) or данные.get("history") or []
    out = []
    for точка in ряд:
        значение = точка.get("value")
        дата = (точка.get("date") or "")[:10]
        if значение is None or not дата:
            continue
        out.append({"дата": дата, "значение": int(значение)})
    return sorted(out, key=lambda т: т["дата"])


# ──────────────────────────── выгрузки ────────────────────────────

def popular_queries(host_id: str, date_from: str, date_to: str,
                    limit: int | None = None) -> list[QueryStat]:
    """Запросы, по которым сайт показывался, от самых показываемых."""
    out: list[QueryStat] = []
    offset = 0
    indicators = "&".join(f"query_indicator={i}" for i in INDICATORS)
    uid = user_id()
    while True:
        take = PAGE if limit is None else min(PAGE, limit - len(out))
        if take <= 0:
            break
        payload = api_json(
            f"{BASE}/user/{uid}/hosts/{_host_path(host_id)}/search-queries/popular/"
            f"?order_by=TOTAL_SHOWS&{indicators}"
            f"&date_from={date_from}&date_to={date_to}&offset={offset}&limit={take}"
        )
        batch = payload.get("queries") or []
        for q in batch:
            ind = q.get("indicators") or {}
            out.append(QueryStat(
                query=q.get("query_text") or "",
                query_id=q.get("query_id"),
                shows=int(ind.get("TOTAL_SHOWS") or 0),
                clicks=int(ind.get("TOTAL_CLICKS") or 0),
                avg_show_position=ind.get("AVG_SHOW_POSITION"),
                avg_click_position=ind.get("AVG_CLICK_POSITION"),
            ))
        total = int(payload.get("count") or 0)
        offset += len(batch)
        if not batch or offset >= total or (limit is not None and len(out) >= limit):
            break
    return out


def query_pages(host_id: str, limit: int | None = None) -> list[PageStat]:
    """
    Какая страница собирает показы по каждому запросу.

    Своё окно у этой выгрузки: Вебмастер отдаёт `query-analytics` только за
    последние две недели и период не принимает. Поэтому даты берутся из самого
    ответа, а не назначаются нами — иначе в базе стояло бы окно, которого
    не было.

    Средняя позиция сводится по дням с весом показов: день с одним показом и
    день с семьюдесятью не равны, а простое среднее уравняло бы их.
    """
    out: list[PageStat] = []
    offset = 0
    uid = user_id()
    while True:
        take = PAGE if limit is None else min(PAGE, limit - len(out))
        if take <= 0:
            break
        payload = api_json(
            f"{BASE}/user/{uid}/hosts/{_host_path(host_id)}/query-analytics/list",
            method="POST",
            body={"offset": offset, "limit": take,
                  "device_type_indicator": "ALL",
                  "text_indicator": "QUERY",
                  "order_by": {"indicator": "TOTAL_SHOWS", "order": "DESC"}},
        )
        batch = payload.get("text_indicator_to_statistics") or []
        for item in batch:
            by_field: dict[str, list[tuple[str, float]]] = {}
            for s in item.get("statistics") or []:
                by_field.setdefault(s.get("field") or "", []).append(
                    (s.get("date") or "", float(s.get("value") or 0))
                )
            shows_by_date = dict(by_field.get("IMPRESSIONS", []))
            shows = sum(shows_by_date.values())
            weighted = sum(v * shows_by_date.get(d, 0) for d, v in by_field.get("POSITION", []))
            dates = sorted(d for d in shows_by_date if d)
            out.append(PageStat(
                query=((item.get("text_indicator") or {}).get("value") or ""),
                url=((item.get("popular_complementary_indicator") or {}).get("value")),
                shows=int(shows),
                clicks=int(sum(v for _, v in by_field.get("CLICKS", []))),
                demand=int(sum(v for _, v in by_field.get("DEMAND", []))),
                avg_position=round(weighted / shows, 2) if shows else None,
                date_from=dates[0] if dates else None,
                date_to=dates[-1] if dates else None,
            ))
        total = int(payload.get("count") or 0)
        offset += len(batch)
        if not batch or offset >= total or (limit is not None and len(out) >= limit):
            break
    return out


# ──────────────────────────── выгрузка в базу ────────────────────────────

def pull(project_id: int = 1, days: int = 28, with_pages: bool = True) -> dict:
    """
    Снимает первичку по проекту и кладёт в базу. Возвращает, что снялось.

    Хост определяется один раз и запоминается у проекта: незачем ходить в
    чужой API за адресом на каждый отчёт.
    """
    storage.init_db()
    project = storage.get_project(project_id)
    if not project:
        raise SystemExit(f"проекта {project_id} нет")

    host = project.get("webmaster_host") or resolve_host(project["domain"])
    if not host:
        raise SystemExit(
            f"домен {project['domain']} не найден среди сайтов Вебмастера. "
            "Права подтверждаются на webmaster.yandex.ru, из кода это не делается."
        )
    # Сохранённый адрес проверяется, а не принимается на веру. Записанное один
    # раз неглавное зеркало держало бы проект немым навсегда: `pull` берёт
    # сохранённое значение раньше разрешения имени и выгружал бы с адреса, по
    # которому Вебмастер учёт не ведёт.
    if not is_main_mirror(host):
        главное = resolve_host(project["domain"])
        if главное and главное != host:
            print(f"{host} — не главное зеркало {project['domain']}: Вебмастер "
                  f"ведёт учёт по {главное}. Беру главное и перезаписываю "
                  f"привязку проекта.", file=sys.stderr)
            host = главное
    storage.set_project_yandex(project_id, webmaster_host=host)

    date_from, date_to = window(days)
    queries = popular_queries(host, date_from, date_to)
    saved = storage.save_search_queries(
        host, date_from, date_to,
        [asdict(q) for q in queries], project_id=project_id,
    )

    pages_saved = 0
    pages_window = None
    if with_pages:
        dated = [p for p in query_pages(host) if p.date_from and p.date_to]
        if dated:
            pages_window = (min(p.date_from for p in dated), max(p.date_to for p in dated))
            pages_saved = storage.save_search_query_pages(
                host, pages_window[0], pages_window[1],
                [{"query": p.query, "url": p.url, "shows": p.shows, "clicks": p.clicks,
                  "avg_position": p.avg_position, "demand": p.demand} for p in dated],
                project_id=project_id,
            )

    shows = sum(q.shows for q in queries)
    clicks = sum(q.clicks for q in queries)
    return {
        "project_id": project_id,
        "host_id": host,
        "date_from": date_from,
        "date_to": date_to,
        "queries": saved,
        "shows": shows,
        "clicks": clicks,
        "ctr": ctr(clicks, shows),
        "pages": pages_saved,
        "pages_window": pages_window,
        "zero_click_queries": sum(1 for q in queries if q.shows >= 10 and q.clicks == 0),
    }


# ──────────────────────────── пул отслеживания ────────────────────────────

#: Порог показов, ниже которого запрос в пул не берётся. Это проектное решение
#: про стоимость, а не измерение: каждый снятый запрос — платный запрос к Search
#: API, помноженный на число снимков. Порог называется в отчёте.
MIN_SHOWS = 10

#: Сколько запросов держать в пуле. Тоже про стоимость. Отсечённое перечисляется,
#: а не пропадает молча.
POOL_LIMIT = 60


def _is_junk(phrase: str) -> bool:
    """Справочный хвост или чужой продукт. Правила общие с `yaseo.pool`."""
    vendor = pool.VENDOR.search(phrase) if pool.VENDOR is not None else None
    return bool(pool.NOISE.search(phrase) or vendor)


def _article_targets(project_id: int) -> dict[str, str]:
    """
    Запросы, под которые у нас есть живая страница, и адрес этой страницы.

    Берутся и целевой запрос, и дополнительные: статья заявлена под несколько
    формулировок, и мерить надо все.
    """
    out: dict[str, str] = {}
    for art in storage.list_articles(project_id):
        url = art.get("url") or ""
        for q in [art.get("target_query")] + list(art.get("extra_queries") or []):
            if q and q.strip():
                out.setdefault(q.strip().lower(), url)
    return out


def sync_pool(project_id: int = 1, min_shows: int = MIN_SHOWS, limit: int = POOL_LIMIT,
              apply: bool = False, retire_too: bool = False) -> dict:
    """
    Приводит пул отслеживания к тому, что реально показывает Яндекс.

    Повод: формулировки, придуманные при сборе ядра, часто не совпадают с
    теми, по которым реально идут показы, — мерить надо вторые.

    Запрос без показов снимается с отслеживания не всегда. Ноль показов не
    означает отсутствие спроса — он означает, что нас по этой фразе не
    показывают, и для целевой фразы это ровно тот случай, который надо
    продолжать мерить.
    Под защитой две группы:

    - фразы с вердиктом «ядро» — заявленные цели проекта;
    - целевые запросы живых статей и страниц. Окно Вебмастера смотрит назад на
      месяц, а страница, выложенная на прошлой неделе, показов в нём ещё не
      набрала. Снять её с замера значит перестать мерить ровно то, ради чего
      её и делали.

    Мусор в пул не берётся: справочные и развлекательные хвосты, а также чужие
    продукты отсеиваются теми же правилами, что и при расширении через Wordstat
    (`yaseo.pool`). Определение мусора в пакете одно, второго здесь нет.

    Снятие обратимо: `untrack` гасит признак активности, накопленные замеры
    остаются на месте.
    """
    storage.init_db()
    project = storage.get_project(project_id)
    if not project:
        raise SystemExit(f"проекта {project_id} нет")
    host = project.get("webmaster_host") or resolve_host(project["domain"])
    if not host:
        raise SystemExit(f"домен {project['domain']} не найден среди сайтов Вебмастера")

    region = int(project["region"])
    real = storage.search_queries_latest(host)
    if not real:
        raise SystemExit(
            "выгрузки Вебмастера ещё нет — сначала "
            f"yaseo webmaster --pull --project {project_id}"
        )

    above = [r for r in real if (r["shows"] or 0) >= min_shows]
    junk = [r for r in above if _is_junk(r["query"])]
    clean = [r for r in above if not _is_junk(r["query"])]
    taken = clean[:limit]
    cut_by_limit = clean[limit:]
    cut_by_shows = [r for r in real if (r["shows"] or 0) < min_shows]

    wanted = {r["query"].strip().lower(): r for r in taken}
    tracked_map = {q.strip().lower(): q for q in storage.project_queries(project_id)}
    core_targets = {
        c["phrase"].strip().lower()
        for c in storage.get_core(project_id, verdict="ядро")
    }
    article_targets = _article_targets(project_id)

    keep, add, retire = [], [], []
    for key, row in wanted.items():
        entry = {"query": row["query"], "shows": row["shows"], "clicks": row["clicks"]}
        if key in tracked_map:
            keep.append({**entry, "reason": "показы есть, уже отслеживается"})
        else:
            add.append({**entry,
                        "reason": f"показов {row['shows']} — реальный запрос, а мы его не мерили"})
    for key, original in tracked_map.items():
        if key in wanted:
            continue
        held = {"query": original, "shows": 0, "clicks": 0}
        if key in article_targets:
            keep.append({**held, "reason": f"целевой запрос страницы "
                                           f"{article_targets[key]} — её и мерим"})
        elif key in core_targets:
            keep.append({**held, "reason": "цель ядра: показов нет, но ноль показов — это наша "
                                           "невидимость, а не отсутствие спроса"})
        else:
            retire.append({**held,
                           "reason": "показов за окно нет, целью ни в ядре, ни у страниц не заявлен"})

    # Добавление и снятие разведены намеренно. Добавить запрос — значит начать
    # мерить то, что уже показывается: прибавка знания. Снять — перестать мерить,
    # и если страница под фразу существует, но в реестре статей её нет, инструмент
    # об этом не знает и предложит снять живую цель: свежая страница показов за
    # окно ещё не набрала. Поэтому снятие делается отдельным ключом и глазами.
    applied = {"tracked": 0, "untracked": 0}
    if apply:
        for row in add:
            storage.track_for_project(project_id, row["query"], region)
            applied["tracked"] += 1
        if retire_too:
            for row in retire:
                storage.untrack(row["query"], region)
                applied["untracked"] += 1

    return {
        "project_id": project_id,
        "host_id": host,
        "region": region,
        "window": (real[0]["date_from"], real[0]["date_to"]),
        "webmaster_queries": len(real),
        "min_shows": min_shows,
        "limit": limit,
        "add": add,
        "keep": keep,
        "retire": retire,
        "dropped_by_shows": len(cut_by_shows),
        "dropped_as_junk": [r["query"] for r in junk],
        "dropped_by_limit": [r["query"] for r in cut_by_limit],
        "applied": applied if apply else None,
        "pool_after": len(storage.project_queries(project_id)),
    }


# ──────────────────────────── CLI ────────────────────────────

def _print_pull(res: dict) -> None:
    print(f"\nСайт {res['host_id']} · окно {res['date_from']} … {res['date_to']}")
    print(f"  запросов с показами : {res['queries']}")
    print(f"  показов             : {res['shows']}")
    print(f"  кликов              : {res['clicks']}")
    print(f"  CTR                 : {res['ctr'] if res['ctr'] is not None else 'нет данных'}%")
    if res["pages"]:
        w = res["pages_window"]
        print(f"  страниц по запросам : {res['pages']} (своё окно {w[0]} … {w[1]})")
    print(f"\n  запросов с показами от 10 и нулём кликов: {res['zero_click_queries']}")
    print("  это не проблема позиций, а проблема сниппета — заголовка и описания.")


def _print_sync(res: dict) -> None:
    w = res["window"]
    print(f"\nСайт {res['host_id']} · окно Вебмастера {w[0]} … {w[1]}")
    print(f"Запросов у Яндекса: {res['webmaster_queries']}. "
          f"Порог показов {res['min_shows']}, предел пула {res['limit']}.")

    def block(title: str, rows: list[dict]) -> None:
        print(f"\n  {title} ({len(rows)}):")
        for r in rows[:60]:
            print(f"    {r['shows']:>6} показов {r['clicks']:>4} кликов  "
                  f"{r['query'][:46]:<46} {r['reason']}")
        if not rows:
            print("    —")

    block("Добавить", res["add"])
    block("Оставить", res["keep"])
    block("Снять с отслеживания", res["retire"])

    print(f"\n  Отсечено порогом показов: {res['dropped_by_shows']}")
    if res["dropped_as_junk"]:
        print(f"  Отсечено как справочное или чужой продукт: {len(res['dropped_as_junk'])} — "
              + ", ".join(res["dropped_as_junk"][:8])
              + ("…" if len(res["dropped_as_junk"]) > 8 else ""))
    if res["dropped_by_limit"]:
        print(f"  Отсечено пределом пула: {len(res['dropped_by_limit'])} — "
              + ", ".join(res["dropped_by_limit"][:8])
              + ("…" if len(res["dropped_by_limit"]) > 8 else ""))
    if res["applied"] is None:
        print("\n  Показ без записи. Записать: --apply (добавит), "
              "--apply --retire (ещё и снимет).")
        return
    print(f"\n  Поставлено на отслеживание: {res['applied']['tracked']}, "
          f"снято: {res['applied']['untracked']}. Пул теперь: {res['pool_after']}.")
    if not res["applied"]["untracked"] and res["retire"]:
        print(f"  Снятие не выполнялось: {len(res['retire'])} запросов ждут ключа --retire. "
              "Инструмент видит только страницы из реестра статей — свежая посадочная, "
              "которой в реестре нет, выглядит как заброшенный запрос.")
    if res["applied"]["untracked"]:
        print("  Снятие обратимо: замеры остались, погашен только признак активности.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Яндекс.Вебмастер: показы, клики, позиции")
    p.add_argument("--hosts", action="store_true", help="какие сайты открыты токену")
    p.add_argument("--pull", action="store_true", help="выгрузить запросы и страницы в базу")
    p.add_argument("--indexing", action="store_true",
                   help="сколько страниц в поиске и что обошёл робот")
    p.add_argument("--sync-pool", action="store_true", dest="sync_pool",
                   help="привести пул отслеживания к реальным запросам")
    p.add_argument("--top", type=int, default=0, help="показать N запросов последнего окна")
    p.add_argument("--project", type=int, default=1)
    p.add_argument("--days", type=int, default=28)
    p.add_argument("--min-shows", type=int, default=MIN_SHOWS, dest="min_shows")
    p.add_argument("--limit", type=int, default=POOL_LIMIT)
    p.add_argument("--apply", action="store_true", help="записать, а не только показать")
    p.add_argument("--retire", action="store_true",
                   help="вместе с --apply: ещё и снять с отслеживания лишнее")
    args = p.parse_args(argv)

    if args.hosts:
        for h in hosts():
            mark = "подтверждён" if h.get("verified") else "НЕ подтверждён"
            print(f"  {h.get('host_id'):<32} {h.get('ascii_host_url'):<32} {mark}")
        return 0

    if args.pull:
        _print_pull(pull(args.project, days=args.days))
        return 0

    if args.indexing:
        project = storage.get_project(args.project) or {}
        host = project.get("webmaster_host") or resolve_host(project.get("domain", ""))
        if not host:
            print("У проекта нет привязки к Вебмастеру — сначала --pull", file=sys.stderr)
            return 1
        for title, data in (("Страницы в поиске", in_search(host, args.days)),
                            ("Обход робота", crawl_stats(host, args.days))):
            print(f"\n{title}:")
            for k, v in data.items():
                print(f"  {k}: {v}")
        return 0

    if args.sync_pool:
        _print_sync(sync_pool(args.project, min_shows=args.min_shows, limit=args.limit,
                              apply=args.apply, retire_too=args.retire))
        return 0

    if args.top:
        project = storage.get_project(args.project)
        host = (project or {}).get("webmaster_host")
        if not host:
            print("У проекта нет привязки к Вебмастеру — сначала --pull")
            return 1
        print(f"  {'показы':>7} {'клики':>6} {'CTR':>6} {'ср.поз':>7}  запрос")
        for r in storage.search_queries_latest(host, limit=args.top):
            c = ctr(r["clicks"], r["shows"])
            pos = r["avg_show_position"]
            print(f"  {r['shows']:>7} {r['clicks']:>6} "
                  f"{(f'{c}%' if c is not None else '—'):>6} "
                  f"{(f'{pos:.1f}' if pos is not None else '—'):>7}  {r['query']}")
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
