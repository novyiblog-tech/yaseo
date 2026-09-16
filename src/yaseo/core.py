#!/usr/bin/env python3
"""
Семантическое ядро проекта — сбор, проверка выдачей, вердикт.

Ядро принадлежит проекту, а не инструменту: подключили другой сайт — собираем
его семантику под него. Семена проекта лежат в `projects.brand_terms` и
`projects.seed_terms`, результат — в `core_entries`.

Три типа фраз:

- `brand`    — запросы по имени компании и его написаниям («гранит строй»,
               «гранитстрой», «granit stroy») и уточнённые формы.
- `target`   — по роду деятельности: есть и слово про услугу, и слово про объект
               («остекление балкона», «ремонт квартиры»).
- `adjacent` — околоцелевые: слово про услугу без объекта («остекление»,
               «фирменный стиль»). Тема шире услуги, покупатель соседний.

Мина брендового ядра. Имя компании бывает общим словом или отраслевым
термином («гранит» — камень). Голая фраза тогда собирает чужой спрос.
Поэтому по каждой брендовой фразе снимается выдача: если нас в топе нет и в
фразе нет уточнения ниши, строка помечается `brand_ambiguous` и в чистое
брендовое ядро не идёт.

Правило отбора: решает состав выдачи, а не объём спроса. Ноль в Wordstat
означает «ниже порога отчётности сервиса», а не отсутствие спроса: узкая фраза
с нулём в Wordstat может иметь полностью коммерческий топ, а широкая с тысячами
показов — учебный. В топе стоят конкуренты, а не покупатели.

Источники (одна метрика — одно место):
- частотность и расширения — `yaseo.wordstat`    (Yandex Wordstat API v2)
- выдача и её состав       — `yaseo.yandex_serp`  (Yandex Search API v2)
- конкурентность           — `yaseo.competition`  (расчёт по составу выдачи)
- отнесение фразы к нише   — `yaseo.pool`         (единственный фильтр, второго нет)

Расход API. `collect` тратит один запрос Wordstat на seed — расширения приходят
тем же ответом, и по ним запросов не делается. `validate` тратит один запрос
Search API на фразу, поэтому лимит обязателен и задаётся явно.

Словарь ниши принадлежит проекту (`pool.niche_stems`): что проект продаёт и
кому. Без словаря тип фразы не выше `adjacent`, а признак специализации топа
берётся по общим коммерческим словам; это называется вслух в отчёте.

Отчёты пишутся в каталог данных (`config.reports_dir()`).

Публичный API:
- collect(project_id, brand_terms, seed_terms, region)   -> list[CoreEntry]
- validate(entries, our_domain, limit, region)           -> list[CoreEntry]
- build(project_id, ..., validate_top=60)                -> dict
- rejudge(project_id, apply=False)                       -> dict   бесплатный пересуд
- render_markdown(core)                                  -> str

Запуск как скрипт:
    yaseo core --project 1 --brand "моя компания" --seed "остекление балкона" --collect
    yaseo core --project 1 --validate-top 20 --apply
    yaseo core --project 1 --rejudge            # показать
    yaseo core --project 1 --rejudge --apply    # записать
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import config, morph, storage, wordstat
from .competition import _is_pseudo, _norm, score_serp
from .pool import (
    _judge,
    _stem_hits,
    niche_decision,
    niche_stems,
)
from .pool import СВОЙ as СВОЙ_СЛОВАРЬ
from .yandex_serp import RUSSIA, serp, serp_batch

BRAND, TARGET, ADJACENT = "brand", "target", "adjacent"
CORE, EDGE, DROP = "ядро", "периферия", "отбросить"

KIND_TITLES = {
    BRAND: "Брендовое ядро",
    TARGET: "Целевое ядро",
    ADJACENT: "Околоцелевые",
}

#: Дополнительные уточнения, которые превращают общеотраслевую брендовую фразу
#: в нашу. Точка подключения; по умолчанию пусто. Слова про услугу сюда брать
#: не стоит: если имя компании само слово из отрасли («гранит плитка»),
#: такие слова компанию от отрасли не отличают. Отличает привязка к объекту
#: ниши или собственный дескриптор компании.
BRAND_QUALIFIERS: tuple[str, ...] = ()


def _domain_label(domain: str | None) -> str:
    """Имя второго уровня: `shop.example.ru` → `example`. Дескриптор компании."""
    parts = config.normalize_domain(domain or "").split(".")
    return parts[-2] if len(parts) >= 2 else ""


def brand_qualifiers(stems: dict | None = None) -> tuple[str, ...]:
    """
    Чем брендовая фраза уточняется до нашей — по данным того проекта, чьё ядро.

    Уточнением служат объекты ниши проекта (то, что привязывает общий термин к
    его рынку) и собственный дескриптор компании — имя домена второго уровня
    (`example` у `example.ru`): его в чужом спросе почти не бывает.
    """
    st = stems or niche_stems()
    out: list[str] = list(BRAND_QUALIFIERS) + list(st.get("объекты") or ())
    pid = st.get("проект")
    if pid:
        try:
            project = storage.get_project(int(pid)) or {}
        except Exception:  # noqa: BLE001 — нет базы: без дескриптора
            project = {}
        label = _domain_label(project.get("domain"))
        if len(label) >= 3:
            out.append(label.lower())
    return tuple(dict.fromkeys(out))


#: Общие признаки коммерческой страницы — когда словарь ниши не задан, по ним
#: решается, специализирован ли топ. Проверяются по заголовку и сниппету.
COMMERCIAL_RE = re.compile(
    r"(купить|цен[аыу]\b|стоимост|заказать|услуг|под\s+ключ|прайс|доставк|"
    r"агентств|студи[яи]|компани[яи])",
    re.I,
)
#: Латиница в домене, которая выдаёт специалиста ниши. Точка подключения.
SPECIALIZED_DOMAIN: tuple[str, ...] = ()
SPECIALIZED_DOMAIN_NICHE: tuple[str, ...] = ()

#: Обучающие и энциклопедические площадки.
EDU_DOMAINS: tuple[str, ...] = (
    "wikipedia.org", "habr.com", "vc.ru", "dzen.ru", "practicum.yandex.ru",
    "skillbox.ru", "netology.ru", "sky.pro", "gb.ru", "otus.ru", "stackoverflow.com",
)
#: Разделы знаний у крупных SaaS и медиа: journal.tinkoff.ru, blog.calltouch.ru.
EDU_SUBDOMAIN_RE = re.compile(r"^(journal|blog|academy|school|wiki|help|edu|learn)\.", re.I)
EDU_TITLE_RE = re.compile(
    r"(что\s+так(ое|ие)|\bтоп[-\s]?\d|\bобзор|\bгайд|простыми словами|для чайников|"
    r"пошагов|инструкц|словар|энциклопед)",
    re.I,
)

#: Признаки того, что топ рассказывает про другое значение брендового термина,
#: а не про компанию. Точка подключения: для имени «гранит» это были бы
#: «горная порода», «месторождени», «твёрдость».
MODEL_MARKERS: tuple[str, ...] = ()

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}

LIMITS = (
    "Ноль в Wordstat означает «ниже порога отчётности сервиса», а не отсутствие спроса. "
    "Выдача меняется, состав топа верен на дату снятия. "
    "Ссылочный вес в расчёт не входит: источника по Яндексу нет. "
    "Веса факторов конкурентности — проектное решение, не измерение."
)


@dataclass
class CoreEntry:
    phrase: str
    kind: str                        # brand | target | adjacent
    freq: int | None
    source: str                      # seed | expansion | wordstat
    commercial: bool | None
    competition: float | None
    our_position: int | None
    top_domains: list[str] = field(default_factory=list)
    verdict: str = EDGE              # ядро | периферия | отбросить
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── разбор фразы ─────────────────────────────────────────────────────────────


def _key(text: str) -> str:
    """Ключ сравнения: только буквы и цифры. «гранит строй» и «гранитстрой» сходятся."""
    return re.sub(r"[^0-9a-zа-яё]+", "", text.lower())


def brand_keys(brand_terms: list[str]) -> list[str]:
    """Ключи брендовых фраз. По ним фраза опознаётся как брендовая."""
    keys = {_key(t) for t in brand_terms if _key(t)}
    # Длинные формы содержат короткие: «гранит строй москва» найдётся по «гранитстрой».
    return sorted(keys, key=len)


def is_brand(phrase: str, keys: list[str]) -> bool:
    k = _key(phrase)
    return any(key and key in k for key in keys)


def brand_qualified(phrase: str, stems: dict | None = None) -> set[str]:
    """Уточнения ниши во фразе. Пусто — фраза голая и собирает чужой спрос."""
    return _stem_hits(phrase, brand_qualifiers(stems))


def classify(phrase: str, keys: list[str], stems: dict | None = None) -> str:
    """
    Тип фразы по её составу. Брендовая — если содержит имя компании.
    Целевая — если есть и услуга, и объект. Иначе околоцелевая: тема шире.

    `stems` — словарь ниши проекта (`pool.niche_stems`). `None` — словарь не
    задан, и целевой фраза стать не может: судить «услуга плюс объект» нечем.
    Словарь нужен там, где ядро принадлежит конкретному проекту.
    """
    if is_brand(phrase, keys):
        return BRAND
    st = stems or niche_stems()
    services = _stem_hits(phrase, st["услуги"])
    objects = _stem_hits(phrase, st["объекты"])
    return TARGET if (services and objects) else ADJACENT


# ── частотность с диска ──────────────────────────────────────────────────────


def known_frequencies(project_id: int | None = None, region: int = RUSSIA) -> dict[str, int]:
    """
    Частотности, которые уже лежат на диске: снапшот Wordstat, таблица замеров,
    прошлое ядро проекта. Нужны, чтобы не платить второй раз за то же число.
    """
    out: dict[str, int] = {}
    _, snap = wordstat.latest_snapshot()
    out.update({k: v for k, v in snap.items() if k})

    with storage.connect() as conn:
        for row in conn.execute(
            "SELECT phrase, freq FROM keyword_freq WHERE region=? AND error IS NULL "
            "AND freq IS NOT NULL ORDER BY fetched_at",
            (str(region),),
        ):
            out[row["phrase"]] = int(row["freq"])

    if project_id:
        for row in storage.get_core(project_id):
            if row.get("freq") is not None:
                out[row["phrase"]] = int(row["freq"])
    return out


# ── сбор ─────────────────────────────────────────────────────────────────────


def collect(
    project_id: int,
    brand_terms: list[str],
    seed_terms: list[str],
    region: int = RUSSIA,
    min_freq: int = 0,
    limit_per_seed: int = 40,
    cache_writes: bool = False,
    progress: bool = True,
) -> list[CoreEntry]:
    """
    Собирает кандидатов ядра. Один запрос Wordstat на seed: расширения приходят
    тем же ответом. Отнесение к нише — через `pool`, второго фильтра нет.

    Порог частотности по умолчанию нулевой: конкретность важнее частотности,
    и отсечение по объёму выкинуло бы узкие фразы с нулём в Wordstat.
    """
    storage.init_db()
    keys = brand_keys(brand_terms)
    # Словарь ниши берётся у проекта, чьё это ядро. Считается один раз на сбор:
    # он не меняется по ходу и ходит в базу.
    stems = niche_stems(project_id)
    cache = known_frequencies(project_id, region)
    entries: dict[str, CoreEntry] = {}

    seeds: list[tuple[str, bool]] = [(t, True) for t in brand_terms] + [
        (t, False) for t in seed_terms
    ]

    for i, (seed, is_brand_seed) in enumerate(seeds, 1):
        f = wordstat.frequency(seed, region=str(region), num=200)

        if f.error:
            seed_freq = cache.get(seed)
            note = f"Wordstat не ответил: {f.error}"
        else:
            seed_freq = f.freq if f.freq is not None else cache.get(seed)
            note = ""
            if cache_writes:
                storage.save_frequency(f)

        if seed_freq is not None:
            cache[seed] = seed_freq

        kind = BRAND if is_brand_seed else classify(seed, keys, stems)
        entries.setdefault(
            seed,
            CoreEntry(
                phrase=seed, kind=kind, freq=seed_freq, source="seed",
                commercial=None, competition=None, our_position=None,
                verdict=EDGE,
                reason=note or "seed проекта, выдача не снималась",
            ),
        )

        taken = 0
        over_quota = 0
        for e in sorted(f.expansions, key=lambda x: (x.kind != "top", -x.freq)):
            phrase = (e.phrase or "").strip()
            if not phrase or phrase in entries:
                continue

            # Расширения брендового seed судятся как брендовые целиком, даже если
            # имени компании в них нет: Wordstat отдаёт по общему термину
            # соседний словарь отрасли, а не наш спрос.
            if is_brand_seed or is_brand(phrase, keys):
                entry = _judge_brand(phrase, e.freq, e.kind, stems)
            else:
                verdict = _judge(phrase, e.freq, e.kind, seed, min_freq,
                                 known=set(), stems=stems)
                entry = CoreEntry(
                    phrase=phrase, kind=classify(phrase, keys, stems), freq=e.freq,
                    source="expansion", commercial=None, competition=None,
                    our_position=None,
                    verdict=EDGE if verdict.accepted else DROP,
                    reason=verdict.reason,
                )

            if entry.verdict != DROP:
                if taken >= limit_per_seed:
                    # Квота выбрана. Строку всё равно сохраняем, но вердиктом
                    # DROP и с названной причиной: молчаливое обрезание читается
                    # как «больше ничего не нашлось», а это неправда.
                    over_quota += 1
                    entry.verdict = DROP
                    entry.reason = (
                        f"{entry.reason}; не вошло в квоту {limit_per_seed} "
                        f"на seed — поднять лимит: --limit-per-seed"
                    )
                    entries[phrase] = entry
                    continue
                taken += 1
                entry.reason = f"{entry.reason}; выдача не снималась"
            entries[phrase] = entry

        if progress:
            freq_txt = "нет данных" if seed_freq is None else str(seed_freq)
            tail = f", за квотой {over_quota}" if over_quota else ""
            print(
                f"[{i:02}/{len(seeds)}] {seed!r}: показов {freq_txt}, "
                f"взято расширений {taken}{tail}",
                file=sys.stderr,
            )

    deduped, dedup_report = dedup_forms(
        list(entries.values()), prefer={s for s, _ in seeds}
    )
    collapsed = sum(len(r.get("dropped", [])) for r in dedup_report if r["collapsed"])
    if progress and dedup_report:
        kept_apart = sum(1 for r in dedup_report if not r["collapsed"])
        print(
            f"Схлопнуто словоформ: {collapsed}. "
            f"Групп оставлено раздельно из-за расхождения частотности: {kept_apart}.",
            file=sys.stderr,
        )
    return deduped


def _judge_brand(phrase: str, freq: int, kind: str,
                 stems: dict | None = None) -> CoreEntry:
    """
    Брендовая фраза из расширений. Без уточнения ниши она может собирать чужой
    спрос, если имя компании — общий термин. Такие расширения отбрасываются
    здесь, а не проверяются выдачей: каждая проверка платная.
    """
    st = stems or niche_stems()
    qual = brand_qualified(phrase, st)
    if qual:
        return CoreEntry(
            phrase=phrase, kind=BRAND, freq=freq, source="expansion",
            commercial=None, competition=None, our_position=None, verdict=EDGE,
            reason=f"брендовая фраза с уточнением ниши: {', '.join(sorted(qual))}",
        )
    # Метка `brand_ambiguous` общая — по ней строку находит пересуд, — а
    # объяснение называет свою причину.
    почему = ("словарь ниши не задан, привязать фразу к рынку проекта нечем"
              if st.get("откуда") != СВОЙ_СЛОВАРЬ
              else "имя продукта без слова про его нишу — спрос может быть чужой")
    return CoreEntry(
        phrase=phrase, kind=BRAND, freq=freq, source="expansion",
        commercial=None, competition=None, our_position=None, verdict=DROP,
        reason=f"brand_ambiguous: брендовая фраза без уточнения ниши — {почему}",
    )


def _sort_key(e: CoreEntry) -> tuple:
    kinds = {BRAND: 0, TARGET: 1, ADJACENT: 2}
    verdicts = {CORE: 0, EDGE: 1, DROP: 2}
    return (kinds.get(e.kind, 9), verdicts.get(e.verdict, 9), -(e.freq or 0), e.phrase)


# ── схлопывание словоформ ────────────────────────────────────────────────────


def dedup_forms(
    entries: list[CoreEntry], prefer: set[str] | None = None
) -> tuple[list[CoreEntry], list[dict]]:
    """
    Схлопывает словоформы одной фразы в одну строку ядра.

    Wordstat приводит слова к начальной форме, поэтому «сквозная аналитика» и
    «сквозные аналитики» — один его запрос, и обе строки показывают одни и те
    же 2276 показов. Ключ группировки — мультимножество основ (`morph`).

    Схлопывание подтверждается данными, а не только правилом: если у форм
    частотность совпала, Wordstat действительно считает их одним запросом, и
    группа сливается. Если частотность разошлась — значит для Wordstat это
    разные запросы (так ведут себя «0 7» и «0.7»), и строки остаются обе,
    но получают взаимную пометку. Молча слить расходящиеся числа нельзя:
    это была бы подмена измерения правилом.

    Возвращает (строки, отчёт о схлопывании).
    """
    groups: dict[str, list[CoreEntry]] = {}
    for e in entries:
        groups.setdefault(morph.stem_key(e.phrase) or e.phrase.lower(), []).append(e)

    out: list[CoreEntry] = []
    report: list[dict] = []

    for key, group in groups.items():
        if len(group) == 1:
            out.append(group[0])
            continue

        measured = {e.freq for e in group if e.freq is not None}
        variants = [e.phrase for e in group]

        if len(measured) > 1:
            # Разошлись числа — Wordstat различает эти формы. Не сливаем.
            for e in group:
                others = ", ".join(
                    f"{o.phrase!r} {o.freq}" for o in group if o is not e
                )
                e.reason = f"{e.reason}; рядом словоформа с другой частотностью: {others}"
                out.append(e)
            report.append({
                "key": key, "collapsed": False, "kept": variants,
                "why": "частотность форм разошлась — для Wordstat это разные запросы",
            })
            continue

        by_verdict = {CORE: 0, EDGE: 1, DROP: 2}
        keep_phrase = morph.pick_canonical(variants, prefer=prefer)
        keeper = next(e for e in group if e.phrase == keep_phrase)

        # Вердикт группы — самый сильный из встреченных: если хоть одна форма
        # прошла проверку выдачей, результат относится ко всей группе.
        best = min(group, key=lambda e: by_verdict.get(e.verdict, 9))
        if best is not keeper and by_verdict.get(best.verdict, 9) < by_verdict.get(
            keeper.verdict, 9
        ):
            keeper.verdict = best.verdict
            keeper.reason = best.reason
            keeper.competition = best.competition
            keeper.our_position = best.our_position
            keeper.top_domains = best.top_domains
            keeper.commercial = best.commercial

        dropped = [p for p in variants if p != keep_phrase]
        keeper.reason = (
            f"{keeper.reason}; схлопнуто словоформ: {len(dropped)} "
            f"({', '.join(repr(p) for p in sorted(dropped))}), частотность у всех "
            f"{next(iter(measured)) if measured else 'нет данных'}"
        )
        out.append(keeper)

        # Схлопнутые формы не выбрасываются, а возвращаются вердиктом
        # «отбросить». Причина в хранилище: `save_core_entries` — upsert, он
        # ничего не удаляет. Выброси форму из результата — и её старая строка
        # осталась бы в базе нетронутой, то есть схлопывание не было бы видно
        # после пересборки. Заодно решение остаётся проверяемым: видно, во что
        # форму слили.
        for twin in group:
            if twin is keeper:
                continue
            twin.verdict = DROP
            twin.reason = (
                f"словоформа: та же фраза, что {keep_phrase!r} — у Wordstat это "
                "один запрос с одной частотностью. Решение смотреть там"
            )
            out.append(twin)
        report.append({
            "key": key, "collapsed": True, "kept": [keep_phrase], "dropped": dropped,
            "why": "одинаковые основы и одинаковая частотность — один запрос Wordstat",
        })

    return sorted(out, key=_sort_key), report


# ── проверка выдачей ─────────────────────────────────────────────────────────


def _priority(entries: list[CoreEntry]) -> list[CoreEntry]:
    """
    Очередь на проверку. Сначала то, что назвал оператор (seed), внутри —
    brand, затем target с ненулевой частотностью, затем остальное.
    """
    kinds = {BRAND: 0, TARGET: 1, ADJACENT: 2}

    def key(e: CoreEntry) -> tuple:
        return (
            0 if e.source == "seed" else 1,
            kinds.get(e.kind, 9),
            0 if (e.freq or 0) > 0 else 1,
            -(e.freq or 0),
            e.phrase,
        )

    return sorted([e for e in entries if e.verdict != DROP], key=key)


#: Признаки сайта ЗАКАЗЧИКА услуги, а не конкурента. Точка подключения.
#: Различие принципиальное, когда проект продаёт услуги бизнесу: топ из
#: исполнителей означает коммерческий спрос на услугу, топ из самих заказчиков —
#: что запрос про их продукт. Пример для поставщика оборудования ресторанам:
#: `re.compile(r"(меню\s+ресторан|забронировать\s+стол|доставк\w+\s+ед)", re.I)`.
CLIENT_TITLE_RE: re.Pattern | None = None

#: Навигационные и кадровые запросы. Их интент — связаться с конкретной
#: компанией либо найти работу, а не купить услугу. Сигнал сидит в самой
#: фразе, и выдачей его не поймать: топ там законно состоит из сайтов самих компаний.
NAVIGATIONAL_RE = re.compile(
    r"\b(контакт\w*|телефон\w*|адрес\w*|email|почт[аы]|реквизит\w*"
    r"|вакансии|резюме|работа|зарплат\w*|отзывы\s+сотрудник\w*"
    r"|личный\s+кабинет|войти|вход)\b",
    re.I,
)


def _domain_profile(
    docs, niche_only: bool = False, stems: dict | None = None
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """
    Домены топа и то, кем они являются: специалист ниши, обучающий, прочее.
    `niche_only` сужает признак специализации до объекта ниши — так меряются
    брендовые фразы, где слово про услугу ничего не доказывает.

    Специализация судится словарём проекта; без словаря — общими
    коммерческими словами (`COMMERCIAL_RE`), и тогда `niche_only` ничего не
    находит: объекта ниши назвать нечем.
    """
    st = stems or niche_stems()
    words = tuple(st.get("объекты") or ()) if niche_only else \
        tuple(st.get("услуги") or ()) + tuple(st.get("объекты ниши") or ())
    spec_domains = SPECIALIZED_DOMAIN_NICHE if niche_only else SPECIALIZED_DOMAIN

    def _spec_text(text: str) -> bool:
        if words:
            return bool(_stem_hits(text, words))
        return (not niche_only) and bool(COMMERCIAL_RE.search(text))

    domains: list[str] = []
    specialized: list[str] = []
    educational: list[str] = []
    model: list[str] = []
    clients: list[str] = []

    for d in docs:
        dom = _norm(d.domain)
        text = f"{d.title or ''} {d.passage or ''}"
        domains.append(dom)

        is_edu = (
            any(dom == e or dom.endswith("." + e) for e in EDU_DOMAINS)
            or bool(EDU_SUBDOMAIN_RE.match(dom))
            or bool(EDU_TITLE_RE.search(d.title or ""))
        )
        is_spec = (
            _spec_text(text)
            or any(m in dom for m in spec_domains)
        )
        if is_spec:
            specialized.append(dom)
        if is_edu:
            educational.append(dom)
        if any(m in text.lower() for m in MODEL_MARKERS):
            model.append(dom)
        if CLIENT_TITLE_RE is not None and CLIENT_TITLE_RE.search(text):
            clients.append(dom)
    return domains, specialized, educational, model, clients


def _evidence(specialized: list[str], educational: list[str], total: int) -> str:
    named = ", ".join(dict.fromkeys(specialized[:3])) or "нет"
    return (
        f"специализированных {len(specialized)} из {total} ({named}), "
        f"обучающих и энциклопедических {len(educational)}"
    )


def _apply_serp(entry: CoreEntry, result, our_domain: str | None, top: int = 10,
                stems: dict | None = None) -> None:
    """Проставляет по фразе доказательство и вердикт. Мутирует запись."""
    if result.error:
        entry.reason = f"выдача не получена: {result.error}. Вердикт отложен"
        return

    comp = score_serp(result, our_domain=our_domain, top=top)
    docs = [d for d in result.docs if not _is_pseudo(d)][:top]
    if not docs:
        entry.reason = "в выдаче нет пригодных документов, вердикт отложен"
        return

    domains, specialized, educational, model, clients = _domain_profile(
        docs, niche_only=(entry.kind == BRAND), stems=stems
    )
    entry.top_domains = domains
    entry.our_position = comp.our_position
    entry.competition = None if comp.error else comp.score

    # Топ из сайтов заказчиков означает, что запрос ведёт к ним, а не к
    # покупателям услуги. Такой спрос коммерческим для проекта не является.
    client_heavy = len(clients) * 2 >= len(docs)
    navigational = bool(NAVIGATIONAL_RE.search(entry.phrase))
    entry.commercial = (
        len(specialized) > len(educational) and not client_heavy and not navigational
    )

    proof = _evidence(specialized, educational, len(docs))
    if navigational:
        proof += (
            "; запрос навигационный — ведёт к контактам или вакансиям компании, "
            "услугу так не покупают"
        )
    if client_heavy:
        named = ", ".join(dict.fromkeys(clients[:3]))
        proof += (
            f"; сайтов заказчиков {len(clients)} из {len(docs)} ({named}) — "
            "это клиенты, а не конкуренты"
        )

    if entry.kind == BRAND:
        _verdict_brand(entry, proof, model, docs, stems)
    elif entry.kind == TARGET:
        _verdict_target(entry, proof)
    else:
        _verdict_adjacent(entry, proof)


def _verdict_brand(entry: CoreEntry, proof: str, model: list[str], docs,
                   stems: dict | None = None) -> None:
    if entry.our_position:
        entry.verdict = CORE
        entry.reason = f"мы в топе на позиции {entry.our_position}; {proof}"
        return

    qual = brand_qualified(entry.phrase, stems)
    if qual:
        entry.verdict = CORE
        entry.reason = (
            f"уточнение ниши во фразе: {', '.join(sorted(qual))}; "
            f"нас в топе нет; {proof}"
        )
        return

    named = ", ".join(dict.fromkeys((model or [_norm(d.domain) for d in docs])[:3]))
    other = (f"признаки другого значения термина у {len(model)} из {len(docs)} "
             f"страниц ({named}); " if MODEL_MARKERS else f"в топе: {named}; ")
    entry.verdict = EDGE
    entry.reason = (
        f"brand_ambiguous: нас в топе нет, уточнения ниши во фразе нет; "
        f"{other}{proof}. В чистое брендовое ядро не берём"
    )


def _verdict_target(entry: CoreEntry, proof: str) -> None:
    if entry.commercial:
        entry.verdict = CORE
        entry.reason = f"топ коммерческий: {proof}"
        return

    entry.verdict = EDGE
    # Причина должна совпадать с тем, что произошло. Навигационный запрос
    # и запрос с топом из заказчиков — это не «топ обучающий».
    if NAVIGATIONAL_RE.search(entry.phrase):
        entry.reason = f"навигационный запрос, покупателя услуг не приводит: {proof}"
    elif "сайтов заказчиков" in proof:
        entry.reason = f"топ занят клиентами, а не конкурентами: {proof}"
    else:
        entry.kind = ADJACENT
        entry.reason = f"топ обучающий, тема шире услуги: {proof}"


def _verdict_adjacent(entry: CoreEntry, proof: str) -> None:
    entry.verdict = EDGE
    if entry.commercial:
        entry.reason = f"топ коммерческий, но во фразе нет объекта ниши: {proof}"
    else:
        entry.reason = f"топ обучающий, там ищет не покупатель услуги: {proof}"


#: Сколько снимков на брендовую фразу. Выдача Яндекса нестабильна: пять
#: одинаковых запросов подряд могут дать 11, 6, 5, 6, 5, и одиночный снимок
#: позицией не является.
#: Для брендовых фраз это критично: вердикт «нас в топе нет» переводит фразу в
#: brand_ambiguous, и один неудачный снимок вычёркивает живую фразу из ядра.
#: У остальных типов вердикт решает состав топа, а не наша позиция, поэтому
#: там снимок по умолчанию один — каждый повтор платный.
BRAND_REPEATS = 3


def _median_position(values: list[int | None]) -> int | None:
    """Медиана позиций — то же правило, что в трекере, из одного места:
    своя копия формулы однажды уже разошлась с трекерской."""
    from .tracker import _median

    return _median(values)


def _our_position(result, our_domain: str | None) -> int | None:
    if not our_domain:
        return None
    target = our_domain.lower().removeprefix("www.")
    for d in getattr(result, "docs", []):
        if getattr(d, "is_wizard", False):
            continue
        if (getattr(d, "domain", "") or "").lower().removeprefix("www.") == target:
            return getattr(d, "organic_position", None) or d.position
    return None


def validate(
    entries: list[CoreEntry],
    our_domain: str | None = None,
    limit: int = 60,
    region: int = RUSSIA,
    top: int = 10,
    progress: bool = True,
    repeats: int = 1,
    brand_repeats: int = BRAND_REPEATS,
    stems: dict | None = None,
) -> list[CoreEntry]:
    """
    Снимает выдачу и выносит вердикт. Каждый вызов платный, поэтому лимит
    обязателен: за его пределами вердикт остаётся отложенным, и это видно.

    Брендовые фразы снимаются несколькими снимками, и в вердикт идёт медиана
    позиции: одиночный снимок выдачи позицией не является. Представителем
    состава топа берётся тот снимок, чья позиция совпала с медианой, — так
    вердикт и позиция описывают одну и ту же выдачу, а не разные.
    """
    queue = _priority(entries)[: max(limit, 0)]
    if not queue:
        return entries
    our_domain = config.domain_or_none(our_domain)

    plain = [e for e in queue if e.kind != BRAND or brand_repeats <= 1]
    branded = [e for e in queue if e.kind == BRAND and brand_repeats > 1]

    if plain:
        serps = serp_batch(
            [e.phrase for e in plain], region=region, n=top, progress=progress
        )
        for entry, result in zip(plain, serps):
            _apply_serp(entry, result, our_domain, top=top, stems=stems)
            if repeats <= 1 and not result.error:
                entry.reason = f"{entry.reason}; снимок один"

    for entry in branded:
        shots = [
            serp(entry.phrase, region=region, n=top) for _ in range(brand_repeats)
        ]
        ok = [r for r in shots if not r.error]
        if not ok:
            _apply_serp(entry, shots[0], our_domain, top=top, stems=stems)
            continue

        samples = [_our_position(r, our_domain) for r in ok]
        median = _median_position(samples)
        representative = next(
            (r for r, s in zip(ok, samples) if s == median), ok[0]
        )
        _apply_serp(entry, representative, our_domain, top=top, stems=stems)
        entry.our_position = median

        seen = ", ".join("нет" if s is None else str(s) for s in samples)
        entry.reason = (
            f"{entry.reason}; снимков {len(ok)}, позиции [{seen}], "
            f"в вердикт медиана {median if median is not None else 'нет в топе'}"
        )
        if progress:
            print(
                f"  бренд {entry.phrase!r}: [{seen}] -> "
                f"{median if median is not None else 'нет в топе'}",
                file=sys.stderr,
            )

    checked = {id(e) for e in queue}
    for e in entries:
        if id(e) not in checked and e.verdict != DROP:
            e.reason = f"{e.reason}: вне лимита проверки {limit}"
    return sorted(entries, key=_sort_key)


# ── сборка ───────────────────────────────────────────────────────────────────


def _slug(text: str) -> str:
    out = "".join(TRANSLIT.get(ch, ch) for ch in text.lower().strip())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return out or "project"


def _terms(project: dict, field_name: str) -> list[str]:
    try:
        value = json.loads(project.get(field_name) or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def summarize(entries: list[CoreEntry]) -> dict:
    counts: dict[str, dict[str, int]] = {}
    for kind in (BRAND, TARGET, ADJACENT):
        rows = [e for e in entries if e.kind == kind]
        counts[kind] = {
            "всего": len(rows),
            CORE: sum(1 for e in rows if e.verdict == CORE),
            EDGE: sum(1 for e in rows if e.verdict == EDGE),
            DROP: sum(1 for e in rows if e.verdict == DROP),
        }
    counts["итого"] = {
        "всего": len(entries),
        CORE: sum(1 for e in entries if e.verdict == CORE),
        EDGE: sum(1 for e in entries if e.verdict == EDGE),
        DROP: sum(1 for e in entries if e.verdict == DROP),
    }
    return counts


def build(
    project_id: int,
    brand_terms: list[str] | None = None,
    seed_terms: list[str] | None = None,
    region: int | None = None,
    our_domain: str | None = None,
    validate_top: int = 60,
    collect_only: bool = False,
    apply: bool = False,
    write_files: bool = True,
    progress: bool = True,
    limit_per_seed: int = 40,
    brand_repeats: int = BRAND_REPEATS,
) -> dict:
    """
    Полный проход: сбор → проверка выдачей → отчёт. Без `apply` в базу не пишет.
    Семена берутся из аргументов, иначе из проекта.
    """
    storage.init_db()
    project = storage.get_project(project_id)
    if not project:
        raise SystemExit(f"проекта id={project_id} в базе нет: yaseo storage --project-add ИМЯ --domain example.ru")

    region = region if region is not None else int(project.get("region") or RUSSIA)
    our_domain = our_domain or project["domain"]
    brands = brand_terms if brand_terms is not None else _terms(project, "brand_terms")
    seeds = seed_terms if seed_terms is not None else _terms(project, "seed_terms")
    if not brands and not seeds:
        raise SystemExit(
            "нет семян. Передайте --brand и --seed или запишите их в проект "
            "(storage.set_project_terms)."
        )

    stems = niche_stems(project_id)
    entries = collect(
        project_id, brands, seeds, region=region,
        limit_per_seed=limit_per_seed, cache_writes=apply, progress=progress,
    )
    validated = 0
    if not collect_only and validate_top > 0:
        validated = min(len(_priority(entries)), validate_top)
        entries = validate(
            entries, our_domain=our_domain, limit=validate_top,
            region=region, progress=progress, brand_repeats=brand_repeats,
            stems=stems,
        )

    day = datetime.now().strftime("%Y-%m-%d")
    slug = _slug(project["name"])
    core = {
        "project": project["name"],
        "project_id": project_id,
        "domain": our_domain,
        "region": region,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "day": day,
        "brand_terms": brands,
        "seed_terms": seeds,
        # Каким словарём ниши судили это ядро. Пишется в отчёт, потому что
        # «свой словарь» и «словарь не задан» дают разный состав ядра, а без
        # подписи выглядели бы одинаково.
        "словарь ниши": {
            "откуда": stems["откуда"],
            "почему": stems["почему"],
            "основ услуг": len(stems["услуги"]),
            "основ объекта": len(stems["объекты"]),
        },
        "validated": validated,
        "validate_limit": 0 if collect_only else validate_top,
        "summary": summarize(entries),
        "entries": [e.to_dict() for e in entries],
        "limits": LIMITS,
        "files": {},
        "saved": 0,
    }

    # Молчаливое сохранение неизмеренного ядра запрещено. Если Wordstat не
    # отвечал подряд, ядро сохранилось бы без единой частотности, и по нему
    # человек сделал бы вывод «спроса нет», хотя измерения просто не было.
    # «Нет данных» — это не ноль, и одно за другое не выдаётся.
    measured = sum(1 for e in entries if getattr(e, "freq", None) is not None)
    core["measured"] = measured
    core["without_frequency"] = len(entries) - measured
    if entries and measured / len(entries) < 0.5:
        core["skipped_save"] = (
            f"не сохраняю: частотность есть только у {measured} строк из "
            f"{len(entries)}. Похоже, Wordstat не отвечал — это «нет данных», "
            "а не «нет спроса». Повторить сбор позже."
        )
        apply = False

    if apply:
        storage.set_project_terms(project_id, brands, seeds)
        core["saved"] = storage.save_core_entries(project_id, entries)

    if write_files:
        out_dir = config.reports_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / f"core-{slug}-{day}.json"
        json_path.write_text(json.dumps(core, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path = out_dir / f"semantic-core-{slug}-{day}.md"
        md_path.write_text(render_markdown(core), encoding="utf-8")
        core["files"] = {"json": str(json_path), "md": str(md_path)}

    return core


# ── пересуд по новому словарю ────────────────────────────────────────────────

#: Причины, в которых решение принял словарь ниши. Только такие строки пересуд
#: и трогает: у них вердикт выведен из состава фразы, а состав судится словарём.
СЛОВАРНЫЕ_ПРИЧИНЫ: tuple[str, ...] = (
    "ассоциация вне ниши",
    "ассоциация в нише",
    "но нет слова про услугу",
    "название чужого объекта",
    "brand_ambiguous",
    "брендовая фраза с уточнением ниши",
    "инструментальный спрос",
    "уточнение про чужой продукт",
)

#: Причины, которые словарём не отменяются, даже если стоят в той же строке.
#: Мусорный хвост остаётся мусорным при любом словаре; квота — это лимит сбора,
#: а не суждение о фразе; словоформа отослана к своему близнецу, и решение по
#: ней принимает он. Пересудить такую строку значило бы подменить её причину.
НЕ_СЛОВАРНЫЕ_ПРИЧИНЫ: tuple[str, ...] = (
    "справочный или развлекательный",
    "не вошло в квоту",
    "словоформа:",
    "уточнение: содержит исходную фразу",
    "seed проекта",
)

#: Пометка пересуда в причине. Пишется одна и та же, чтобы повторный прогон
#: заменял её, а не наращивал: причина — объяснение, а не журнал.
ПОМЕТКА_ПЕРЕСУДА = "; пересуд по словарю проекта"


def _serp_taken(row: dict) -> bool:
    """
    Снималась ли по строке выдача. Следом считается любое поле, которое кроме
    выдачи взяться неоткуда: состав топа, наша позиция, конкурентность.

    Формулировка в `reason` («выдача не снималась») тут вторична и одна не
    решает: пометка может остаться в тексте, когда состав топа уже записан.
    Верить тексту против данных — это переписать измеренное правилом.
    """
    return bool(row.get("top_domains")) or (row.get("our_position") is not None) \
        or (row.get("competition") is not None)


def _dictionary_judged(row: dict) -> bool:
    """Решил ли вердикт этой строки словарь ниши, а не что-то другое."""
    reason = row.get("reason") or ""
    if any(mark in reason for mark in НЕ_СЛОВАРНЫЕ_ПРИЧИНЫ):
        return False
    return any(mark in reason for mark in СЛОВАРНЫЕ_ПРИЧИНЫ)


def _instrumental_stands(phrase: str, reason: str, stems: dict) -> bool:
    """
    Остаётся ли в силе отказ «инструментальный спрос» при новом словаре.

    Правило `pool._judge` отбивает такую фразу связкой из двух условий: seed был
    широкий И объекта ниши во фразе нет. Первое условие проверить нечем — seed в
    `core_entries` не хранится, и выдумывать его нельзя. Второе словарь меняет,
    и только его мы и перепроверяем: появился объект — связка распалась и фразу
    судят заново, не появился — отказ стоит.
    """
    if "инструментальный спрос" not in reason:
        return False
    return not _stem_hits(phrase, stems["объекты ниши"])


def _counts(rows: list[dict]) -> dict[str, int]:
    """Сводка «тип × вердикт» одной плоской таблицей: ключ «kind/verdict»."""
    out: dict[str, int] = {}
    for r in rows:
        key = f"{r.get('kind')}/{r.get('verdict')}"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def rejudge(project_id: int, apply: bool = False) -> dict:
    """
    Пересудить уже накопленное ядро проекта новым словарём ниши. Бесплатно.

    Словарь ниши у проекта сменился, а строки ядра разложены прежним словарём.
    Пересобирать ядро ради этого дорого: сбор — это Wordstat, проверка —
    выдача, и то и другое деньги. При этом сами фразы, частотности и
    доказательства уже лежат в базе, за них уже заплачено. Пересуд читает базу
    и не ходит наружу ни разу.

    Три правила честности, и они важнее удобства.

    1. Тип (`brand` / `target` / `adjacent`) пересчитывается у всех строк: тип
       выводится из состава фразы, а состав судится словарём. Строки, потерявшие
       `brand`, считаются отдельно: их тип был назначен происхождением (пришли
       расширением брендового seed), и снять его молча нельзя. Это не мелочь:
       Wordstat лемматизирует похожие слова в одно, и имя продукта, совпавшее
       с частью общего слова, делает брендовыми сотни чужих строк.

    2. Вердикт пересчитывается ТОЛЬКО там, где его вынес словарь и только
       словарь: причина строки названа в `СЛОВАРНЫЕ_ПРИЧИНЫ` и не перебита
       причиной из `НЕ_СЛОВАРНЫЕ_ПРИЧИНЫ`. Уточнение, мусорный хвост, квота и
       словоформа судились не словарём — их вердикт остаётся как есть.

    3. Строки, по которым выдача СНИМАЛАСЬ, вердикта не меняют вовсе. Их
       вердикт — измерение, а измерение новым правилом не переписывают. Такие
       строки попадают в «требует пересуда» и считаются числом: решение по ним
       принимает человек, заказав повторную проверку выдачей.

    Без `apply` только показывает, что изменится.
    """
    storage.init_db()
    project = storage.get_project(project_id)
    if not project:
        raise SystemExit(f"проекта id={project_id} в базе нет")

    stems = niche_stems(project_id)
    keys = brand_keys(_terms(project, "brand_terms"))
    rows = storage.get_core(project_id)

    типы: list[dict] = []          # сменился тип
    вердикты: list[dict] = []      # сменился вердикт
    бренд_снят: list[dict] = []    # потеряли brand, назначенный происхождением
    пересуд: list[dict] = []       # выдача снималась — вердикт не трогаем
    записи: list[tuple] = []
    стало: list[dict] = []

    for row in rows:
        phrase = row["phrase"]
        было_тип, было_вердикт = row.get("kind"), row.get("verdict")
        причина = (row.get("reason") or "").split(ПОМЕТКА_ПЕРЕСУДА)[0]

        стал_тип = classify(phrase, keys, stems)

        # Брендовое происхождение снимается только своим словарём и только
        # целиком. Тип `brand` у строки, в которой имени компании нет, назначен
        # происхождением: она пришла расширением брендового seed. Разобрать её
        # по составу — значит применить к ней правило, по которому её не судили:
        # мусорные расширения общего брендового термина уехали бы в adjacent и
        # раздули околоцелевые.
        #
        # Перебить происхождение вправе только полное попадание в СВОЙ словарь
        # проекта: и услуга, и объект (`TARGET`). Половина попадания слабее
        # происхождения. Без своего словаря не перебивается ничего.
        if было_тип == BRAND and not is_brand(phrase, keys):
            свой_и_целевая = (stems.get("откуда") == СВОЙ_СЛОВАРЬ
                              and стал_тип == TARGET)
            if not свой_и_целевая:
                стал_тип = BRAND

        стал_вердикт, новая_причина = было_вердикт, причина

        серп = _serp_taken(row)
        if not серп and _dictionary_judged(row):
            if стал_тип == BRAND:
                qual = brand_qualified(phrase, stems)
                стал_вердикт = EDGE if qual else DROP
                новая_причина = (
                    f"брендовая фраза с уточнением ниши: {', '.join(sorted(qual))}"
                    if qual else
                    "brand_ambiguous: брендовая фраза без уточнения ниши"
                )
            elif _instrumental_stands(phrase, причина, stems):
                # Инструментальный спрос («как настроить», «интеграция») отбит
                # не словарём в одиночку, а связкой «широкий seed И ни одного
                # объекта ниши во фразе». Seed в базе не лежит — восстановить
                # его из фразы нельзя. Зато вторую половину связки проверить
                # можно, и словарь меняет ровно её: пока объекта во фразе нет,
                # отказ остаётся в силе. Без этой проверки строки про настройку
                # чужих инструментов уезжали бы из «отбросить» в «периферию»,
                # если слово инструмента есть в словаре услуг.
                стал_вердикт, новая_причина = было_вердикт, причина
            else:
                взять, почему = niche_decision(phrase, stems)
                стал_вердикт = EDGE if взять else DROP
                новая_причина = почему
            if "выдача не снималась" not in новая_причина:
                новая_причина += "; выдача не снималась"

        строка = {"фраза": phrase, "частотность": row.get("freq"),
                  "было": f"{было_тип}/{было_вердикт}",
                  "стало": f"{стал_тип}/{стал_вердикт}"}

        if серп and (стал_тип != было_тип):
            # Вердикт не трогаем, но молчать нельзя: он получен под прежним
            # словарём, а тип фразы под новым — другой.
            пересуд.append({**строка, "стало": f"{стал_тип}/{было_вердикт}",
                            "вердикт": было_вердикт})

        if стал_тип == было_тип and стал_вердикт == было_вердикт:
            стало.append({"kind": было_тип, "verdict": было_вердикт})
            continue

        if стал_тип != было_тип:
            типы.append(строка)
            if было_тип == BRAND:
                бренд_снят.append(строка)
        if стал_вердикт != было_вердикт:
            вердикты.append(строка)

        пометка = (f"{ПОМЕТКА_ПЕРЕСУДА} {datetime.now().strftime('%d.%m.%Y')}: "
                   f"{было_тип}/{было_вердикт} → {стал_тип}/{стал_вердикт}, "
                   f"словарь — {stems['откуда']}")
        записи.append((стал_тип, стал_вердикт, новая_причина + пометка,
                       project_id, phrase))
        стало.append({"kind": стал_тип, "verdict": стал_вердикт})

    if apply and записи:
        with storage.connect() as conn:
            conn.executemany(
                "UPDATE core_entries SET kind=?, verdict=?, reason=? "
                "WHERE project_id=? AND phrase=?",
                записи,
            )

    return {
        "проект": int(project_id),
        "название": project["name"],
        "словарь": {"откуда": stems["откуда"], "почему": stems["почему"],
                    "основ услуг": len(stems["услуги"]),
                    "основ объекта": len(stems["объекты"])},
        "применено": apply,
        "строк всего": len(rows),
        "сменился тип": типы,
        "сменился вердикт": вердикты,
        "снято брендовое происхождение": бренд_снят,
        "требует пересуда": пересуд,
        "было": _counts([dict(r) for r in rows]),
        "стало": _counts(стало),
    }


# ── Markdown ─────────────────────────────────────────────────────────────────


def _cell(text: str, width: int = 70) -> str:
    t = (text or "").replace("|", "/").replace("\n", " ").strip()
    return t if len(t) <= width else t[: width - 1] + "…"


def _freq_cell(value) -> str:
    if value is None:
        return "нет данных"
    if value == 0:
        return "0 (ниже порога)"
    return f"{int(value):,}".replace(",", " ")


def _pos_cell(value) -> str:
    return "нет в топе" if not value else str(value)


def _comp_cell(value) -> str:
    return "нет данных" if value is None else f"{value:.0f}"


def _moment(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError):
        return iso
    return dt.strftime("%d.%m.%Y %H:%M")


def render_markdown(core: dict) -> str:
    rows = core.get("entries") or []
    summary = core.get("summary") or {}
    total = summary.get("итого", {})

    lines = [
        f"# Семантическое ядро: {core['project']} ({core['domain']})",
        "",
        f"Собрано {_moment(core['generated_at'])} · регион {core['region']} · "
        f"фраз {total.get('всего', 0)}, из них проверено выдачей {core.get('validated', 0)}.",
        "",
        "Ядро принадлежит проекту: сменится сайт — сменятся семена и состав ядра. "
        "Решает состав выдачи, а не объём спроса.",
        "",
        "## Сводка",
        "",
        "| Тип | Всего | Ядро | Периферия | Отброшено |",
        "|---|---:|---:|---:|---:|",
    ]
    titles = {**KIND_TITLES, "итого": "Итого"}
    for kind in (BRAND, TARGET, ADJACENT, "итого"):
        s = summary.get(kind, {})
        lines.append(
            f"| {titles[kind]} | {s.get('всего', 0)} | {s.get(CORE, 0)} | "
            f"{s.get(EDGE, 0)} | {s.get(DROP, 0)} |"
        )

    if core.get("brand_terms"):
        lines += ["", f"**Брендовые семена:** {', '.join(core['brand_terms'])}"]
    if core.get("seed_terms"):
        lines += ["", f"**Семена по роду деятельности:** {', '.join(core['seed_terms'])}"]
    словарь = core.get("словарь ниши")
    if словарь:
        lines += [
            "",
            f"**Словарь ниши:** {словарь['откуда']} — основ услуг "
            f"{словарь['основ услуг']}, основ объекта {словарь['основ объекта']}. "
            f"{словарь['почему']}",
        ]

    for kind in (BRAND, TARGET, ADJACENT):
        picked = [r for r in rows if r["kind"] == kind and r["verdict"] != DROP]
        lines += ["", f"## {KIND_TITLES[kind]} ({len(picked)})", ""]
        if not picked:
            lines.append("Пусто.")
            continue
        lines += [
            "| Фраза | Показов | Наша позиция | Конкурентность | Вердикт | Доказательство |",
            "|---|---:|---|---:|---|---|",
        ]
        for r in sorted(picked, key=lambda x: (x["verdict"] != CORE, -(x["freq"] or 0))):
            lines.append(
                f"| {_cell(r['phrase'], 60)} | {_freq_cell(r['freq'])} | "
                f"{_pos_cell(r['our_position'])} | {_comp_cell(r['competition'])} | "
                f"{r['verdict']} | {_cell(r['reason'], 180)} |"
            )

    dropped = [r for r in rows if r["verdict"] == DROP]
    lines += ["", f"## Отброшено ({len(dropped)})", ""]
    if dropped:
        lines += ["| Фраза | Показов | Тип | Причина |", "|---|---:|---|---|"]
        for r in sorted(dropped, key=lambda x: -(x["freq"] or 0)):
            lines.append(
                f"| {_cell(r['phrase'], 60)} | {_freq_cell(r['freq'])} | "
                f"{r['kind']} | {_cell(r['reason'], 140)} |"
            )
    else:
        lines.append("Ничего не отброшено.")

    unchecked = [r for r in rows if r["verdict"] != DROP and not r["top_domains"]]
    lines += [
        "",
        "## Ограничения",
        "",
        "- Частотность: Yandex Wordstat API v2. Ноль означает «ниже порога отчётности сервиса», "
        "а не отсутствие спроса. `нет данных` — значение не измерялось.",
        f"- Выдача: Yandex Search API v2, снята {_moment(core['generated_at'])}, "
        f"регион {core['region']}. Топ меняется, вердикт верен на эту дату.",
        "- Ссылочный вес в расчёт не входит: источника по Яндексу нет, и вывод о нём не делается.",
        "- Веса факторов конкурентности — проектное решение, не измерение. "
        "Разбор по факторам возвращает `yaseo.competition`.",
        f"- Вердикт отложен у {len(unchecked)} фраз: выдача по ним не снималась "
        f"(лимит проверки {core.get('validate_limit', 0)}, каждый запрос платный).",
        "- В топе стоят конкуренты, а не покупатели. Отсутствие заказчиков в выдаче — норма.",
        "",
    ]
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────


def _print_summary(core: dict) -> None:
    s = core["summary"]
    print(f"\nПроект: {core['project']} ({core['domain']}), регион {core['region']}")
    print(f"{'тип':<16} {'всего':>6} {'ядро':>6} {'периферия':>10} {'отброшено':>10}")
    titles = {**KIND_TITLES, "итого": "Итого"}
    for kind in (BRAND, TARGET, ADJACENT, "итого"):
        row = s.get(kind, {})
        print(
            f"{titles[kind]:<16} {row.get('всего', 0):>6} {row.get(CORE, 0):>6} "
            f"{row.get(EDGE, 0):>10} {row.get(DROP, 0):>10}"
        )
    print(f"Проверено выдачей: {core.get('validated', 0)}")


def _print_table(core: dict, limit: int) -> None:
    rows = [r for r in core["entries"] if r["verdict"] != DROP]
    order = {CORE: 0, EDGE: 1}
    rows.sort(key=lambda r: ({BRAND: 0, TARGET: 1, ADJACENT: 2}[r["kind"]],
                             order.get(r["verdict"], 9), -(r["freq"] or 0)))
    print(f"\n{'тип':<9} {'показов':>15} {'поз':>10} {'конк':>5} {'вердикт':<11} фраза")
    for r in rows[:limit]:
        print(
            f"{r['kind']:<9} {_freq_cell(r['freq']):>15} {_pos_cell(r['our_position']):>10} "
            f"{_comp_cell(r['competition'])[:5]:>5} {r['verdict']:<11} {r['phrase'][:46]}"
        )
    if len(rows) > limit:
        print(f"... ещё {len(rows) - limit} строк. Полный список — в отчётах.")


def _print_rejudge(res: dict, show: int = 40) -> None:
    сл = res["словарь"]
    print(f"\nПроект {res['проект']} «{res['название']}», строк ядра {res['строк всего']}")
    print(f"Словарь ниши: {сл['откуда']} — основ услуг {сл['основ услуг']}, "
          f"основ объекта {сл['основ объекта']}. {сл['почему']}")

    типы, вердикты = res["сменился тип"], res["сменился вердикт"]
    if not типы and not вердикты:
        print("\nНичего не меняется: типы и вердикты совпадают с новым словарём.")
    else:
        print(f"\nСменится тип у {len(типы)} строк, вердикт у {len(вердикты)}.")
        показать = {r["фраза"]: r for r in типы + вердикты}
        for r in list(показать.values())[:show]:
            print(f"  {str(r['частотность'] or '—'):>7}  {r['фраза'][:50]:<50} "
                  f"{r['было']} → {r['стало']}")
        if len(показать) > show:
            print(f"  ... ещё {len(показать) - show} строк")

    бренд = res["снято брендовое происхождение"]
    if бренд:
        print(f"\nСнято брендовое происхождение у {len(бренд)} строк: тип был "
              "назначен не составом фразы, а тем, что она пришла расширением "
              "брендового seed. Новый словарь разбирает их по составу.")

    пересуд = res["требует пересуда"]
    if пересуд:
        print(f"\nВердикт получен под прежним словарём, требует пересуда: "
              f"{len(пересуд)} строк. Выдача по ним снималась, поэтому вердикт "
              "НЕ переписан — измерение новым правилом не переписывают. "
              "Пересмотр стоит запроса в Search API на фразу.")
        for r in пересуд[:show]:
            print(f"  {str(r['частотность'] or '—'):>7}  {r['фраза'][:50]:<50} "
                  f"{r['было']} → тип {r['стало'].split('/')[0]}, "
                  f"вердикт оставлен «{r['вердикт']}»")

    print("\nБыло:  " + ", ".join(f"{k} {v}" for k, v in res["было"].items()))
    print("Стало: " + ", ".join(f"{k} {v}" for k, v in res["стало"].items()))
    print("\nЗаписано в базу." if res["применено"]
          else "\nПоказ без записи. Записать: --apply")


def _smeta_core(args) -> str:
    """
    Смета прогона ядра: сколько обращений, к какому платному источнику, по
    какому проекту и что уже накоплено. Это самый дорогой прогон пакета —
    семена в Wordstat плюс проверка выдачей по числу фраз в очереди, — и
    человек должен видеть оба числа до запуска.

    Источников два, и складывать их в одно число нельзя: они тарифицируются
    отдельно. Поэтому смета печатается двумя блоками.
    """
    from . import estimate as smeta

    project = storage.get_project(args.project) or {}
    имя, домен = project.get("name"), project.get("domain")
    семян = len(_terms(project, "brand_terms")) + len(_terms(project, "seed_terms"))
    if args.brand or args.seed:
        семян = len(args.brand or []) + len(args.seed or [])

    блоки = []
    if not args.rejudge:
        накоплено = "частотности, уже лежащие на диске, второй раз не покупаются"
        блоки.append(smeta.text(smeta.estimate(
            "wordstat", семян, project=имя, domain=домен,
            из_чего=f"{семян} {smeta.plural(семян, 'семя', 'семени', 'семян')} × "
                    "один запрос расширений",
            накоплено=накоплено)))

    # Проверка выдачей: сколько строк реально ждёт вердикта. Считается по базе,
    # а не по потолку `--validate-top`: потолок — это разрешение, а не расход.
    ждут = 0
    if not args.collect and not args.rejudge and args.validate_top > 0:
        ждут = sum(1 for r in storage.get_core(args.project)
                   if not (r.get("top_domains") or r.get("our_position")
                           or r.get("competition")))
        ждут = min(ждут, args.validate_top)
        блоки.append(smeta.text(smeta.estimate(
            "search-api", ждут, project=имя, domain=домен,
            из_чего=f"{ждут} {smeta.plural(ждут, 'фраза', 'фразы', 'фраз')} без "
                    "снятой выдачи, по одному снимку",
            потолок=args.validate_top,
            накоплено=("строки, по которым выдача уже снималась, "
                       "не переснимаются"),
            примечание=("Брендовые фразы снимаются "
                        f"{args.brand_repeats} раза каждая: одиночный снимок "
                        "позицией не является"))))

    if args.rejudge:
        блоки.append("Пересуд ничего не тратит: работает по тому, что уже в базе.")

    return ("Сухой прогон: ничего не потрачено, в сеть не ходили.\n\n"
            + "\n\n".join(блоки)
            + "\n\nСходить по-настоящему, но без записи: --fetch-only"
              "\nЗаписать в базу: --apply")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Семантическое ядро проекта")
    p.add_argument("--project", type=int, required=True, help="id проекта")
    p.add_argument("--пересудить", "--rejudge", action="store_true", dest="rejudge",
                   help="пересчитать тип и вердикт у накопленных строк ядра по "
                        "словарю ниши проекта. Бесплатно: ни Wordstat, ни выдачи. "
                        "Вердикт, полученный проверкой выдачи, не переписывается")
    p.add_argument("--brand", action="append", help="брендовая фраза (можно повторять)")
    p.add_argument("--seed", action="append", help="фраза по роду деятельности (можно повторять)")
    p.add_argument("--region", type=int, default=None)
    p.add_argument("--our-domain", default=None, dest="our_domain")
    p.add_argument("--validate-top", type=int, default=60, dest="validate_top",
                   help="сколько фраз проверить выдачей; каждый запрос платный")
    p.add_argument("--collect", action="store_true",
                   help="только сбор через Wordstat, без трат на выдачу")
    p.add_argument("--dry", action="store_true", help="ничего не писать: ни файлов, ни базы")
    p.add_argument("--apply", action="store_true", help="записать ядро в базу")
    p.add_argument("--show", type=int, default=20, help="сколько строк показать")
    p.add_argument("--quiet", action="store_true", help="не печатать таблицу")
    p.add_argument("--limit-per-seed", type=int, default=40, dest="limit_per_seed",
                   help="сколько расширений брать с одного seed; за квотой строки "
                        "сохраняются вердиктом «отбросить» с названной причиной")
    p.add_argument("--brand-repeats", type=int, default=BRAND_REPEATS,
                   dest="brand_repeats",
                   help="снимков выдачи на брендовую фразу; в вердикт идёт медиана. "
                        "Каждый снимок платный")
    p.add_argument("--сходить-без-записи", "--fetch-only", action="store_true",
                   dest="fetch_only",
                   help="сходить в Wordstat и выдачу (ПЛАТНО), но не писать ни "
                        "файлов, ни базы")
    args = p.parse_args(argv)

    # `--dry` значит «ничего не потрачено»: если бы он снимал только запись, а
    # сбор и проверка шли как обычно, слово «сухой» врало бы о деньгах.
    # Сходить и не записывать — отдельный ключ с честным именем.
    if args.dry and not args.fetch_only:
        print(_smeta_core(args), file=sys.stderr)
        return 0

    if args.rejudge:
        # Пересуд ничего не собирает и никуда не ходит: работает по тому, что
        # уже лежит в базе. Поэтому он раньше `build` и до всякой оплаты.
        _print_rejudge(rejudge(args.project, apply=args.apply and not args.dry),
                       show=args.show * 2)
        return 0

    core = build(
        args.project,
        brand_terms=args.brand,
        seed_terms=args.seed,
        region=args.region,
        our_domain=args.our_domain,
        validate_top=args.validate_top,
        collect_only=args.collect,
        apply=args.apply and not args.dry,
        write_files=not args.dry,
        limit_per_seed=args.limit_per_seed,
        brand_repeats=args.brand_repeats,
    )

    _print_summary(core)
    if not args.quiet:
        _print_table(core, args.show)

    if args.dry:
        print("\nПрогон вхолостую: ни файлов, ни записи в базу.")
    else:
        for kind, path in (core.get("files") or {}).items():
            print(f"Сохранено ({kind}): {path}")
        if args.apply:
            print(f"Записано строк ядра: {core['saved']}")
        else:
            print("В базу не писали. Записать: --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
