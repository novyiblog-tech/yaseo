#!/usr/bin/env python3
"""
Трекер позиций yaseo.

Связывает выдачу, конкурентность и хранилище: снимает топ по отслеживаемым
запросам, складывает снимок, считает конкурентность и фиксирует позиции
наших доменов и конкурентов.

Позиция без истории — это мнение. Позиция с историей — факт с датой.

Публичный API:
- run_tracking(domains, queries, region, n)  -> TrackingRun
- report(domain, region)                     -> list[dict]
- movers(domain, region, limit)              -> кто вырос и кто просел

Запуск как скрипт:
    yaseo track --add "пластиковые окна"
    yaseo track --run --dry          # смета, ничего не тратит
    yaseo track --run                # по всем активным проектам
    yaseo track --run    --domain example.ru --also rival.ru
    yaseo track --report --domain example.ru
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config
from . import estimate as smeta
from . import storage
from .competition import score_serp
from .yandex_serp import RUSSIA, serp

#: Выдача Яндекса нестабильна: пять одинаковых запросов подряд могут дать
#: позиции 11, 6, 5, 6, 5 — одиночный снимок позицией не является. Поэтому замер
#: складывается из нескольких снимков, а в историю идёт медиана.
DEFAULT_REPEATS = config.TRACKING_DEFAULTS["repeats"]
DEFAULT_MAX_CALLS = config.TRACKING_DEFAULTS["max_calls"]
DEFAULT_COLD_REPEATS = config.TRACKING_DEFAULTS["repeats_cold"]
DEFAULT_MIN_INTERVAL_DAYS = config.TRACKING_DEFAULTS["min_interval_days"]


#: Отсутствие в снятом топе при расчёте медианы. Больше любой реальной позиции.
BEYOND = 10_000


# ──────────────────────────── настройки автозамера ────────────────────────────


def settings() -> dict:
    """Настройки трекера (`config.tracking_settings`): умолчания и файл поверх."""
    return config.tracking_settings()


def _int_setting(key: str, minimum: int, fallback: int) -> int:
    try:
        return max(minimum, int(settings().get(key)))
    except (TypeError, ValueError):
        return fallback


def max_calls() -> int:
    """
    Потолок обращений к Search API за один прогон.

    Старшинство: переменная окружения `YASEO_TRACKING_MAX_CALLS` → файл
    настроек → умолчание. Окружение сильнее файла, чтобы разовый большой прогон
    можно было разрешить, не правя настройку насовсем.
    """
    raw = config.env_int("YASEO_TRACKING_MAX_CALLS")
    if raw is not None:
        return max(1, raw)
    return _int_setting("max_calls", 1, DEFAULT_MAX_CALLS)


def repeats_setting() -> int:
    return _int_setting("repeats", 1, DEFAULT_REPEATS)


def repeats_hot() -> int:
    """Снимков по фразе, которая была в снятом топе. Медиану есть из чего брать."""
    return _int_setting("repeats_hot", 1, repeats_setting())


def repeats_cold() -> int:
    """
    Снимков по фразе, которой в снятом топе не было.

    Три снимка подряд отвечают по ней одно и то же «нет в топе», и медиана из
    трёх пустот — та же пустота. В типичном пуле таких фраз большинство, и
    три снимка на каждую тратили бы основную часть бюджета впустую.
    """
    return _int_setting("repeats_cold", 1, DEFAULT_COLD_REPEATS)


def min_interval_days() -> int:
    """
    Сколько суток между платными прогонами по одному домену. 0 — срока нет.

    Старшинство: `YASEO_TRACKING_MIN_INTERVAL_DAYS` → файл → умолчание (13).
    Срок держит трекер, а не расписание: cron и launchd умеют «каждый
    понедельник», но не «через две недели от прошлого замера».
    """
    raw = config.env_int("YASEO_TRACKING_MIN_INTERVAL_DAYS")
    if raw is not None:
        return max(0, raw)
    return _int_setting("min_interval_days", 0, DEFAULT_MIN_INTERVAL_DAYS)


def competitors(domain: str) -> list[str]:
    """Соперники, чьи позиции снимаются рядом с нашими по тому же ядру."""
    table = settings().get("competitors") or {}
    if not isinstance(table, dict):
        return []
    dom = config.normalize_domain(domain)
    return [str(d).strip() for d in (table.get(dom) or []) if str(d).strip()]


def _queries_of_project(pid: int, region: int) -> tuple[list[str], int]:
    """
    Фразы проекта и сколько из них досталось ему по правилу «фразы без проекта».

    Возвращает (фразы, сколько_без_проекта). Фразы, заведённые без проекта
    (`yaseo track --add`), по умолчанию ничьи. Настройка `orphan_project`
    называет проект, которому их отдать; второе число печатается в журнал,
    потому что это назначение, а не измерение.
    """
    orphan_owner = settings().get("orphan_project")
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT query, project_id FROM tracked_queries "
            "WHERE active=1 AND region=? ORDER BY query", (region,)
        ).fetchall()
    own = [r["query"] for r in rows if r["project_id"] == pid]
    orphans = [r["query"] for r in rows if r["project_id"] is None]
    try:
        owner = int(orphan_owner) if orphan_owner is not None else None
    except (TypeError, ValueError):
        owner = None
    if owner == pid:
        return own + orphans, len(orphans)
    return own, 0


def auto_targets(region: int = RUSSIA) -> list[dict]:
    """
    Что снимает автозамер: по одной цели на активный проект.

    Домен берётся из таблицы `projects` — там же, где его знает весь остальной
    пакет. Состав замера, зашитый в аргументы расписания, становится вторым
    невидимым местом правды, и новый проект в замер не попадает.

    Проект без единой своей фразы в цель не попадает: снимать по нему чужое
    ядро значило бы потратить деньги на чужой спрос и записать результат ему.
    """
    out = []
    for p in storage.list_projects(active_only=True):
        pid = int(p["id"])
        if int(p.get("region") or RUSSIA) != region:
            continue
        queries, orphans = _queries_of_project(pid, region)
        out.append({
            "project_id": pid,
            "project": p["name"],
            "domain": p["domain"],
            "queries": queries,
            "унаследовано_без_проекта": orphans,
            "competitors": competitors(p["domain"]),
        })
    return out


def last_measured(domain: str, region: int = RUSSIA) -> str | None:
    """
    Когда домен мерили в последний раз. Берётся из `positions`, а не из `runs`:
    прогон мог остановиться на потолке и денег не потратить, а нас интересует
    именно свежесть данных по домену.
    """
    dom = (domain or "").lower().removeprefix("www.")
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT MAX(checked_at) AS last FROM positions WHERE domain=? AND region=?",
            (dom, region),
        ).fetchone()
    return row["last"] if row and row["last"] else None


def due(domain: str, region: int = RUSSIA, interval: int | None = None) -> dict:
    """
    Пора ли платить за замер этого домена.

    Гейт расписания: держит и `--run` из терминала (путь, которым ходит
    cron/launchd и по которому никто не нажимал), и MCP-инструмент
    `run_tracking` (через `run_tracking(min_interval=...)` — у агента,
    зовущего инструмент повторно, тоже должен быть тормоз, не только у
    расписания). Разовый прогон из терминала снимает срок ключом `--сейчас`,
    вызов MCP — параметром `now=true`.

    Возвращает разбор словами, а не голое «нет»: молчаливый пропуск читается
    как поломка расписания, и его начинают чинить вместо того, чтобы понять.
    """
    days = min_interval_days() if interval is None else int(interval)
    last = last_measured(domain, region)
    if not days:
        return {"пора": True, "почему": "срок не задан — снимаем каждый прогон",
                "прошлый": last, "следующий": None, "прошло суток": None,
                "интервал": 0}
    if not last:
        return {"пора": True, "почему": "замеров по домену ещё не было",
                "прошлый": None, "следующий": None, "прошло суток": None,
                "интервал": days}

    try:
        prev = datetime.fromisoformat(last)
    except ValueError:
        return {"пора": True, "почему": f"дата прошлого замера не читается ({last!r})",
                "прошлый": last, "следующий": None, "прошло суток": None,
                "интервал": days}
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone.utc)

    passed = (datetime.now(timezone.utc) - prev).total_seconds() / 86400
    nxt = prev + timedelta(days=days)
    return {
        "пора": passed >= days,
        "почему": None,
        "прошлый": prev.astimezone().strftime("%d.%m.%Y %H:%M"),
        "следующий": nxt.astimezone().strftime("%d.%m.%Y %H:%M"),
        "прошло суток": round(passed, 1),
        "интервал": days,
    }


def plan_repeats(queries: list[str], domain: str, region: int = RUSSIA,
                 hot: int | None = None, cold: int | None = None,
                 n: int = 10) -> dict:
    """
    Сколько снимков на каждую фразу — и разбор по группам для сметы.

    Одна функция на смету и на прогон намеренно: разойдись они, и смета называла
    бы одну цену, а списывалась другая. Одна метрика — одно место.

    Горячая — была в снятом топе в прошлый замер, либо замеров по ней не было
    вовсе: новую фразу надо разрешить честно, а не объявить холодной авансом.
    Холодная — в прошлый замер в топ не попала.

    `n` — глубина снимка (topN). Обращений к Search API на один снимок больше
    одного, если глубина больше 10 (`estimate.pages_for_depth`): топ-10 — одно
    обращение, топ-30 — три. Поле «обращений» здесь — это именно обращения к
    API, а не число снимков; при умолчании n=10 страница одна и числа не
    расходятся, но `--n 30` без этого умножения обошёлся бы втрое дороже
    показанной сметы.
    """
    h = repeats_hot() if hot is None else max(1, int(hot))
    c = repeats_cold() if cold is None else max(1, int(cold))

    # Повтор фразы в списке: план складывается в словарь по фразе и считает её
    # один раз, а прогон, идущий по исходному списку, снял бы её дважды —
    # смета и трата разошлись бы.
    #
    # Повтора не должно быть и в самом прогоне: снимать одну фразу дважды —
    # платить дважды за один ответ. Поэтому список схлопывается здесь, один раз,
    # тем же ключом, которым ищется история (`strip().lower()`), и возвращается
    # наружу (`фразы`): прогон ходит по нему же, и разойтись им больше негде.
    фразы: list[str] = []
    видели: set[str] = set()
    for q in queries:
        key = q.strip().lower()
        if not key or key in видели:
            continue
        видели.add(key)
        фразы.append(q)

    known = {r["query"].strip().lower(): r["position"]
             for r in storage.latest_positions(domain, region=region, queries=фразы)}

    plan: dict[str, int] = {}
    hot_q: list[str] = []
    cold_q: list[str] = []
    new_q: list[str] = []
    for q in фразы:
        key = q.strip().lower()
        if key not in known:
            new_q.append(q)
            plan[q] = h
        elif known[key] is None:
            cold_q.append(q)
            plan[q] = c
        else:
            hot_q.append(q)
            plan[q] = h

    snapshots = sum(plan.values())
    pages = smeta.pages_for_depth(n)
    calls = snapshots * pages
    parts = []
    if hot_q or new_q:
        n_hot = len(hot_q) + len(new_q)
        tail = f" (из них новых {len(new_q)})" if new_q else ""
        parts.append(f"{n_hot} {smeta.plural(n_hot, 'горячая', 'горячие', 'горячих')} × "
                     f"{h} {smeta.plural(h, 'снимок', 'снимка', 'снимков')}{tail}")
    if cold_q:
        n_cold = len(cold_q)
        parts.append(f"{n_cold} {smeta.plural(n_cold, 'холодная', 'холодные', 'холодных')} × "
                     f"{c} {smeta.plural(c, 'снимок', 'снимка', 'снимков')}")

    из_чего = " + ".join(parts)
    if pages > 1:
        # Глубина больше топ-10: снимок стоит несколько обращений, и это
        # обязано быть видно в разборе, а не только в итоговом числе.
        из_чего = из_чего or (
            f"{len(фразы)} {smeta.plural(len(фразы), 'фраза', 'фразы', 'фраз')}"
        )
        из_чего += (
            f" × {pages} {smeta.plural(pages, 'страница', 'страницы', 'страниц')} "
            f"(глубина топ-{n})"
        )
    # Голое «= N» без слова «обращений» — только когда итог не очевиден из
    # самой записи (несколько групп или несколько страниц на снимок):
    # вызывающий сам допишет «обращений к …» после этого числа, повторять
    # слово здесь не надо — иначе получилось бы «= 9 обращений = 9 обращений
    # к Yandex Search API».
    if len(parts) > 1 or pages > 1:
        из_чего += f" = {calls}"

    return {
        "план": plan,
        # Список, по которому посчитаны и деньги, и группы. Прогон обязан идти
        # по нему же: считать по одному списку, а снимать по другому — это и
        # есть расхождение сметы с тратой.
        "фразы": фразы,
        # Обращений к Search API — снимков × страниц на снимок (см. docstring).
        "обращений": calls,
        "снимков": snapshots,
        "страниц_на_снимок": pages,
        "горячих": len(hot_q) + len(new_q),
        "холодных": len(cold_q),
        "новых": len(new_q),
        "снимков горячей": h,
        "снимков холодной": c,
        "из_чего": из_чего,
        "ровно": len(фразы) * h * pages,
    }


def run_estimate(target: dict, repeats: int | None = None,
                 cap: int | None = None, n: int = 10) -> dict:
    """
    Смета одной цели автозамера — той же функцией, что и у всех платных вызовов.

    Соперники обращений не добавляют: их позиции читаются из той же выдачи,
    которую мы уже оплатили за свой домен.

    Количество считает `plan_repeats`, а не умножение «фразы × снимки»: снимков
    на фразу два разных числа, и смета обязана назвать обе группы отдельно —
    иначе она покажет цену, которой не будет. `n` — глубина снимка: при
    n > 10 `plan_repeats` умножает ещё и на число страниц.
    """
    cap = int(cap if cap is not None else max_calls())
    phrases = len(target["queries"])
    rivals = target.get("competitors") or []
    pl = plan_repeats(target["queries"], target["domain"], region=RUSSIA,
                      hot=repeats, n=n)
    known = storage.latest_positions(target["domain"], region=RUSSIA,
                                     queries=target["queries"])
    накоплено = (f"замеров по {target['domain']} нет — пересниматься нечему"
                 if not known else
                 f"по {target['domain']} уже есть история по {len(known)} фразам; "
                 "прогон её не заменяет, а дописывает")

    notes = []
    if rivals:
        notes.append("Рядом снимаются соперники: " + ", ".join(rivals)
                     + " — из той же выдачи, без доплаты")
    if pl["холодных"]:
        notes.append(
            f"Холодные — те, кого в прошлый замер не было в снятом топе: по ним "
            f"{pl['снимков холодной']} {smeta.plural(pl['снимков холодной'], 'снимок', 'снимка', 'снимков')}, "
            f"а не {pl['снимков горячей']}, потому что медиану разрешать не из чего. "
            f"Тремя снимками по всем вышло бы {pl['ровно']} обращений")
    return smeta.estimate(
        "search-api", pl["обращений"],
        project=target["project"], domain=target["domain"],
        из_чего=pl["из_чего"] or f"{phrases} {smeta.plural(phrases, 'фраза', 'фразы', 'фраз')}",
        накоплено=накоплено, потолок=cap,
        примечание=" · ".join(notes) or None,
    )


def _median(values: list) -> int | None:
    """
    Медиана позиций. Отсутствие в топе — полноценное значение, а не пропуск.

    Если выбросить None, замер [5, None, None] даст медиану 5, хотя в двух
    снимках из трёх нас в топе не было. Это оптимистическое враньё, поэтому
    отсутствие участвует в расчёте и может само стать медианой.

    Нечётная длина — обычная медиана, средний элемент по счёту.

    Чётная длина — НИЖНЯЯ медиана (`ranked[mid-1]`), а не среднее двух
    центральных элементов. Середины между двумя реальными позициями никто не
    снимал: [3, 7] обычной медианой стал бы 5 — числом, которого нет ни в
    одном снимке. Нижняя медиана берёт вместо этого фактически увиденное
    значение (3) — то же самое место, где встал бы `statistics.median_low`.

    Особый случай — когда один из двух центральных элементов сам «нет в
    топе» (например [5, None] → ranked [5, BEYOND]). Нижняя медиана взяла бы
    5 и заявила бы уверенную позицию там, где ровно половина снимков её не
    нашла вовсе. Раньше здесь считалось арифметическое среднее 5 и BEYOND —
    то есть 5002, число без какого-либо смысла (BEYOND не место в выдаче, а
    флаг отсутствия, и его нельзя складывать и делить). Обе версии — 5 и
    5002 — одинаково лгут об уверенности, которой нет. Правильный ответ
    здесь один: если верхний из двух центральных элементов — «нет в топе»,
    то и медиана — «нет в топе», консервативно и объяснимо: выборка
    поделилась ровно пополам между «нашли» и «не нашли», перевес отдаётся
    отсутствию, а не удаче одного снимка.
    """
    if not values:
        return None
    ranked = sorted(BEYOND if v is None else v for v in values)
    n = len(ranked)
    mid = n // 2
    if n % 2:
        med = ranked[mid]
    else:
        lo, hi = ranked[mid - 1], ranked[mid]
        # Если хотя бы один из двух центральных элементов «нет в топе» (а
        # `hi >= lo`, значит, это всегда именно `hi`), медиана тоже «нет в
        # топе» — не нижний реальный сосед и не выдуманное среднее.
        med = BEYOND if hi >= BEYOND else lo
    return None if med >= BEYOND else med


def our_position(samples: list | None = None, single: int | None = None,
                 snapshots: int | None = None) -> dict:
    """
    «Наша позиция» — одно выражение и подпись к нему, которая не врёт о способе.

    Позиция статьи (`article_checks.position`) берётся из одного снимка, а
    позиция трекера — медиана нескольких. Подпись, объясняющая число не тем
    способом, которым его получили, вводит в заблуждение.

    Поэтому способ выводится из того, что пришло: есть выборки — медиана и
    сколько снимков; пришло одно значение — так и написано, «один снимок
    выдачи». Поле «как» печатается рядом с числом.
    """
    if samples:
        got = [s for s in samples if s is not None]
        n = snapshots or len(samples)
        return {
            "позиция": _median(samples),
            "как": f"медиана {n} {smeta.plural(n, 'снимка', 'снимков', 'снимков')}",
            "снимков": n,
            "найдено в": len(got),
        }
    return {
        "позиция": single,
        "как": "один снимок выдачи",
        "снимков": 1 if single is not None else 0,
        "найдено в": 1 if single is not None else 0,
    }


def _domain_position(result, domain: str) -> int | None:
    """Органическая позиция домена в снимке. Блоки Яндекса не считаются."""
    target = domain.lower().removeprefix("www.")
    for d in result.docs:
        if getattr(d, "is_wizard", False):
            continue
        if (d.domain or "").lower().removeprefix("www.") == target:
            return getattr(d, "organic_position", None) or d.position
    return None


def measure(query: str, domain: str, region: int = RUSSIA, n: int = 10,
            repeats: int = DEFAULT_REPEATS) -> dict:
    """
    Замер позиции по нескольким снимкам. Возвращает медиану, разброс и сами снимки.
    Разброс — это не шум, который надо прятать: он показывает, насколько позиции можно верить.
    """
    results = [serp(query, region=region, n=n) for _ in range(max(1, repeats))]
    ok = [r for r in results if not r.error]
    samples = [_domain_position(r, domain) for r in ok]
    found = [s for s in samples if s is not None]

    return {
        "query": query,
        "domain": domain,
        "median": _median(samples),
        "min": min(found) if found else None,
        "max": max(found) if found else None,
        "samples": samples,
        "found_in": len(found),
        "snapshots": len(ok),
        "failed": len(results) - len(ok),
        #: Причины неудачных снимков — дословно от источника.
        "errors": [r.error for r in results if r.error],
        "results": ok,
    }


@dataclass
class TrackingRun:
    run_id: int
    region: int
    started_at: str
    queries: int
    ok: int
    failed: list[str] = field(default_factory=list)
    positions_recorded: int = 0
    #: Сколько платных обращений прогон собирался сделать (фразы × снимки).
    #: Записей позиций (`positions_recorded`) всегда больше или меньше: они
    #: считают домены, а не запросы к API, и путать их деньгами нельзя.
    calls_planned: int = 0
    #: Заполнено — прогон остановлен ДО первой траты, и здесь сказано почему.
    stopped: str | None = None
    #: Почему не снялись запросы из `failed`: запрос → первая причина от источника.
    reasons: dict[str, str] = field(default_factory=dict)


def run_tracking(
    domains: list[str],
    queries: list[str] | None = None,
    region: int = RUSSIA,
    n: int = 10,
    note: str | None = None,
    progress: bool = True,
    repeats: int = DEFAULT_REPEATS,
    cap: int | None = None,
    min_interval: int | None = None,
    now: bool = False,
) -> TrackingRun:
    """
    Снимает выдачу по запросам, сохраняет снимки, конкурентность и позиции.
    Запросы не заданы — берутся отслеживаемые из базы.

    Каждый запрос снимается `repeats` раз, в историю идёт медиана: выдача Яндекса
    прыгает между одинаковыми запросами, и одиночный снимок дал бы ложную динамику.

    `cap` — потолок платных обращений за прогон. Прогон, который в него не
    укладывается, останавливается ДО первого запроса и говорит об этом в
    журнал. Так пул не может вырасти незаметно и потратить в разы больше без
    чьего-либо согласия. `cap=0` снимает потолок — это осознанное решение
    человека, а не умолчание.

    `min_interval` — гейт срока между прогонами по домену `domains[0]`, тот же,
    что держит `--run` в CLI (`due()`), но встроенный в саму функцию: прогон
    раньше срока останавливается ДО первого запроса той же причиной, что и
    потолок (`TrackingRun.stopped`). По умолчанию `None` — гейта здесь нет:
    CLI держит его сам, отдельно, ДО вызова этой функции (см. `main()`), и
    трогать поведение существующих вызывающих не нужно. Гейт включает вызывающий
    код, которому нужен он именно здесь — например, MCP-инструмент `run_tracking`,
    у которого своего гейта нет. `now=True` снимает гейт на этот один раз.
    """
    storage.init_db()
    qs = queries if queries is not None else storage.tracked(region)
    started = datetime.now(timezone.utc).isoformat()

    if not qs or not domains:
        return TrackingRun(run_id=-1, region=region, started_at=started, queries=len(qs or []), ok=0)

    primary = domains[0]

    # Снимков на фразу два разных числа, и считает их одна функция на смету и
    # на прогон: разойдись они — смета назвала бы одну цену, а списалась бы другая.
    pl = plan_repeats(qs, primary, region=region, hot=repeats, n=n)
    # Прогон идёт по тому же списку, по которому посчитана смета (`pl["фразы"]`),
    # а не по исходному: порядок тот же, схлопнуты только повторы.
    qs = pl["фразы"]
    per_query = pl["план"]
    planned = pl["обращений"]

    if min_interval is not None and not now:
        d = due(primary, region, interval=min_interval)
        if not d["пора"]:
            why = (
                f"пропуск без трат: прошлый замер {d['прошлый']} "
                f"({d['прошло суток']} сут назад при сроке {d['интервал']} сут), "
                f"следующий не раньше {d['следующий']}. Этот прогон обошёлся бы в "
                f"{planned} {smeta.plural(planned, 'обращение', 'обращения', 'обращений')} "
                f"к Yandex Search API. Ничего не потрачено. Снять срок на этот раз — "
                f"параметр now=true, или совсем — YASEO_TRACKING_MIN_INTERVAL_DAYS=0"
            )
            print(why, file=sys.stderr)
            return TrackingRun(run_id=-1, region=region, started_at=started,
                               queries=len(qs), ok=0, calls_planned=planned, stopped=why)

    limit = max_calls() if cap is None else int(cap)
    if limit and planned > limit:
        why = (f"прогон остановлен до первого запроса: {pl['из_чего']} = {planned} "
               f"обращений к Yandex Search API при потолке {limit}. "
               f"Ничего не потрачено. Поднять потолок — "
               f"«max_calls» в {config.tracking_file()} или "
               f"YASEO_TRACKING_MAX_CALLS на один прогон")
        print(why, file=sys.stderr)
        return TrackingRun(run_id=-1, region=region, started_at=started,
                           queries=len(qs), ok=0, calls_planned=planned, stopped=why)

    run_id = storage.start_run("tracking", region=region, note=note)

    ok = 0
    failed: list[str] = []
    reasons: dict[str, str] = {}
    recorded = 0

    for i, query in enumerate(qs, 1):
        m = measure(query, primary, region=region, n=n,
                    repeats=per_query.get(query, repeats))

        # Все снимки уходят в базу: по ним потом видно, кто стоял рядом и насколько прыгала выдача.
        snap_ids = [storage.save_serp(r, run_id=run_id) for r in m["results"]]
        if not m["results"]:
            failed.append(query)
            reasons[query] = (m.get("errors") or ["снимок не получен"])[0]
            if progress:
                print(f"[{i:03}/{len(qs)}] ERR  {query!r}", file=sys.stderr)
            continue

        ok += 1
        # Представительный снимок — тот, где позиция совпала с медианой. Тогда сохранённое
        # число и сохранённое доказательство описывают одно и то же состояние выдачи.
        idx = 0
        if m["median"] is not None:
            for j, res in enumerate(m["results"]):
                if _domain_position(res, primary) == m["median"]:
                    idx = j
                    break
        rep, rep_snap = m["results"][idx], snap_ids[idx]

        storage.save_competition(
            score_serp(rep, our_domain=primary), snapshot_id=rep_snap, run_id=run_id
        )
        recorded += storage.record_positions(
            rep, domains, snapshot_id=rep_snap, run_id=run_id,
            samples={primary.lower().removeprefix("www."): m["samples"]},
            override={primary.lower().removeprefix("www."): m["median"]},
        )

        if progress:
            spread = "" if m["min"] == m["max"] else f" разброс {m['min']}–{m['max']}"
            pos = m["median"] if m["median"] is not None else "нет в топе"
            stability = f" найдено в {m['found_in']} из {m['snapshots']}"
            print(
                f"[{i:03}/{len(qs)}] OK   {query!r:44} медиана {pos}{spread}{stability}",
                file=sys.stderr,
            )

    storage.finish_run(run_id)
    return TrackingRun(
        run_id=run_id, region=region, started_at=started,
        queries=len(qs), ok=ok, failed=failed, reasons=reasons, positions_recorded=recorded,
        calls_planned=planned,
    )


def calibrate(query: str, seen: int | None, domain: str | None = None,
              region: int = RUSSIA, n: int = 10, repeats: int = DEFAULT_REPEATS,
              note: str | None = None) -> dict:
    """
    Сверка замера с тем, что человек видит в браузере.

    Search API и живая выдача — разные пути обслуживания, и совпадение надо
    измерять, а не предполагать. Автоматически не проверить: Яндекс отдаёт
    капчу браузеру под управлением скрипта.
    """
    storage.init_db()
    domain = config.default_domain(domain)
    m = measure(query, domain, region=region, n=n, repeats=repeats)
    storage.save_calibration(
        query, domain, seen, m["median"], api_samples=m["samples"], region=region, note=note
    )
    rep = storage.calibration_report(domain, region=region)
    return {"measure": m, "seen": seen, "report": rep}


def report(domain: str | None = None, region: int = RUSSIA) -> list[dict]:
    return storage.latest_positions(config.default_domain(domain), region=region)


def movers(domain: str | None = None, region: int = RUSSIA, limit: int = 10) -> tuple[list[dict], list[dict]]:
    """Кто вырос и кто просел с прошлого замера."""
    rows = [r for r in report(domain, region) if r["delta"]]
    up = sorted([r for r in rows if r["delta"] > 0], key=lambda r: -r["delta"])[:limit]
    down = sorted([r for r in rows if r["delta"] < 0], key=lambda r: r["delta"])[:limit]
    return up, down


def _fmt_pos(v) -> str:
    return "нет в топе" if v is None else str(v)


def _fmt_delta(v) -> str:
    if v is None:
        return "—"
    if v > 0:
        return f"↑ {v}"
    if v < 0:
        return f"↓ {abs(v)}"
    return "="


def _print_report(domain: str, rows: list[dict]) -> None:
    if not rows:
        print(f"По домену {domain} замеров ещё нет. Сначала: --run --domain {domain}")
        return
    print(f"\nПозиции {domain}. Медиана по нескольким снимкам, дельта — к прошлому замеру.")
    print(f"  {'запрос':<40} {'сейчас':>10} {'было':>10} {'дельта':>8} {'разброс':>9}  оценка")
    for r in sorted(rows, key=lambda r: (r["position"] is None, r["position"] or 999)):
        # Один замер — предыдущего значения не существует. Это не то же самое,
        # что «домена не было в топе»: показываем прочерк, а не отсутствие.
        was = "—" if r["measurements"] < 2 else _fmt_pos(r["previous"])
        sp = r.get("spread")
        spread = f"{sp[0]}–{sp[1]}" if sp else "—"
        if r.get("within_noise"):
            verdict = "шум"
        elif r["delta"]:
            verdict = "рост" if r["delta"] > 0 else "падение"
        else:
            verdict = "—"
        print(
            f"  {r['query'][:40]:<40} {_fmt_pos(r['position']):>10} "
            f"{was:>10} {_fmt_delta(r['delta']):>8} {spread:>9}  {verdict}"
        )
    in_top = sum(1 for r in rows if r["position"] is not None)
    print(f"\n  В топе по {in_top} запросам из {len(rows)}.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Трекер позиций yaseo")
    p.add_argument("--add", action="append", help="добавить запрос в отслеживаемые")
    p.add_argument("--remove", action="append", help="снять запрос с отслеживания")
    p.add_argument("--list", action="store_true", help="показать отслеживаемые запросы")
    p.add_argument("--run", action="store_true", help="снять позиции по отслеживаемым запросам")
    p.add_argument("--report", action="store_true", help="показать текущие позиции с дельтой")
    p.add_argument("--movers", action="store_true", help="кто вырос и кто просел")
    p.add_argument("--domain", default=None,
                   help="основной домен; без него --run идёт по всем активным "
                        "проектам из таблицы projects")
    p.add_argument("--also", action="append", default=[], help="ещё домен для замера (конкурент)")
    p.add_argument("--region", type=int, default=RUSSIA)
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--repeats", type=int, default=None,
                   help="снимков на запрос; в историю идёт медиана")
    p.add_argument("--dry", action="store_true",
                   help="показать смету прогона и ничего не потратить")
    p.add_argument("--сейчас", "--now", dest="now", action="store_true",
                   help="снять минимальный интервал между прогонами на этот раз")
    p.add_argument("--max-calls", type=int, default=None,
                   help="потолок платных обращений за прогон; 0 — снять потолок")
    p.add_argument("--calibrate", metavar="ЗАПРОС",
                   help="сверить замер с живой выдачей; вместе с --seen")
    p.add_argument("--seen", type=int, default=None,
                   help="позиция, которую видит человек в браузере; 0 = не нашёл")
    p.add_argument("--calibration", action="store_true", help="показать накопленные сверки")
    args = p.parse_args(argv)

    storage.init_db()
    # Домен нужен только отчётам и сверке. У прогона его отсутствие — не пробел,
    # а команда «иди по таблице проектов», поэтому умолчание вычисляется только
    # там, где домен действительно нужен.
    domain = args.domain
    if args.report or args.movers or args.calibrate or args.calibration:
        try:
            domain = config.default_domain(args.domain)
        except config.DomainNotSetError as exc:
            print(exc, file=sys.stderr)
            return 1

    if args.add:
        for q in args.add:
            storage.track(q, region=args.region)
        print(f"добавлено запросов: {len(args.add)}")

    if args.remove:
        for q in args.remove:
            storage.untrack(q, region=args.region)
        print(f"снято с отслеживания: {len(args.remove)}")

    if args.list:
        qs = storage.tracked(args.region)
        print(f"Отслеживается запросов: {len(qs)}")
        for q in qs:
            print(f"  {q}")

    if args.run:
        repeats = args.repeats if args.repeats is not None else repeats_setting()
        cap = max_calls() if args.max_calls is None else int(args.max_calls)

        # Цели прогона. Домен назван руками — снимаем ровно его (так работает
        # разовый прогон человека). Не назван — идём по таблице проектов, где
        # домен и живёт.
        if args.domain:
            targets = [{
                "project_id": None, "project": "задан руками", "domain": args.domain,
                "queries": storage.tracked(args.region),
                "унаследовано_без_проекта": 0,
                "competitors": list(args.also) or competitors(args.domain),
            }]
        else:
            targets = auto_targets(args.region)

        if not targets:
            print("Активных проектов в этом регионе нет — снимать нечего.", file=sys.stderr)
            return 1

        stamp = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M")
        total = 0
        skipped = 0
        failed_any = False

        for t in targets:
            est = run_estimate(t, repeats=repeats, cap=cap, n=args.n)
            print(f"\n{stamp} · {t['project']} · {t['domain']}")
            print(smeta.text(est))
            if t.get("унаследовано_без_проекта"):
                print(f"Фраз без проекта в этом наборе: {t['унаследовано_без_проекта']} — "
                      f"отнесены сюда настройкой «orphan_project» из {config.tracking_file()}, "
                      f"это назначение, а не измерение")
            if not t["queries"]:
                print("Своих отслеживаемых фраз у проекта нет — пропускаю: снимать "
                      "по нему чужое ядро значило бы потратить на чужой спрос.")
                continue

            # Гейт срока. Стоит ПОСЛЕ печати сметы намеренно: человек, который
            # читает журнал, должен видеть не только «пропущено», но и во что
            # обошёлся бы этот прогон — иначе экономия остаётся невидимой, а
            # пропуск читается как поломка расписания.
            d = due(t["domain"], args.region)
            forced = args.now and not d["пора"]
            if forced:
                print(f"Срок снят ключом --сейчас: прошлый замер {d['прошлый']}, "
                      f"по сроку следующий был бы {d['следующий']}.")
                d = dict(d, пора=True, почему=None)
            if not d["пора"]:
                print(f"Пропускаю без трат: прошлый замер {d['прошлый']} "
                      f"({d['прошло суток']} сут назад при сроке {d['интервал']} сут), "
                      f"следующий не раньше {d['следующий']}. "
                      f"Этот прогон стоил бы {smeta.calls_word(est['обращений'])}. "
                      f"Снять срок на один раз — ключ --сейчас или "
                      f"YASEO_TRACKING_MIN_INTERVAL_DAYS=0")
                skipped += 1
                continue
            if d["почему"]:
                print(f"Срок не держит: {d['почему']}.")
            elif d["прошлый"] and not forced:
                print(f"Срок вышел: прошлый замер {d['прошлый']}, "
                      f"прошло {d['прошло суток']} сут при сроке {d['интервал']} сут.")

            if args.dry:
                continue

            res = run_tracking(
                [t["domain"], *t["competitors"]], queries=t["queries"],
                region=args.region, n=args.n, repeats=repeats, cap=cap,
                note=f"автозамер · {t['project']}",
            )
            if res.stopped:
                failed_any = True
                print(f"Прогон по {t['domain']} не состоялся: {res.stopped}")
                continue
            total += res.calls_planned
            # Стоимость и дата в журнале обязательны: число записей позиций легко
            # прочитать как расход, а это разные величины — позиции считают
            # домены, а обращения считают запросы.
            # Разбор берётся из сметы, снятой ДО прогона. Пересчитать его после
            # нельзя: прогон только что переписал позиции, и холодная фраза,
            # вошедшая в топ, задним числом стала бы горячей — журнал показал бы
            # расход, которого не было.
            print(f"Прогон {res.run_id} · {stamp} · {t['project']} ({t['domain']}): "
                  f"снято {res.ok} из {res.queries}, "
                  f"обращений к Yandex Search API {res.calls_planned} "
                  f"({est['из_чего']}) при потолке {cap or 'снят'}, "
                  f"записей позиций {res.positions_recorded}, "
                  f"домены: {', '.join([t['domain'], *t['competitors']])}")
            if res.failed:
                failed_any = True
                print(f"Не удалось: {', '.join(res.failed)}", file=sys.stderr)

        tail = (f" Целей пропущено по сроку: {skipped}." if skipped else "")
        if args.dry:
            print(f"\n--dry: ничего не потрачено, прогон не запускался.{tail}")
        else:
            print(f"\nИтого за {stamp}: обращений к Yandex Search API {total},{tail} "
                  f"потолок на прогон {cap or 'снят'}.")
        if failed_any:
            return 1

    if args.calibrate:
        if args.seen is None:
            print("Нужен --seen: какую позицию вы видите в браузере (0 — не нашли).",
                  file=sys.stderr)
            return 1
        seen = None if args.seen == 0 else args.seen
        res = calibrate(args.calibrate, seen, domain=domain, region=args.region,
                        n=args.n, repeats=args.repeats)
        m, rep = res["measure"], res["report"]
        print(f"\nСверка по {args.calibrate!r}, регион {args.region}")
        print(f"  Вы видите:  {_fmt_pos(seen)}")
        print(f"  Замер даёт: {_fmt_pos(m['median'])}  выборки {m['samples']}")
        if seen and m["median"]:
            print(f"  Расхождение: {m['median'] - seen:+d}")
        print(f"\n  Всего сверок: {rep['checks']}, сравнимых: {rep['comparable']}")
        if rep["avg_offset"] is not None:
            trust = "" if rep["trustworthy"] else "  (сверок мало, доверять рано)"
            print(f"  Среднее расхождение: {rep['avg_offset']:+}{trust}")

    if args.calibration:
        rep = storage.calibration_report(domain, region=args.region)
        print(f"\nСверки с живой выдачей по {domain}: {rep['checks']}")
        print(f"  {'запрос':<40} {'видит человек':>14} {'замер':>10} {'дата':>12}")
        for r in rep["rows"][:20]:
            print(f"  {r['query'][:40]:<40} {_fmt_pos(r['seen_position']):>14} "
                  f"{_fmt_pos(r['api_position']):>10} {r['checked_at'][:10]:>12}")
        if rep["avg_offset"] is not None:
            print(f"\n  Среднее расхождение: {rep['avg_offset']:+} по {rep['comparable']} сверкам")
            if not rep["trustworthy"]:
                print("  Сверок меньше пяти — выводы делать рано.")

    if args.report:
        _print_report(domain, report(domain, args.region))

    if args.movers:
        up, down = movers(domain, args.region)
        print(f"\nВыросли ({len(up)}):")
        for r in up:
            print(f"  {r['query'][:50]:<50} {_fmt_pos(r['previous'])} → {_fmt_pos(r['position'])}")
        print(f"\nПросели ({len(down)}):")
        for r in down:
            print(f"  {r['query'][:50]:<50} {_fmt_pos(r['previous'])} → {_fmt_pos(r['position'])}")

    if not any([args.add, args.remove, args.list, args.run, args.report, args.movers,
                args.calibrate, args.calibration]):
        p.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
