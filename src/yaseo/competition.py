#!/usr/bin/env python3
"""
Конкурентность запроса — расчёт по составу выдачи Яндекса.

Композиционная метрика: сложность выводится из того, кто и чем занимает топ,
а не из ссылочного профиля. Ссылочный вес недоступен без глобального индекса
ссылок, и это ограничение называется вслух в каждом отчёте.

Шкала 0–100. Полосы: <30 низкая · 30–60 средняя · >60 высокая.

Веса факторов — проектное решение, не измерение. Они подобраны под
коммерческие ниши рунета и меняются осознанно, с записью причины.
Каждый расчёт возвращает разбор по факторам, поэтому число проверяемо.

Публичный API:
- score_serp(serp)                  -> Competition
- score_query(query, region, n)     -> Competition
- score_batch(queries, ...)         -> (list[Competition], list[SerpResult])
- serp_competitors(serps)           -> list[DomainStat]

Запуск как скрипт:
    yaseo competition --query "пластиковые окна" --our-domain example.ru
    yaseo competition --queries-file queries.txt --json out.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import config
from .yandex_serp import RUSSIA, SerpResult, serp, serp_batch

# ── Веса факторов. Сумма = 1.0. Проектное решение, см. предупреждение выше. ──
WEIGHTS: dict[str, float] = {
    "title_fit": 0.30,        # насколько страницы топа заточены под запрос
    "depth_coverage": 0.25,   # насколько глубоко домены покрывают тему
    "portal_share": 0.20,     # сколько мест держат крупные площадки
    "homepage_share": 0.10,   # сколько мест держат главные страницы
    "freshness": 0.15,        # насколько свежий топ
}

#: Крупные площадки и агрегаторы: занимают место, вытесняются тяжело.
PORTALS: frozenset[str] = frozenset({
    "yandex.ru", "dzen.ru", "vk.com", "ok.ru", "youtube.com", "rutube.ru",
    "vc.ru", "habr.com", "pikabu.ru", "wikipedia.org", "ru.wikipedia.org",
    "rbc.ru", "companies.rbc.ru", "forbes.ru", "kommersant.ru", "vedomosti.ru",
    "rb.ru", "sostav.ru", "adindex.ru", "cossa.ru",
    "avito.ru", "cian.ru", "domclick.ru", "m2.ru", "novostroy-m.ru",
    "dprofile.ru", "behance.net", "tenchat.ru",
    "pinterest.ru", "pinterest.com", "freepik.com", "shutterstock.com",
    "profi.ru", "youla.ru", "zen.yandex.ru", "t.me",
})

#: Псевдорезультаты выдачи — блоки колдунщиков, не конкуренты.
PSEUDO_TITLE = re.compile(r"^(картинки|видео|карты|товары)\s+по запросу", re.I)

#: Свежим считаем топ, обновлённый за последние N лет.
FRESH_YEARS = 2


@dataclass
class Factor:
    name: str
    value: float           # 0..1
    weight: float
    contribution: float    # value * weight * 100
    evidence: str


@dataclass
class Competition:
    query: str
    region: int
    calculated_at: str
    score: float                     # 0..100
    band: str                        # низкая | средняя | высокая
    docs_analysed: int
    docs_excluded: int
    found_all: int
    our_position: int | None         # позиция нашего домена, если есть
    factors: list[Factor] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _band(score: float) -> str:
    if score < 30:
        return "низкая"
    if score <= 60:
        return "средняя"
    return "высокая"


def _is_pseudo(doc) -> bool:
    """Блок Яндекса, а не сайт. Признак приходит из разбора выдачи."""
    flag = getattr(doc, "is_wizard", None)
    if flag is not None:
        return bool(flag)
    return bool(PSEUDO_TITLE.match(doc.title or ""))  # разбор старых снимков


def _norm(domain: str) -> str:
    return (domain or "").lower().removeprefix("www.")


def _is_portal(domain: str) -> bool:
    d = _norm(domain)
    if d in PORTALS:
        return True
    return any(d.endswith("." + p) for p in PORTALS)


def _query_words(query: str) -> int:
    return max(1, len([w for w in re.split(r"\W+", query) if len(w) > 2]))


def _parse_modtime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:  # формат Яндекса: 20210729T083812
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def score_serp(result: SerpResult, our_domain: str | None = None, top: int = 10) -> Competition:
    """
    Считает конкурентность по готовой выдаче.

    `our_domain` нужен только для поля `our_position`. Не передан — берётся
    `config.default_domain()`; не выводится и оттуда — позиция остаётся пустой,
    а сам расчёт от домена не зависит.
    """
    now = datetime.now(timezone.utc).isoformat()

    if result.error:
        return Competition(
            query=result.query, region=result.region, calculated_at=now,
            score=0.0, band="нет данных", docs_analysed=0, docs_excluded=0,
            found_all=0, our_position=None, error=result.error,
        )

    # Позиция меряется среди сайтов. Блок картинок Яндекса — не конкурент,
    # и включать его в нумерацию значит занижать собственную позицию.
    our = _norm(config.domain_or_none(our_domain) or "")
    our_position = next(
        (d.organic_position for d in result.docs
         if our and _norm(d.domain) == our and not _is_pseudo(d) and d.organic_position),
        None,
    ) if our else None

    usable = [d for d in result.docs if not _is_pseudo(d)]
    ranked = usable[:top]
    excluded = len(result.docs) - len(usable)

    if not ranked:
        return Competition(
            query=result.query, region=result.region, calculated_at=now,
            score=0.0, band="нет данных", docs_analysed=0, docs_excluded=excluded,
            found_all=result.found_all, our_position=our_position,
            error="в выдаче нет пригодных документов",
        )

    n = len(ranked)
    qwords = _query_words(result.query)

    # 1. Заточенность title: доля слов запроса, попавших в заголовок.
    fit_vals = [min(d.title_hlwords / qwords, 1.0) for d in ranked]
    title_fit = sum(fit_vals) / n
    exact = sum(1 for v in fit_vals if v >= 1.0)

    # 2. Глубина покрытия: медиана числа страниц домена по запросу, лог-шкала.
    counts = sorted(d.domain_doccount for d in ranked)
    median_dc = counts[n // 2] if n % 2 else (counts[n // 2 - 1] + counts[n // 2]) / 2
    depth_coverage = min(math.log10(max(median_dc, 1) + 1) / 3.0, 1.0)  # ~1000 стр -> 1.0

    # 3. Доля крупных площадок.
    portals = [_norm(d.domain) for d in ranked if _is_portal(d.domain)]
    portal_share = len(portals) / n

    # 4. Доля главных страниц.
    homepages = sum(1 for d in ranked if d.url_depth == 0)
    homepage_share = homepages / n

    # 5. Свежесть.
    now_dt = datetime.now(timezone.utc)
    cutoff = now_dt.replace(year=now_dt.year - FRESH_YEARS)
    known = [dt for dt in (_parse_modtime(d.modtime) for d in ranked) if dt is not None]
    fresh = sum(1 for dt in known if dt >= cutoff)
    freshness = (fresh / len(known)) if known else 0.5  # нет дат -> нейтрально

    raw = {
        "title_fit": (title_fit, f"{exact} из {n} страниц с полным вхождением запроса в title"),
        "depth_coverage": (depth_coverage, f"медиана {median_dc:.0f} страниц домена по запросу"),
        "portal_share": (
            portal_share,
            f"{len(portals)} из {n} мест у крупных площадок"
            + (f": {', '.join(portals[:3])}" if portals else ""),
        ),
        "homepage_share": (homepage_share, f"{homepages} из {n} — главные страницы"),
        "freshness": (
            freshness,
            f"{fresh} из {len(known)} обновлены за {FRESH_YEARS} года"
            if known else "дат обновления нет, взято нейтральное 0.5",
        ),
    }

    factors = [
        Factor(name=k, value=round(v, 3), weight=WEIGHTS[k],
               contribution=round(v * WEIGHTS[k] * 100, 1), evidence=ev)
        for k, (v, ev) in raw.items()
    ]
    score = round(sum(f.contribution for f in factors), 1)

    return Competition(
        query=result.query, region=result.region, calculated_at=now,
        score=score, band=_band(score), docs_analysed=n, docs_excluded=excluded,
        found_all=result.found_all, our_position=our_position, factors=factors,
    )


def score_query(
    query: str, region: int = RUSSIA, n: int = 10, our_domain: str | None = None
) -> Competition:
    return score_serp(serp(query, region=region, n=n), our_domain=our_domain)


def score_batch(
    queries: list[str], region: int = RUSSIA, n: int = 10,
    our_domain: str | None = None, progress: bool = True,
) -> tuple[list[Competition], list[SerpResult]]:
    our_domain = config.domain_or_none(our_domain)
    serps = serp_batch(queries, region=region, n=n, progress=progress)
    return [score_serp(s, our_domain=our_domain) for s in serps], serps


@dataclass
class DomainStat:
    domain: str
    appearances: int
    queries: list[str]
    best_position: int
    avg_position: float
    is_portal: bool


def serp_competitors(serps: list[SerpResult], top: int = 10) -> list[DomainStat]:
    """Кто повторяется в выдаче по набору запросов — это и есть поисковые конкуренты."""
    hits: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for s in serps:
        if s.error:
            continue
        for d in [x for x in s.docs if not _is_pseudo(x)][:top]:
            dom = _norm(d.domain)
            if dom:
                hits[dom].append((s.query, d.position))

    stats = [
        DomainStat(
            domain=dom,
            appearances=len(rows),
            queries=sorted({q for q, _ in rows}),
            best_position=min(p for _, p in rows),
            avg_position=round(sum(p for _, p in rows) / len(rows), 1),
            is_portal=_is_portal(dom),
        )
        for dom, rows in hits.items()
    ]
    return sorted(stats, key=lambda s: (-s.appearances, s.avg_position))


def _print_one(c: Competition) -> None:
    if c.error:
        print(f"{c.query!r}: ОШИБКА — {c.error}", file=sys.stderr)
        return
    pos = f"наша позиция {c.our_position}" if c.our_position else "нас в топе нет"
    print(f"\n{c.query!r}  регион {c.region}")
    print(f"  Конкурентность: {c.score} / 100 — {c.band}")
    print(f"  Разобрано {c.docs_analysed}, исключено {c.docs_excluded}, всего найдено {c.found_all}. {pos}.")
    print(f"  {'фактор':<16} {'знач':>6} {'вес':>5} {'вклад':>6}  доказательство")
    for f in c.factors:
        print(f"  {f.name:<16} {f.value:>6.2f} {f.weight:>5.2f} {f.contribution:>6.1f}  {f.evidence}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Конкурентность запроса по выдаче Яндекса")
    p.add_argument("--query", action="append", help="запрос (можно повторять)")
    p.add_argument("--queries-file", help="файл со списком запросов, по одному в строке")
    p.add_argument("--region", type=int, default=RUSSIA)
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--our-domain", default=None,
                   help="наш домен для колонки «наша позиция»; по умолчанию YASEO_DOMAIN")
    p.add_argument("--json", help="сохранить результат в JSON")
    p.add_argument("--competitors", action="store_true", help="показать сводку по доменам")
    args = p.parse_args(argv)

    queries = list(args.query or [])
    if args.queries_file:
        with open(args.queries_file, encoding="utf-8") as f:
            queries += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not queries:
        p.error("нужен --query или --queries-file")

    comps, serps = score_batch(queries, args.region, args.n, args.our_domain)

    for c in comps:
        _print_one(c)

    if args.competitors:
        print("\nПоисковые конкуренты по набору запросов:")
        print(f"  {'домен':<32} {'вхожд':>6} {'лучш':>5} {'сред':>6}  тип")
        for s in serp_competitors(serps)[:20]:
            kind = "площадка" if s.is_portal else "сайт"
            print(f"  {s.domain[:32]:<32} {s.appearances:>6} {s.best_position:>5} {s.avg_position:>6}  {kind}")

    if args.json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "region": args.region,
            "weights": WEIGHTS,
            "note": "Веса — проектное решение, не измерение. Ссылочный вес в расчёт не входит.",
            "competition": [c.to_dict() for c in comps],
            "competitors": [asdict(s) for s in serp_competitors(serps)],
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nСохранено: {args.json}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
