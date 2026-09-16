#!/usr/bin/env python3
"""
Яндекс.Метрика: что происходит на странице после перехода из поиска.

Вебмастер доводит человека до сайта — показы, клики, средняя позиция. Дальше
он не видит ничего. Метрика начинается ровно там, где Вебмастер заканчивается:
сколько визитов пришло на конкретную посадочную, сколько из них отказы, как
глубоко смотрели, сколько времени провели.

Разделение метрик жёсткое: клики берутся у Вебмастера, визиты — у Метрики, и
одно другим не проверяется. Это разные совокупности (клик из выдачи и визит с
засчитанным счётчиком считаются по разным правилам), и попытка сверить их даст
расхождение, которое нечем объяснить.

Источник трафика указывается всегда. `organic` — переходы из поисковых систем
вообще; `organic:yandex` — только из Яндекса. Смешивать нельзя: SEO под Яндекс
и суммарная органика — разные числа, и в отчёте движок называется.

Публичный API:
- counters()                       — счётчики, доступные токену
- resolve_counter(domain)          — счётчик по домену
- landing_pages(counter, ...)      — поведение по посадочным страницам
- pull(project_id, days, engine)   — выгрузить и положить в базу
- цепочка(project_id, ..., goal_ids) — воронка по целям счётчика

Запуск как скрипт:
    yaseo metrika --counters
    yaseo metrika --pull --project 1 --days 28
    yaseo metrika --pull --project 1 --engine yandex
    yaseo metrika --chain --project 1 --goals 111,222,333
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
from datetime import date, timedelta

from . import storage
from .yandex_auth import api_json

MANAGEMENT = "https://api-metrika.yandex.net/management/v1"
STAT = "https://api-metrika.yandex.net/stat/v1/data"

#: Порядок здесь задаёт порядок чисел в ответе Метрики — она возвращает голый
#: список значений без имён, и разъехавшийся порядок молча переставит колонки.
METRICS = (
    "ym:s:visits",
    "ym:s:bounceRate",
    "ym:s:pageDepth",
    "ym:s:avgVisitDurationSeconds",
)

#: Предел строк на страницу выгрузки.
PAGE = 500


#: Слепое пятно замера, которое обязано ехать вместе с числами воронки. Если
#: счётчик поднимается только после согласия на cookies, все числа — про
#: согласившихся, а не про всех пришедших, и ноль означает «ноль среди
#: согласившихся», а не «никто не сделал».
СЛЕПОЕ_ПЯТНО = ("если счётчик ставится после согласия на cookies, считаны только "
                "согласившиеся — ноль тогда означает ноль среди них")


def goals(counter: int) -> list[dict]:
    """Цели, заведённые на счётчике. Спрашиваем Метрику, а не свой список."""
    payload = api_json(f"{MANAGEMENT}/counter/{int(counter)}/goals")
    return [{"id": g.get("id"), "имя": g.get("name"), "тип": g.get("type"),
             "ключ": ((g.get("conditions") or [{}])[0] or {}).get("url")}
            for g in (payload.get("goals") or [])]


def цепочка(project_id: int = 1, date_from: str = "", date_to: str = "",
            limit: int = PAGE, goal_ids: list[int] | None = None) -> dict:
    """Воронка по страницам: визиты → цели в заданном порядке.

    `goal_ids` — цели воронки по порядку (от «прочитал» к «оставил заявку»).
    Не заданы — берутся все цели счётчика в том порядке, в каком их отдаёт
    Метрика.

    Отсутствие цели на счётчике называется вслух и НЕ подменяется нулём:
    «цели нет» и «цель есть, достижений ноль» — разные новости, и вторая
    означает работу, а первая — сломанный прибор.
    """
    storage.init_db()
    project = storage.get_project(project_id)
    if not project:
        raise SystemExit(f"проекта {project_id} нет")
    counter = project.get("metrika_counter") or resolve_counter(project["domain"])
    if not counter:
        raise SystemExit(f"счётчик для {project['domain']} не найден")
    if not date_from or not date_to:
        date_from, date_to = window(28)

    на_счётчике = {int(g["id"]): g for g in goals(int(counter)) if g.get("id") is not None}
    порядок = [int(x) for x in goal_ids] if goal_ids else list(на_счётчике)
    беру = [{"id": gid, "ключ": f"goal{gid}",
             "имя": str(на_счётчике[gid].get("имя") or gid),
             "зачем": f"цель {gid} ({на_счётчике[gid].get('тип') or 'тип не назван'})"}
            for gid in порядок if gid in на_счётчике]
    нет = [{"имя": f"цель {gid}", "id": gid}
           for gid in порядок if gid not in на_счётчике]

    metrics = ["ym:s:visits"] + [f"ym:s:goal{ц['id']}reaches" for ц in беру]
    params = urllib.parse.urlencode({
        "ids": int(counter), "metrics": ",".join(metrics),
        "dimensions": "ym:s:startURL", "date1": date_from, "date2": date_to,
        "sort": "-ym:s:visits", "limit": limit,
    })
    payload = api_json(f"{STAT}?{params}")

    страницы = []
    for item in (payload.get("data") or []):
        значения = list(item.get("metrics") or [])
        значения += [None] * (len(metrics) - len(значения))
        строка = {"url": ((item.get("dimensions") or [{}])[0] or {}).get("name"),
                  "визиты": int(значения[0] or 0)}
        for i, ц in enumerate(беру, start=1):
            строка[ц["ключ"]] = int(значения[i] or 0)
        страницы.append(строка)

    итоги = [int(x or 0) for x in (payload.get("totals") or [])]
    всего = {"визиты": итоги[0] if итоги else 0}
    for i, ц in enumerate(беру, start=1):
        всего[ц["ключ"]] = итоги[i] if len(итоги) > i else 0

    return {"счётчик": int(counter), "с": date_from, "по": date_to,
            "цели": беру, "целей нет на счётчике": нет,
            "страницы": страницы, "всего": всего,
            "слепое пятно": СЛЕПОЕ_ПЯТНО}


def window(days: int = 28) -> tuple[str, str]:
    end = date.today()
    return str(end - timedelta(days=max(days, 1) - 1)), str(end)


def source_label(engine: str | None) -> str:
    """Как источник называется в базе. Движок входит в название, а не подразумевается."""
    return f"organic:{engine.strip().lower()}" if engine else "organic"


def _filters(engine: str | None) -> str:
    base = "ym:s:lastTrafficSource=='organic'"
    if engine:
        return f"{base} AND ym:s:lastSearchEngineRoot=='{engine.strip().lower()}'"
    return base


def _fmt(value, suffix: str = "", digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


# ──────────────────────────── счётчики ────────────────────────────

def counters() -> list[dict]:
    payload = api_json(f"{MANAGEMENT}/counters")
    out = []
    for c in payload.get("counters") or []:
        site = c.get("site") or ((c.get("site2") or {}).get("site"))
        out.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "site": site,
            "status": c.get("status"),
            "owner": c.get("owner_login"),
        })
    return out


def resolve_counter(domain: str) -> int | None:
    want = domain.strip().lower().removeprefix("www.")
    for c in counters():
        site = (c.get("site") or "").strip().lower().removeprefix("www.")
        if site == want:
            return int(c["id"])
    return None


# ──────────────────────────── выгрузка ────────────────────────────

def landing_pages(counter: int, date_from: str, date_to: str,
                  engine: str | None = None, limit: int = PAGE) -> list[dict]:
    """
    Поведение по посадочным страницам органики.

    Метрика возвращает метрики списком без имён, поэтому разбор идёт строго по
    порядку из `METRICS`. Пустая выдача — это «данных за окно нет», а не ноль:
    ноль визитов и отсутствие строки различаются, и подставлять первое вместо
    второго нельзя.
    """
    rows: list[dict] = []
    offset = 1  # Метрика нумерует строки с единицы
    while True:
        take = min(PAGE, limit - len(rows)) if limit else PAGE
        if take <= 0:
            break
        params = urllib.parse.urlencode({
            "ids": int(counter),
            "metrics": ",".join(METRICS),
            "dimensions": "ym:s:startURL",
            "date1": date_from,
            "date2": date_to,
            "filters": _filters(engine),
            "sort": "-ym:s:visits",
            "limit": take,
            "offset": offset,
        })
        payload = api_json(f"{STAT}?{params}")
        batch = payload.get("data") or []
        for item in batch:
            dims = item.get("dimensions") or [{}]
            values = list(item.get("metrics") or [])
            values += [None] * (len(METRICS) - len(values))
            rows.append({
                "url": (dims[0] or {}).get("name"),
                "visits": int(values[0]) if values[0] is not None else None,
                "bounce_rate": values[1],
                "page_depth": values[2],
                "avg_duration": values[3],
            })
        offset += len(batch)
        total = int(payload.get("total_rows") or 0)
        if not batch or (limit and len(rows) >= limit) or offset > total:
            break
    return rows


def pull(project_id: int = 1, days: int = 28, engine: str | None = None) -> dict:
    """Снимает поведение по посадочным и кладёт в базу под своим источником."""
    storage.init_db()
    project = storage.get_project(project_id)
    if not project:
        raise SystemExit(f"проекта {project_id} нет")

    counter = project.get("metrika_counter") or resolve_counter(project["domain"])
    if not counter:
        known = ", ".join(f"{c['id']} ({c['site']})" for c in counters()) or "нет ни одного"
        raise SystemExit(
            f"счётчик для {project['domain']} не найден. Доступные: {known}. "
            f"Задать вручную: yaseo metrika --set-counter <id> --project {project_id}"
        )
    storage.set_project_yandex(project_id, metrika_counter=int(counter))

    date_from, date_to = window(days)
    source = source_label(engine)
    rows = landing_pages(int(counter), date_from, date_to, engine=engine)
    saved = storage.save_metrika_pages(int(counter), date_from, date_to, source, rows,
                                       project_id=project_id)
    return {
        "project_id": project_id,
        "counter": int(counter),
        "source": source,
        "date_from": date_from,
        "date_to": date_to,
        "pages": saved,
        "visits": sum(r["visits"] or 0 for r in rows),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Яндекс.Метрика: поведение на посадочных")
    p.add_argument("--counters", action="store_true", help="счётчики, доступные токену")
    p.add_argument("--pull", action="store_true", help="выгрузить поведение в базу")
    p.add_argument("--set-counter", type=int, default=None, dest="set_counter",
                   help="привязать счётчик к проекту вручную")
    p.add_argument("--top", type=int, default=0, help="показать N страниц последнего окна")
    p.add_argument("--цепочка", "--chain", action="store_true", dest="chain",
                   help="воронка по целям счётчика: визиты → цели по порядку")
    p.add_argument("--goals", default=None,
                   help="id целей воронки через запятую, по порядку; по умолчанию все цели счётчика")
    p.add_argument("--цели", "--show-goals", action="store_true", dest="show_goals",
                   help="цели, заведённые на счётчике проекта")
    p.add_argument("--с", dest="date_from", default=None, help="начало периода ГГГГ-ММ-ДД")
    p.add_argument("--по", dest="date_to", default=None, help="конец периода ГГГГ-ММ-ДД")
    p.add_argument("--project", type=int, default=1)
    p.add_argument("--days", type=int, default=28)
    p.add_argument("--engine", default=None,
                   help="сузить до одного движка: yandex, google. По умолчанию вся органика")
    args = p.parse_args(argv)
    goal_ids = [int(x) for x in args.goals.split(",") if x.strip()] if args.goals else None

    if args.counters:
        for c in counters():
            print(f"  {c['id']:<12} {str(c['site'] or '—'):<28} {c['name']} · {c['status']}")
        return 0

    if args.set_counter:
        storage.init_db()
        storage.set_project_yandex(args.project, metrika_counter=args.set_counter)
        print(f"Проекту {args.project} назначен счётчик {args.set_counter}.")
        return 0

    if args.pull:
        res = pull(args.project, days=args.days, engine=args.engine)
        print(f"\nСчётчик {res['counter']} · окно {res['date_from']} … {res['date_to']} "
              f"· источник {res['source']}")
        print(f"  посадочных страниц : {res['pages']}")
        print(f"  визитов            : {res['visits']}")
        return 0

    if args.show_goals:
        project = storage.get_project(args.project) or {}
        counter = project.get("metrika_counter") or resolve_counter(project.get("domain", ""))
        if not counter:
            print("У проекта нет привязки к Метрике — сначала --pull")
            return 1
        читаем = set(goal_ids or [])
        for g in goals(int(counter)):
            метка = "· в воронке" if (not читаем or g["id"] in читаем) else "  не в воронке"
            print(f"  {g['id']:<12} {str(g['имя'])[:32]:<32} {str(g['ключ'] or ''):<18} {метка}")
        return 0

    if args.chain:
        d_from, d_to = args.date_from, args.date_to
        if not d_from or not d_to:
            d_from, d_to = window(args.days)
        r = цепочка(args.project, d_from, d_to, goal_ids=goal_ids)
        print(f"\n═══ Воронка · счётчик {r['счётчик']} · "
              f"{r['с']} … {r['по']} ═══")
        print(f"  ⚠ {r['слепое пятно']}")
        for отсутствует in r["целей нет на счётчике"]:
            print(f"  ! цели «{отсутствует['имя']}» ({отсутствует['id']}) на счётчике "
                  f"НЕТ — это не ноль, это отсутствующий прибор")
        в = r["всего"]
        print("\n  ── всего за период ──")
        print(f"     визитов {в.get('визиты', 0)}")
        пред = в.get("визиты", 0)
        for ц in r["цели"]:
            сейчас = в.get(ц["ключ"], 0)
            доля = f"{сейчас / пред * 100:.1f}% от предыдущего" if пред else "не от чего считать"
            print(f"     {ц['имя']:<24} {сейчас:>6}   {доля}")
            print(f"        {ц['зачем']}")
            пред = сейчас
        рвётся = next((ц["имя"] for ц in r["цели"] if в.get(ц["ключ"], 0) == 0), None)
        if рвётся:
            print(f"\n  ! цепочка рвётся на звене «{рвётся}»: дальше него не прошёл никто")
        строки = r["страницы"]
        if строки:
            ключи = [ц["ключ"] for ц in r["цели"]]
            шапка = "".join(f"{ц['имя'][:9]:>10}" for ц in r["цели"])
            print(f"\n  ── по страницам ──\n     {'визиты':>7}{шапка}  страница")
            for x in строки[:12]:
                числа = "".join(f"{x.get(k, 0):>10}" for k in ключи)
                путь = storage.normalize_path(x['url'])
                print(f"     {x['визиты']:>7}{числа}  {путь[:44]}")
        return 0

    if args.top:
        project = storage.get_project(args.project)
        counter = (project or {}).get("metrika_counter")
        if not counter:
            print("У проекта нет привязки к Метрике — сначала --pull")
            return 1
        print(f"  {'визиты':>7} {'отказы':>8} {'глубина':>8} {'время':>7}  страница")
        for r in storage.metrika_pages_latest(int(counter))[:args.top]:
            print(f"  {r['visits'] or 0:>7} "
                  f"{_fmt(r['bounce_rate'], '%'):>8} "
                  f"{_fmt(r['page_depth'], '', 2):>8} "
                  f"{_fmt(r['avg_duration'], 'с'):>7}  "
                  f"{storage.normalize_path(r['url'])}")
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
