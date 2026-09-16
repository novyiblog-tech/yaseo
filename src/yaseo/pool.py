#!/usr/bin/env python3
"""
Расширение пула отслеживаемых запросов через Wordstat.

Wordstat отдаёт два семейства расширений: уточнения (содержат саму фразу) и
ассоциации (смежный спрос). Ассоциации бывают на порядок частотнее и при этом
мусорнее: по «пластиковые окна» это «окна мод», «окно песня», «окно это».
Брать их по частотности значит забить пул шумом.

Поэтому решение по каждому кандидату принимается по правилам и объясняется:
у любого кандидата есть `reason`, и видно, почему он взят или отброшен.

Словарь ниши принадлежит проекту, а не модулю (`projects.service_stems` /
`object_stems`). Пока он не задан, ассоциации не судятся по нише и не берутся,
а уточнения (содержат исходную фразу) берутся. Что именно решено, называется
вслух: см. `niche_stems`.

Публичный API:
- niche_stems(project_id)                         -> dict   словарь ниши и «откуда»
- niche_decision(phrase, stems)                   -> (взять, причина)
- suggest(seed, region, min_freq, limit)          -> list[Candidate]
- expand_pool(project_id, seed, ..., apply=False) -> dict
- relabel(project_id, apply=False)                -> dict   типы фраз в ядре

Запуск как скрипт:
    yaseo pool --seed "пластиковые окна"                   # показать
    yaseo pool --seed "..." --project 1 --apply            # записать
    yaseo pool --relabel                                   # типы фраз
    yaseo pool --relabel --apply                           # записать
    yaseo pool --project 1 --services окн,остеклен --objects дом,квартир,балкон
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field

from . import storage
from .wordstat import frequency

#: Основы слов ниши по умолчанию. Сверяем по началу слова, а не по точному
#: совпадению: русская морфология даёт «остекление», «остеклению», «окон», и
#: список точных форм пришлось бы вести бесконечно.
#:
#: Умолчания пусты намеренно: словарь ниши — свойство проекта, и любой
#: встроенный словарь судил бы чужой продукт чужими словами. Задаётся у
#: проекта: `yaseo pool --project N --services ... --objects ...`.
#:
#: Объекты разведены на два списка, потому что они называют разных людей:
#: заказчика услуги (CLIENT — делает фразу целевой в паре с услугой,
#: `core.classify`) и конечного потребителя (CONSUMER — обозначает нишу, но
#: целевой фразу не делает). Иначе чужой потребительский спрос получал бы
#: метку «целевой».
CLIENT_OBJECT_STEMS: tuple[str, ...] = ()
CONSUMER_OBJECT_STEMS: tuple[str, ...] = ()
NICHE_OBJECT_STEMS: tuple[str, ...] = CLIENT_OBJECT_STEMS + CONSUMER_OBJECT_STEMS

#: То, что делает фразу целевой в паре с услугой (читает `core.classify`).
OBJECT_STEMS: tuple[str, ...] = CLIENT_OBJECT_STEMS
SERVICE_STEMS: tuple[str, ...] = ()
NICHE_STEMS: tuple[str, ...] = NICHE_OBJECT_STEMS + SERVICE_STEMS

#: Мусорные хвосты: справочные и развлекательные запросы.
NOISE = re.compile(
    r"\b(это|значение|вики|википедия|перевод|скачать|бесплатно|торрент|мод|песня|сонг)\b",
    re.I,
)

#: Чужие продукты — точка подключения. Запрос про чужой продукт — это спрос на
#: настройку этого продукта, а не на услугу проекта: широкий сид «сквозная
#: аналитика» легко даёт десятки уточнений про конкретные сервисы, и все они
#: прошли бы как «уточнение: содержит исходную фразу». По умолчанию фильтра нет;
#: пример: `pool.VENDOR = re.compile(r"(битрикс|roistat|amocrm)", re.I)`.
VENDOR: re.Pattern | None = None

#: Инструментальный спрос: «как настроить», «интеграция», «тариф». Ищет тот,
#: кто делает руками сам, а не покупает услугу. Применяется только к
#: уточнениям широкого seed, и только когда у проекта задан словарь объектов.
SETUP = re.compile(
    r"(настро[йи]|подключ|интеграц|установ|инструкц|шаблон|тариф|api|выгрузк)",
    re.I,
)

#: Название чужого объекта или бренда без слова про услугу — точка подключения.
#: Пример для рынка новостроек: `re.compile(r"^жк\s+", re.I)` («жк легенда»).
FOREIGN_NAME: re.Pattern | None = None


@dataclass
class Candidate:
    phrase: str
    freq: int
    kind: str          # top | association
    accepted: bool
    reason: str
    #: Что с фразой — одной строкой для человека («взяли — причина»). Два имени
    #: (`status` и `verdict`) под одним выражением (`_вслух`): отчёты читают
    #: то одно, то другое, а второго определения «что с фразой» нет.
    #: Вердикт назван вслух: одна причина без вердикта читается одинаково у
    #: принятой и у отброшенной фразы.
    status: str = field(init=False, default="")
    verdict: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.status = self.verdict = self._вслух()

    def _вслух(self) -> str:
        """Что с фразой — одной строкой для человека."""
        причина = " ".join((self.reason or "").split())
        слово = "взяли" if self.accepted else "не взяли"
        # Тире, а не двоеточие: причина сама бывает с двоеточием («уточнение:
        # содержит исходную фразу»), и два подряд читаются как опечатка.
        return f"{слово} — {причина}" if причина else слово


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\W+", text.lower()) if w]


def _stem_hits(phrase: str, stems: tuple[str, ...]) -> set[str]:
    """Основы ниши, задетые фразой. Сверка по началу слова."""
    return {s for w in _words(phrase) for s in stems if w.startswith(s)}


# ── словарь ниши: чей он ─────────────────────────────────────────────────────

#: Как называется происхождение словаря. Формулировки вынесены в константы,
#: потому что их читает человек в отчёте, а сравнивает — код.
СВОЙ = "свой словарь проекта"
УМОЛЧАНИЕ = "словарь не задан"


def niche_stems(project_id: int | None = None) -> dict:
    """
    Словарь ниши проекта: основы услуг, основы объекта и откуда они взяты.

    Встроенный словарь судил бы чужой продукт чужими словами: законные фразы
    уезжали бы в «вне ниши», а в ядре не оставалось бы ничего. Поэтому словарь
    берётся только у проекта, а его отсутствие называется вслух (`откуда`):
    «так задумано» и «никто не настраивал» выглядят одинаково, а значат разное.

    Ключ «объекты» — то, что вместе с услугой делает фразу целевой
    (`core.classify`). Ключ «объекты ниши» — то, по чему решается «назван ли
    предмет ниши вообще» (`_judge`, `is_broad`). У проекта со своим словарём
    эти списки совпадают: кто заказчик, а кто потребитель,
    — вопрос, который решается внутри ниши, и за проект его никто не решал.
    Придумывать за него второй список значило бы выдать догадку за настройку.
    """
    services: list[str] | None = None
    objects: list[str] | None = None
    if project_id:
        try:
            services, objects = storage.project_stems(int(project_id))
        except Exception:  # noqa: BLE001 — словарь не повод ронять сбор
            services, objects = None, None

    if services and objects:
        return {
            "проект": int(project_id),
            "услуги": tuple(services),
            "объекты": tuple(objects),
            "объекты ниши": tuple(objects),
            "откуда": СВОЙ,
            "почему": "основы услуг и объекта заданы у проекта в базе",
        }

    # Полсловаря — это не словарь: судить по одной половине значило бы выдать
    # догадку за решение. Фильтр ниши не применяется, и сказано, чего не хватает.
    недостаёт = ""
    if services or objects:
        недостаёт = (" У проекта заполнена только половина словаря "
                     f"({'услуги' if services else 'объекты'}), "
                     "а половина словаря нишу не описывает.")
    return {
        "проект": int(project_id) if project_id else None,
        "услуги": SERVICE_STEMS,
        "объекты": OBJECT_STEMS,
        "объекты ниши": NICHE_OBJECT_STEMS,
        "откуда": УМОЛЧАНИЕ,
        "почему": ("словарь ниши у проекта не задан: уточнения берутся, "
                   "ассоциации — нет. Задать: yaseo pool --project N "
                   "--services ... --objects ..." + недостаёт),
    }


def _defined(stems: dict) -> bool:
    """Задан ли словарь ниши хоть как-то."""
    return bool(stems.get("услуги") or stems.get("объекты ниши"))


def is_broad(seed: str, stems: dict | None = None) -> bool:
    """
    Широкий seed — тот, в котором нет слова про объект ниши. «сквозная
    аналитика» широкая, «сквозная аналитика для магазина» нет. Уточнения
    широкого seed сами по себе в нишу не попадают и требуют проверки.

    Здесь ниша берётся целиком («объекты ниши»): для отбора кандидатов важно,
    назван ли предмет ниши вообще, а не чей это спрос. Чей — решает
    `core.classify`. Без словаря объектов широту не определить — seed не
    считается широким, иначе фильтр резал бы законные уточнения.
    """
    st = stems or niche_stems()
    if not st["объекты ниши"]:
        return False
    return not _stem_hits(seed, st["объекты ниши"])


def _niche_verdict(phrase: str, services: set[str], objects: set[str],
                   defined: bool = True) -> tuple[bool, str, bool]:
    """
    Решение по фразе одним только словарём ниши: взять или нет и почему.

    Возвращает (взять, причина, окончательно). «Окончательно» значит, что
    решение не зависит от того, уточнение это или ассоциация: название чужого
    объекта и чужой продукт отбрасываются в обоих случаях. Остальное `_judge`
    вправе перебить правилом уточнения.

    Правило ниши живёт здесь в одном экземпляре намеренно (одна метрика —
    одно место). Его читают трое: отбор кандидатов в пул (`_judge`),
    пересуд накопленного ядра (`core.rejudge`) и человек, который спрашивает
    «почему эту фразу не взяли».
    """
    if FOREIGN_NAME is not None and FOREIGN_NAME.match(phrase.strip()) and not services:
        return False, "название чужого объекта — чужой спрос", True

    vendor = VENDOR.search(phrase) if VENDOR is not None else None
    if vendor and not objects:
        return (False,
                f"уточнение про чужой продукт «{vendor.group(0)}» — спрос на его настройку",
                True)

    # Слово про услугу — однозначный признак темы проекта.
    if services:
        return True, f"ассоциация в нише: {', '.join(sorted(services))}", False

    # Только объект без услуги — слишком широко.
    if objects:
        return False, f"есть объект «{next(iter(objects))}», но нет слова про услугу", False

    if not defined:
        return False, "ассоциация: словарь ниши не задан — смежный спрос не судится", False
    return False, "ассоциация вне ниши", False


def niche_decision(phrase: str, stems: dict | None = None) -> tuple[bool, str]:
    """
    Наша ли это тема по словарю ниши — без знания seed, из одной фразы.

    Нужно там, где seed уже неизвестен: в базе ядра лежит фраза, а от какого
    семени она пришла — нет. Поэтому правило уточнения («содержит исходную
    фразу») здесь не применяется, и решение принимает только состав фразы.
    """
    st = stems or niche_stems()
    services = _stem_hits(phrase, st["услуги"])
    objects = _stem_hits(phrase, st["объекты ниши"])
    взять, причина, _ = _niche_verdict(phrase, services, objects, _defined(st))
    return взять, причина


def _judge(phrase: str, freq: int, kind: str, seed: str, min_freq: int,
           known: set[str], stems: dict | None = None) -> Candidate:
    """
    Решение по кандидату. `stems` — словарь ниши; `None` — словарь не задан
    (`niche_stems()` без проекта).
    """
    st = stems or niche_stems()

    def no(reason: str) -> Candidate:
        return Candidate(phrase, freq, kind, False, reason)

    if phrase in known:
        return no("уже отслеживается")
    if phrase.strip().lower() == seed.strip().lower():
        return no("это сам seed")
    if freq < min_freq:
        return no(f"частотность {freq} ниже порога {min_freq}")
    if NOISE.search(phrase):
        return no("справочный или развлекательный запрос")

    services = _stem_hits(phrase, st["услуги"])
    # Отбор в пул спрашивает «назван ли предмет ниши вообще», поэтому ниша
    # берётся целиком. Кому этот спрос принадлежит — вопрос типа фразы, и он
    # решается отдельно (`core.classify`).
    objects = _stem_hits(phrase, st["объекты ниши"])

    # Правило ниши одно и лежит в `_niche_verdict`: и чужое название без
    # услуги, и чужой продукт отбрасываются до разбора «уточнение или ассоциация».
    взять, причина, окончательно = _niche_verdict(phrase, services, objects, _defined(st))
    if окончательно:
        return no(причина)

    if kind == "top":
        # Уточнение наследует нишу от seed, только если seed сам в нише.
        # У широкого seed уточнение обязано нишу добавить само.
        if is_broad(seed, st) and not objects:
            setup = SETUP.search(phrase)
            if setup:
                return no(
                    f"уточнение широкого seed про «{setup.group(0)}» — "
                    "инструментальный спрос, не покупатель услуги"
                )
        return Candidate(phrase, freq, kind, True, "уточнение: содержит исходную фразу")

    return Candidate(phrase, freq, kind, взять, причина)


def suggest(seed: str, region: str = "225", min_freq: int = 30, limit: int = 20,
            known: set[str] | None = None,
            stems: dict | None = None) -> list[Candidate]:
    """Кандидаты в пул по одной seed-фразе. Ничего не записывает."""
    f = frequency(seed, region=region, num=200)
    if f.error:
        return [Candidate(seed, 0, "seed", False, f"Wordstat не ответил: {f.error}")]

    known = known or set()
    judged = [_judge(e.phrase, e.freq, e.kind, seed, min_freq, known, stems)
              for e in f.expansions]

    # Уточнения вперёд: они всегда ближе к теме, чем самая частотная ассоциация.
    accepted = sorted(
        [c for c in judged if c.accepted],
        key=lambda c: (c.kind != "top", -c.freq),
    )[:limit]
    rejected = [c for c in judged if not c.accepted]
    return accepted + rejected


def expand_pool(project_id: int | None, seed: str, min_freq: int = 30, limit: int = 20,
                region: int = 225, apply: bool = False) -> dict:
    """Расширяет пул проекта. Без `apply` только показывает, что было бы добавлено."""
    storage.init_db()
    known = set(storage.tracked(region))
    # Кандидаты судятся словарём того проекта, чей это пул.
    cands = suggest(seed, region=str(region), min_freq=min_freq, limit=limit,
                    known=known, stems=niche_stems(project_id))
    accepted = [c for c in cands if c.accepted]

    added = 0
    if apply:
        for c in accepted:
            if project_id:
                storage.track_for_project(project_id, c.phrase, region)
            else:
                storage.track(c.phrase, region)
            added += 1

    return {
        "seed": seed,
        "region": region,
        "applied": apply,
        "added": added,
        "accepted": len(accepted),
        "skipped": len(cands) - len(accepted),
        "candidates": [asdict(c) for c in cands],
        "pool_size": len(storage.tracked(region)),
    }


def relabel(project_id: int | None = None, apply: bool = False) -> dict:
    """
    Переклеить типы фраз в уже собранном ядре по действующим правилам.

    Тип (`brand` / `target` / `adjacent`) записан в базу в момент сборки ядра.
    Правка правила или словаря сама по себе строки не чинит: старая метка
    стоит, пока ядро не пересобрано, а пересборка ядра — это Wordstat и
    выдача, то есть деньги.

    Переклейка бесплатна: тип считается из самой фразы тем же `core.classify`,
    ни одного обращения наружу. Без `apply` только показывает, что изменится.
    Второго правила здесь нет — правило одно, в `core.classify`.

    Брендовые строки не трогаются. Их тип назначен не составом фразы, а
    происхождением: расширения брендового seed получают `brand` в
    `core._judge_brand` независимо от слов. Восстановить происхождение из
    одной фразы нельзя, а переклеить их по составу значило бы применить к ним
    чужое правило. Переклеивается граница между `target` и `adjacent`.

    Смена самого словаря ниши — другой случай и другая команда
    (`core.rejudge`): там правило меняется целиком, брендовое происхождение
    пересматривается осознанно и пересчёт называется числом.
    """
    from . import core  # локально: core импортирует pool, кольцо на загрузке

    storage.init_db()
    projects = ([storage.get_project(project_id)] if project_id
                else storage.list_projects())
    changes: list[dict] = []
    for project in [p for p in projects if p]:
        try:
            brands = json.loads(project.get("brand_terms") or "[]")
        except ValueError:
            brands = []
        keys = core.brand_keys(brands)
        # Тип считается словарём того проекта, чьё это ядро.
        stems = niche_stems(int(project["id"]))
        for row in storage.get_core(int(project["id"])):
            was = row.get("kind")
            if was == core.BRAND:
                continue
            now = core.classify(row["phrase"], keys, stems)
            if now == core.BRAND or was == now:
                continue
            changes.append({"проект": int(project["id"]), "фраза": row["phrase"],
                            "было": was, "стало": now, "частотность": row.get("freq")})

    if apply and changes:
        with storage.connect() as conn:
            conn.executemany(
                "UPDATE core_entries SET kind=? WHERE project_id=? AND phrase=?",
                [(c["стало"], c["проект"], c["фраза"]) for c in changes],
            )
    return {"переклеено" if apply else "переклеить": len(changes),
            "применено": apply, "строки": changes}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Расширение пула запросов через Wordstat")
    p.add_argument("--seed", action="append", help="seed-фраза (можно повторять)")
    p.add_argument("--переклеить", "--relabel", action="store_true", dest="relabel",
                   help="пересчитать типы фраз в ядре по действующим правилам "
                        "(бесплатно: наружу не ходит)")
    p.add_argument("--project", type=int, default=None, help="id проекта")
    p.add_argument("--region", type=int, default=225)
    p.add_argument("--min-freq", type=int, default=30, dest="min_freq")
    p.add_argument("--limit", type=int, default=20, help="сколько взять с одного seed")
    p.add_argument("--apply", action="store_true", help="записать в пул, а не только показать")
    p.add_argument("--show-rejected", action="store_true", dest="show_rejected")
    p.add_argument("--services", default=None,
                   help="словарь ниши проекта: основы слов услуг через запятую "
                        "(пусто — снять); нужен --project")
    p.add_argument("--objects", default=None,
                   help="словарь ниши проекта: основы слов объекта через запятую")
    args = p.parse_args(argv)

    if args.services is not None or args.objects is not None:
        if not args.project:
            print("Словарь ниши задаётся проекту: нужен --project", file=sys.stderr)
            return 2

        def _split(raw):
            return None if raw is None else [w.strip() for w in raw.split(",") if w.strip()]

        storage.init_db()
        services, objects = storage.set_project_stems(
            args.project, _split(args.services), _split(args.objects))
        print(f"Словарь ниши проекта {args.project}: услуги {services or '—'}, "
              f"объекты {objects or '—'}")
        if not args.seed and not args.relabel:
            return 0

    if args.relabel:
        res = relabel(args.project, apply=args.apply)
        rows = res["строки"]
        if not rows:
            print("Типы фраз совпадают с правилами — переклеивать нечего.")
            return 0
        print(f"Расходится с правилами строк: {len(rows)}")
        for c in rows[:60]:
            print(f"  проект {c['проект']}  {str(c['частотность'] or '—'):>7}  "
                  f"{c['фраза'][:52]:<52} {c['было']} → {c['стало']}")
        print("\nЗаписано в базу." if args.apply
              else "\nПоказ без записи. Записать: --apply")
        return 0

    if not args.seed:
        print("Нужен --seed или --relabel", file=sys.stderr)
        return 2

    total_added = 0
    for seed in args.seed:
        res = expand_pool(args.project, seed, min_freq=args.min_freq, limit=args.limit,
                          region=args.region, apply=args.apply)
        total_added += res["added"]

        print(f"\nSeed: {seed!r}  регион {res['region']}")
        acc = [c for c in res["candidates"] if c["accepted"]]
        rej = [c for c in res["candidates"] if not c["accepted"]]

        if acc:
            print(f"  Берём {len(acc)}:")
            for c in acc:
                print(f"    {c['freq']:>7}  {c['phrase'][:52]:<52} {c['reason']}")
        else:
            print("  Брать нечего.")

        if args.show_rejected and rej:
            print(f"  Отброшено {len(rej)}:")
            for c in rej[:25]:
                print(f"    {c['freq']:>7}  {c['phrase'][:52]:<52} {c['reason']}")
        elif rej:
            print(f"  Отброшено {len(rej)} — показать: --show-rejected")

    if args.apply:
        print(f"\nДобавлено в пул: {total_added}. Размер пула: {len(storage.tracked(args.region))}.")
    else:
        print("\nПоказ без записи. Записать: --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
