#!/usr/bin/env python3
"""
Бриф на статью — разведка перед написанием.

Модуль превращает целевой запрос в фактуру для брифа: кто стоит в топе Яндекса,
о чём эти страницы, какого они формата, чего в них нет и какие подзапросы люди
задают. Текст статьи здесь не генерируется — генерируется основание, на котором
его пишут.

Источники (одна метрика — одно место):
- выдача и её состав       — `yaseo.yandex_serp`  (Yandex Search API v2)
- конкурентность и полоса  — `yaseo.competition`  (расчёт по составу выдачи)
- частотность и подзапросы — `yaseo.wordstat`     (Yandex Wordstat API v2)

Правила вывода:
- Наблюдение и вывод разведены: `content_gaps` — только то, что видно в данных,
  `recommendations` — вывод, и каждый обязан ссылаться на своё наблюдение.
- Источник молчит — в поле `нет данных`. Ни среднего, ни оценки.
- Ссылочный вес недоступен, поэтому в брифе о нём не рассуждают.
- Дата снятия выдачи стоит в отчёте: выдача меняется, срез верен на момент.

Публичный API:
- build_brief(query, region, n, our_domain) -> Brief
- build_briefs(queries, ...)                -> list[Brief]
- render_markdown(brief)                    -> str

Запуск как скрипт:
    yaseo brief --query "пластиковые окна"
    yaseo brief --query "..." --md out.md
    yaseo brief --queries-file q.txt --md-dir briefs/
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config, wordstat
from .competition import _is_portal, _is_pseudo, _norm, _parse_modtime, score_serp
from .yandex_serp import RUSSIA, SerpResult, serp, serp_batch

# ── Конструкции заголовков. Метка → что искать в title. ───────────────────────
# Это не «типы контента вообще», а то, что часто повторяется в коммерческой
# выдаче рунета. Метка попадает в бриф, если встречается минимум у MIN_PATTERN_PAGES страниц.
TITLE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("вопрос «как»", r"\bкак\b"),
    ("вопрос «что такое»", r"\bчто\s+так(ое|ие)\b"),
    ("цена или стоимость", r"(сколько\s+стоит|\bцен[аыу]\b|\bстоимост)"),
    ("этапы или шаги", r"\b(этап|шаг[иов]|пошагов)"),
    ("кейс или пример", r"\b(кейс|пример)"),
    ("год в заголовке", r"\b20[12]\d\b"),
    ("нумерованный список", r"\b\d+\s+[а-яё]{4,}"),
    ("«под ключ»", r"под\s+ключ"),
    ("топ или рейтинг", r"\b(топ[-\s]?\d|рейтинг|лучш)"),
    # Страница агентства опознаётся и по слову «агентство» в title: такие страницы
    # часто не содержат ни «услуг», ни «заказать» — только название студии.
    ("страница услуги", r"(\bуслуг|заказать|разработк|создани|агентств|\bagency\b|\bстудия\b)"),
    ("обзор или инструкция", r"\b(обзор|гайд|руководств|инструкц|чек-?лист)"),
    ("отзывы", r"\bотзыв"),
)

#: Конструкции, которые задают формат материала — из них выбирается образец формата.
FORMAT_LABELS = frozenset({
    "вопрос «как»", "вопрос «что такое»", "цена или стоимость",
    "этапы или шаги", "кейс или пример", "нумерованный список",
    "обзор или инструкция",
})

#: Конструкции коммерческой страницы: продают услугу, а не отвечают на вопрос.
COMMERCIAL_LABELS = frozenset({"страница услуги", "«под ключ»", "топ или рейтинг", "отзывы"})

#: Конструкция встречается минимум у стольких страниц — иначе это совпадение, не паттерн.
MIN_PATTERN_PAGES = 3

#: Топ старше стольких лет по медиане считаем устаревшим.
STALE_YEARS = 2

#: Вопросные слова — по ним подзапросы Wordstat превращаются в H2.
QUESTION_WORDS = re.compile(r"\b(как|что|сколько|где|какой|какая|какие|почему|зачем|когда|чем)\b")

#: Ценовой интерес в подзапросах.
PRICE_INTENT = re.compile(r"(сколько\s+стоит|\bцен[аыу]?\b|\bстоимост)")

#: Ниже этого порога спрос по самому запросу считаем малым.
#: Порог — проектное решение, не измерение.
LOW_FREQ = 100

#: Как называем два семейства расширений Wordstat.
#: `top` содержит саму фразу — это подзапросы темы. `association` — смежный спрос,
#: он у Wordstat бывает шумным, поэтому в H2 и в кандидаты не идёт.
KIND_LABELS = {"top": "уточнение", "association": "ассоциация"}


@dataclass
class TopPage:
    position: int
    url: str
    domain: str
    title: str
    is_portal: bool
    url_depth: int
    modtime: str | None
    passage: str | None


@dataclass
class Brief:
    query: str
    region: int
    #: момент снятия выдачи — он же дата брифа: срез верен на эту минуту
    generated_at: str
    frequency: int | None
    competition: float | None
    band: str
    our_position: int | None
    found_all: int
    top_pages: list[TopPage] = field(default_factory=list)
    related_queries: list[dict] = field(default_factory=list)
    title_patterns: list[str] = field(default_factory=list)
    content_gaps: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ── Мелкие помощники вывода ──────────────────────────────────────────────────


def _num(value: int | float | None) -> str:
    if value is None:
        return "нет данных"
    return f"{value:,}".replace(",", " ")


def _shows(value: int | None) -> str:
    """«1 показ» · «2 показа» · «13 показов» — бриф читает человек."""
    if value is None:
        return "нет данных"
    tail = abs(value) % 100
    unit = (
        "показов"
        if 11 <= tail <= 14
        else {1: "показ", 2: "показа", 3: "показа", 4: "показа"}.get(tail % 10, "показов")
    )
    return f"{_num(value)} {unit}"


def _fmt_moment(iso: str | None) -> str:
    if not iso:
        return "нет данных"
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M UTC")
    except ValueError:
        return iso


def _fmt_day(modtime: str | None) -> str:
    dt = _parse_modtime(modtime)
    return dt.strftime("%d.%m.%Y") if dt else "нет данных"


def _cell(text: str, limit: int = 72) -> str:
    text = (text or "").replace("|", "/").replace("\n", " ").strip()
    if not text:
        return "нет данных"
    return (text[: limit - 1] + "…") if len(text) > limit else text


def _slug(query: str) -> str:
    return re.sub(r"[^\w]+", "-", query.strip().lower(), flags=re.UNICODE).strip("-") or "brief"


def _query_words(query: str) -> int:
    return max(1, len([w for w in re.split(r"\W+", query) if len(w) > 2]))


# ── Разбор выдачи ────────────────────────────────────────────────────────────


def _top_pages(result: SerpResult, n: int) -> list[TopPage]:
    """Страницы топа без псевдорезультатов: колдунщики — не конкуренты."""
    return [
        TopPage(
            position=d.position,
            url=d.url,
            domain=_norm(d.domain),
            title=d.title,
            is_portal=_is_portal(d.domain),
            url_depth=d.url_depth,
            modtime=d.modtime,
            passage=d.passage,
        )
        for d in result.docs
        if not _is_pseudo(d)
    ][:n]


def _pattern_hits(pages: list[TopPage]) -> dict[str, int]:
    """Сколько заголовков топа несут каждую конструкцию."""
    hits: dict[str, int] = {}
    for label, pattern in TITLE_PATTERNS:
        rx = re.compile(pattern, re.IGNORECASE)
        count = sum(1 for p in pages if rx.search((p.title or "").lower()))
        if count:
            hits[label] = count
    return hits


def _title_patterns(hits: dict[str, int], total: int) -> list[str]:
    return [
        f"{label} — {count} из {total} заголовков"
        for label, count in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))
        if count >= MIN_PATTERN_PAGES
    ]


def _median_modtime(pages: list[TopPage]) -> tuple[datetime | None, int]:
    dates = sorted(d for d in (_parse_modtime(p.modtime) for p in pages) if d is not None)
    if not dates:
        return None, 0
    mid = len(dates) // 2
    median = dates[mid] if len(dates) % 2 else dates[mid - 1]
    return median, len(dates)


def _hlwords(page: TopPage) -> int:
    """Подсветка слов запроса в title — сигнал Яндекса, а не наш разбор строки."""
    return int(getattr(page, "_hlwords", 0))


def _content_gaps(
    query: str,
    pages: list[TopPage],
    hits: dict[str, int],
    related: list[dict],
) -> list[str]:
    """Только наблюдаемое. Каждый разрыв несёт число, по которому его проверяют."""
    n = len(pages)
    if not n:
        return ["Выдача пуста — разрывы не выводятся."]

    gaps: list[str] = []

    with_digits = sum(1 for p in pages if re.search(r"\d", p.title or ""))
    if with_digits == 0:
        gaps.append(f"В заголовках топа нет ни одного числа: 0 из {n}.")

    median, known = _median_modtime(pages)
    if median and known >= 3:
        years = (datetime.now(timezone.utc) - median).days / 365.25
        if years >= STALE_YEARS:
            gaps.append(
                f"Топ устарел: медиана обновления — {median:%d.%m.%Y}, это {years:.1f} года назад "
                f"(даты известны у {known} из {n} страниц)."
            )

    case_rx = re.compile(r"\b(кейс|пример)", re.IGNORECASE)
    cases = sum(1 for p in pages if case_rx.search(f"{p.title or ''} {p.passage or ''}".lower()))
    if cases == 0:
        gaps.append(
            f"Разбора практики в топе нет: слова «кейс» и «пример» не встречаются "
            f"ни в одном заголовке и сниппете (0 из {n})."
        )

    portals = [p for p in pages if p.is_portal]
    if portals and len(portals) * 2 >= n:
        names = ", ".join(sorted({p.domain for p in portals})[:4])
        gaps.append(
            f"Топ держат площадки: {len(portals)} из {n} мест ({names}). "
            f"Собственных сайтов — {n - len(portals)}."
        )

    homepages = sum(1 for p in pages if p.url_depth == 0)
    if homepages * 2 > n:
        gaps.append(
            f"В топе главные страницы: {homepages} из {n} с глубиной пути 0 — "
            f"запрос закрывается ими попутно."
        )

    commercial = max((hits.get(label, 0) for label in COMMERCIAL_LABELS), default=0)
    if commercial and commercial * 2 >= n:
        label = next(
            (l for l in sorted(COMMERCIAL_LABELS) if hits.get(l, 0) == commercial), ""
        )
        gaps.append(
            f"Топ занят коммерческими страницами: конструкция «{label}» стоит "
            f"в {commercial} из {n} заголовков."
        )

    qwords = _query_words(query)
    exact = sum(1 for p in pages if _hlwords(p) >= qwords)
    if exact * 3 <= n:
        gaps.append(
            f"Запрос в заголовке топа почти не звучит дословно: полное вхождение "
            f"у {exact} из {n} страниц."
        )

    price_asked = [r for r in related if PRICE_INTENT.search(r["phrase"])]
    if price_asked:
        answered = sum(
            1 for p in pages if PRICE_INTENT.search(f"{p.title or ''} {p.passage or ''}".lower())
        )
        if answered == 0:
            top_ask = price_asked[0]
            gaps.append(
                f"Про цену спрашивают, в топе не отвечают: подзапрос «{top_ask['phrase']}» — "
                f"{_shows(top_ask['freq'])}, страниц с ценой в топе 0 из {n}."
            )

    if gaps:
        return gaps

    # Пустой список читается как «не проверяли». Показываем, чем именно закрыт каждый
    # признак, — тогда видно, что топ здоровый, а не что разбор не делали.
    checked = [
        f"числа в заголовках есть у {with_digits} из {n}",
        (
            f"медиана обновления — {median:%d.%m.%Y}"
            if median and known >= 3
            else f"дат обновления известно {known} из {n}"
        ),
        f"кейсы или примеры упоминают {cases} из {n}",
        f"площадки держат {len(portals)} из {n} мест",
        f"главных страниц {homepages} из {n}",
        f"полное вхождение запроса в title у {exact} из {n}",
    ]
    return ["Разрывов по наблюдаемым признакам нет: " + ", ".join(checked) + "."]


def _recommendations(
    query: str,
    pages: list[TopPage],
    hits: dict[str, int],
    gaps: list[str],
    related: list[dict],
    frequency: int | None,
    competition: float | None,
    band: str,
    our_position: int | None,
) -> list[str]:
    """Вывод из связки конкурентности, частотности и разрывов. Без наблюдения — не пишем."""
    n = len(pages)
    if not n:
        return ["Выдача не снята — рекомендаций нет."]

    out: list[str] = []
    gap_text = " ".join(gaps)

    if our_position:
        page = next((p for p in pages if p.position == our_position), None)
        url = page.url if page else "наша страница"
        out.append(
            f"Писать бриф на обновление, а не на новую статью: мы уже на {our_position}-й позиции "
            f"({url}). Вторая своя страница под тот же запрос отнимет позицию у первой."
        )

    formats = {l: c for l, c in hits.items() if l in FORMAT_LABELS and c >= MIN_PATTERN_PAGES}
    if formats:
        label, count = max(formats.items(), key=lambda kv: (kv[1], kv[0]))
        out.append(
            f"Взять формат «{label}»: так устроены {count} из {n} заголовков топа — "
            f"поиск подтверждает, какой ответ здесь ждут."
        )
    else:
        # Коммерческие конструкции сюда не попадают, поэтому формулировка говорит
        # именно про формат материала — иначе она спорит с разделом «Паттерны».
        out.append(
            f"Формат материала в топе не устоялся: конструкции формата (вопрос, этапы, "
            f"кейс, список, обзор) не повторяются у {MIN_PATTERN_PAGES}+ заголовков из {n}. "
            f"Структуру задаём от потребности читателя."
        )

    if "нет ни одного числа" in gap_text:
        out.append(
            "Вынести в заголовок конкретное число из собственной практики: в заголовках топа "
            "чисел нет вовсе, цифра сразу отличает сниппет."
        )

    if "Топ устарел" in gap_text:
        median, _ = _median_modtime(pages)
        out.append(
            f"Дать свежие данные с датой замера: медиана обновления топа — "
            f"{median:%d.%m.%Y}, актуальность здесь свободна."
        )

    if "Разбора практики в топе нет" in gap_text:
        out.append(
            "Строить статью на собственном кейсе с проверяемыми цифрами: "
            "в топе разбора практики нет ни на одной странице."
        )

    if "Топ держат площадки" in gap_text:
        portals = sum(1 for p in pages if p.is_portal)
        out.append(
            f"Целиться в подзапросы, а не в общий запрос: площадки держат {portals} из {n} мест "
            f"по нему. Узкие формулировки — в таблице связанных запросов."
        )

    if "В топе главные страницы" in gap_text:
        homepages = sum(1 for p in pages if p.url_depth == 0)
        out.append(
            f"Ответить точнее, чем главная страница: {homepages} из {n} мест держат главные, "
            f"они закрывают запрос вскользь."
        )

    if "Топ занят коммерческими страницами" in gap_text:
        out.append(
            "Писать ответ на вопрос, продающую страницу здесь ставить некуда: "
            "коммерческие страницы в топе уже стоят."
        )

    if "почти не звучит дословно" in gap_text:
        out.append(
            f"Поставить «{query}» в H1 дословно: у страниц топа полного вхождения "
            f"в заголовок почти нет."
        )

    if "Про цену спрашивают, в топе не отвечают" in gap_text:
        out.append(
            "Дать блок с порядком цен и составом работ: спрос на цену в подзапросах есть, "
            "страниц с ответом в топе нет."
        )

    refinements = _refinements(related)

    if competition is not None and frequency is not None:
        facts = f"конкурентность {competition} из 100 ({band}), частотность {_shows(frequency)} в месяц"
        if band == "низкая":
            out.append(f"Запрос берём в работу: {facts}.")
        elif band == "высокая":
            candidates = ", ".join(f"«{r['phrase']}» ({_num(r['freq'])})" for r in refinements[:3])
            tail = (
                f" Кандидаты — уточнения Wordstat: {candidates}."
                if candidates
                else " Уточнений Wordstat по фразе нет, подзапросы искать отдельным прогоном."
            )
            out.append(f"Заходить через подзапросы: {facts}.{tail}")
        else:
            out.append(f"Планировать статью сразу с подзапросами внутри: {facts}.")
    elif frequency is None:
        out.append(
            "Частотность не получена — приоритет темы по объёму спроса не считается "
            "(«нет данных»)."
        )

    if frequency is not None and frequency < LOW_FREQ:
        out.append(
            f"Спрос по самому запросу мал: {_shows(frequency)} в месяц против нашего "
            f"порога {LOW_FREQ} (порог — проектное решение, не измерение). Одно ядро "
            f"трафика не даст, тему вести связкой формулировок."
        )

    questions = [r for r in refinements if QUESTION_WORDS.search(r["phrase"])][:4]
    if questions:
        listed = "; ".join(f"«{r['phrase']}» — {_num(r['freq'])}" for r in questions)
        out.append(f"Готовые H2 из уточнений Wordstat: {listed}.")

    return out


# ── Сборка ───────────────────────────────────────────────────────────────────


def _related(freq: wordstat.Frequency | None, query: str, limit: int = 15) -> list[dict]:
    """
    Расширения Wordstat. Уточнения идут первыми при любой частотности: они содержат
    саму фразу, а ассоциации — смежный спрос, и по частотности они почти всегда выше.
    Сортировка по одному числу выносила бы наверх шум.
    """
    if freq is None or freq.error:
        return []
    seen: set[str] = {query.strip().lower()}
    rows: list[dict] = []
    ordered = sorted(freq.expansions, key=lambda x: (0 if x.kind == "top" else 1, -x.freq))
    for e in ordered:
        phrase = (e.phrase or "").strip()
        key = phrase.lower()
        if not phrase or key in seen:
            continue
        seen.add(key)
        rows.append({"phrase": phrase, "freq": e.freq, "kind": e.kind})
        if len(rows) >= limit:
            break
    return rows


def _refinements(related: list[dict]) -> list[dict]:
    """Только уточнения: в H2 и в кандидаты ассоциации не пускаем."""
    return [r for r in related if r["kind"] == "top"]


def _assemble(
    result: SerpResult,
    freq: wordstat.Frequency | None,
    our_domain: str | None,
    n: int,
) -> Brief:
    now = datetime.now(timezone.utc).isoformat()
    frequency = freq.freq if freq and not freq.error else None

    if result.error:
        return Brief(
            query=result.query,
            region=result.region,
            generated_at=result.fetched_at or now,
            frequency=frequency,
            competition=None,
            band="нет данных",
            our_position=None,
            found_all=0,
            related_queries=_related(freq, result.query),
            error=result.error,
        )

    comp = score_serp(result, our_domain=our_domain, top=n)
    pages = _top_pages(result, n)

    # Подсветку title кладём на страницу приватно: в контракт TopPage она не входит,
    # но без неё нельзя честно сказать «запрос в заголовке не звучит».
    hl = {d.position: d.title_hlwords for d in result.docs}
    for p in pages:
        setattr(p, "_hlwords", hl.get(p.position, 0))

    related = _related(freq, result.query)
    hits = _pattern_hits(pages)
    patterns = _title_patterns(hits, len(pages))
    gaps = _content_gaps(result.query, pages, hits, related)
    competition = comp.score if not comp.error else None

    recs = _recommendations(
        query=result.query,
        pages=pages,
        hits=hits,
        gaps=gaps,
        related=related,
        frequency=frequency,
        competition=competition,
        band=comp.band,
        our_position=comp.our_position,
    )

    return Brief(
        query=result.query,
        region=result.region,
        generated_at=result.fetched_at or now,
        frequency=frequency,
        competition=competition,
        band=comp.band,
        our_position=comp.our_position,
        found_all=result.found_all,
        top_pages=pages,
        related_queries=related,
        title_patterns=patterns,
        content_gaps=gaps,
        recommendations=recs,
        error=None,
    )


def _frequency(query: str, region: int, num: int = 200) -> wordstat.Frequency:
    """Wordstat не обязан ответить. Молчание — это `нет данных`, а не остановка брифа."""
    try:
        return wordstat.frequency(query, region=str(region), num=num)
    except Exception as e:  # noqa: BLE001 — причина уезжает в поле error
        return wordstat.Frequency(
            phrase=query, region=str(region), freq=None, error=f"{type(e).__name__}: {e}"
        )


def build_brief(
    query: str,
    region: int = RUSSIA,
    n: int = 10,
    our_domain: str | None = None,
) -> Brief:
    """Фактура для брифа по одному запросу: выдача + конкурентность + Wordstat."""
    our_domain = config.domain_or_none(our_domain)
    result = serp(query, region=region, n=max(n, 10))
    return _assemble(result, _frequency(query, region), our_domain, n)


def build_briefs(
    queries: list[str],
    region: int = RUSSIA,
    n: int = 10,
    our_domain: str | None = None,
    progress: bool = True,
    delay: float = 0.4,
) -> list[Brief]:
    """Пакетный прогон: выдача снимается пачкой, Wordstat — по одному запросу."""
    our_domain = config.domain_or_none(our_domain)
    results = serp_batch(queries, region=region, n=max(n, 10), progress=progress)
    briefs: list[Brief] = []
    for i, result in enumerate(results, 1):
        briefs.append(_assemble(result, _frequency(result.query, region), our_domain, n))
        if progress:
            print(f"[{i:03}/{len(results)}] бриф собран: {result.query!r}", file=sys.stderr)
        time.sleep(delay)
    return briefs


# ── Markdown ─────────────────────────────────────────────────────────────────

LIMITS = (
    "Ссылочный вес в расчёт не входит: источника по Яндексу нет, и вывод о нём не делается. "
    "Конкурентность считается по составу выдачи, веса факторов — проектное решение, не измерение. "
    "Выдача меняется, бриф верен на дату снятия."
)


def render_markdown(brief: Brief) -> str:
    """Готовый MD-бриф: его можно отдать копирайтеру как есть."""
    moment = _fmt_moment(brief.generated_at)
    lines: list[str] = [
        f"# Бриф: «{brief.query}»",
        "",
        f"Выдача Яндекса снята {moment} · регион {brief.region}. "
        f"Это разведка перед написанием, сам текст статьи пишется отдельно.",
        "",
    ]

    if brief.error:
        lines += [
            f"**Выдача не получена:** {brief.error}",
            "",
            "Фактура по топу — нет данных. Без неё бриф не собирается.",
            "",
            f"Частотность по Wordstat: {_shows(brief.frequency)}"
            + (" в месяц." if brief.frequency is not None else "."),
            "",
            "---",
            "",
            LIMITS,
            "",
        ]
        return "\n".join(lines)

    position = "в снятом топе нет"
    if brief.our_position:
        ordinal = next(
            (i for i, p in enumerate(brief.top_pages, 1) if p.position == brief.our_position), None
        )
        position = f"позиция {brief.our_position}"
        if ordinal and ordinal != brief.our_position:
            position += f" ({ordinal}-я среди разобранных страниц)"

    competition = (
        f"{brief.competition} из 100 — {brief.band}"
        if brief.competition is not None
        else "нет данных"
    )
    lines += [
        f"**Частотность:** {_shows(brief.frequency)} в месяц (Wordstat) · "
        f"**Конкурентность:** {competition} · "
        f"**Наш домен:** {position} · "
        f"**Найдено документов:** {_num(brief.found_all)}",
        "",
        f"## Топ-{len(brief.top_pages)} выдачи",
        "",
        "| # | Домен | Тип | Глубина | Обновлена | Заголовок |",
        "|---:|---|---|---:|---|---|",
    ]
    for p in brief.top_pages:
        lines.append(
            f"| {p.position} | {_cell(p.domain, 28)} | "
            f"{'площадка' if p.is_portal else 'сайт'} | {p.url_depth} | "
            f"{_fmt_day(p.modtime)} | {_cell(p.title)} |"
        )

    if brief.top_pages:
        skipped = brief.top_pages[-1].position - len(brief.top_pages)
        if skipped > 0:
            lines += [
                "",
                f"Нумерация с пропусками: псевдорезультатов исключено — {skipped} "
                f"(блоки колдунщиков: картинки, видео, карты). Это не конкуренты.",
            ]

    lines += ["", "## Паттерны заголовков", ""]
    if brief.title_patterns:
        lines += [f"- {row}" for row in brief.title_patterns]
    else:
        lines.append(
            f"Повторяющихся конструкций у {MIN_PATTERN_PAGES}+ заголовков нет — "
            f"формат в топе не устоялся."
        )

    lines += ["", "## Разрывы в топе", ""]
    lines += [f"- {row}" for row in brief.content_gaps] or [
        "Разрывов по наблюдаемым признакам нет."
    ]

    lines += ["", "## Связанные запросы (Wordstat, топ-15)", ""]
    if brief.related_queries:
        lines += ["| Фраза | Показов в месяц | Тип |", "|---|---:|---|"]
        lines += [
            f"| {_cell(r['phrase'], 60)} | {_num(r['freq'])} | "
            f"{KIND_LABELS.get(r['kind'], r['kind'])} |"
            for r in brief.related_queries
        ]
        if any(r["kind"] == "association" for r in brief.related_queries):
            lines += [
                "",
                "Уточнения содержат саму фразу — это подзапросы темы. Ассоциации Wordstat "
                "показывают смежный спрос и бывают шумными: в структуру статьи их брать "
                "после проверки смысла.",
            ]
    else:
        lines.append("Нет данных: Wordstat не вернул расширений по фразе.")

    lines += ["", "## Рекомендации", ""]
    lines += [f"{i}. {row}" for i, row in enumerate(brief.recommendations, 1)]

    lines += ["", "---", "", LIMITS, ""]
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Бриф на статью по выдаче Яндекса и Wordstat")
    p.add_argument("--query", action="append", help="целевой запрос (можно повторять)")
    p.add_argument("--queries-file", help="файл с запросами, по одному в строке")
    p.add_argument("--region", type=int, default=RUSSIA)
    p.add_argument("--n", type=int, default=10, help="сколько страниц топа разбирать")
    p.add_argument("--our-domain", default=None,
                   help="наш домен; по умолчанию YASEO_DOMAIN или домен единственного проекта")
    p.add_argument("--md", help="сохранить MD-бриф в файл (один запрос)")
    p.add_argument("--md-dir", help="каталог для MD-брифов (пакетный прогон)")
    p.add_argument("--json", help="сохранить всю фактуру в JSON")
    p.add_argument("--quiet", action="store_true", help="не печатать бриф в stdout")
    args = p.parse_args(argv)

    queries = list(args.query or [])
    if args.queries_file:
        with open(args.queries_file, encoding="utf-8") as f:
            queries += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not queries:
        p.error("нужен --query или --queries-file")

    if len(queries) == 1:
        briefs = [build_brief(queries[0], region=args.region, n=args.n, our_domain=args.our_domain)]
    else:
        briefs = build_briefs(queries, region=args.region, n=args.n, our_domain=args.our_domain)

    for b in briefs:
        md = render_markdown(b)
        if not args.quiet:
            print(md)
        if args.md and len(briefs) == 1:
            Path(args.md).write_text(md, encoding="utf-8")
            print(f"Сохранено: {args.md}", file=sys.stderr)
        if args.md_dir:
            out_dir = Path(args.md_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"brief-{_slug(b.query)}.md"
            path.write_text(md, encoding="utf-8")
            print(f"Сохранено: {path}", file=sys.stderr)

    if args.json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "region": args.region,
            "note": LIMITS,
            "briefs": [b.to_dict() for b in briefs],
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"Сохранено: {args.json}", file=sys.stderr)

    return 1 if all(b.error for b in briefs) else 0


if __name__ == "__main__":
    sys.exit(main())
