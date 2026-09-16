#!/usr/bin/env python3
"""
Трекер эффективности статей блога yaseo.

Обычный трекер меряет домен: вышел сайт по запросу или нет. Здесь меряется
конкретная статья: вышла ли по своему целевому запросу ИМЕННО она, или вместо
неё вышла другая наша страница, или не вышло ничего.

Три числа держатся раздельно, потому что смешивать их — врать:
- position         — позиция именно этой статьи, URL совпал
- domain_position  — позиция любой нашей страницы по этому запросу
- ranking_url      — какая наша страница вышла по факту

Отсюда вердикты:
- работает    — статья в топ-10 по своему запросу
- на подходе  — позиция 11–30, видно только при --n 30
- подменилась — в топе наша другая страница вместо целевой. Это каннибализация,
                и это главная находка такого отчёта
- не вышла    — ни целевой, ни другой нашей страницы в снятом топе нет
- нет данных  — замер не удался

Эффективность здесь = позиция целевой страницы по целевому запросу. Клики и
показы дают Вебмастер и Метрика (`yaseo webmaster`, `yaseo metrika`); этот
отчёт их не подменяет выдуманной метрикой и говорит об этом вслух.

Публичный API:
- check_article(article, region, n, run_id) -> list[ArticleVerdict]
- check_all(project_id, region, n, progress) -> list[ArticleVerdict]
- report(project_id)                        -> list[dict]   (только чтение базы)
- effectiveness_summary(project_id)         -> dict
- in_production(project_id)                 -> list[dict]   (материалы в работе)
- cannibalization(query, project_id)        -> dict         (занят ли запрос)
- normalize_url(url) / page_domain(url)     — сравнение адресов

Запуск как скрипт:
    yaseo articles --add --project 1 --url https://example.ru/blog/x \\
        --query "остекление балкона" --title "Остекление балкона"
    yaseo articles --list   --project 1
    yaseo articles --check  --project 1 --n 30
    yaseo articles --report --project 1
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable
from urllib.parse import unquote, urlsplit

from . import config, morph, storage
from . import estimate as smeta
from .competition import _is_pseudo, score_serp
from .yandex_serp import RUSSIA, SerpResult, serp, serp_batch

#: Умолчание потолка обращений за один прогон `check_all`, пока не задано
#: настройкой «articles_max_calls» или переменной `YASEO_ARTICLES_MAX_CALLS`.
DEFAULT_MAX_CALLS = config.TRACKING_DEFAULTS["articles_max_calls"]


def max_calls() -> int:
    """
    Потолок обращений к Search API за один прогон `check_all`.

    Старшинство то же, что у `tracker.max_calls`: переменная окружения
    `YASEO_ARTICLES_MAX_CALLS` → настройка «articles_max_calls» в файле
    трекера (`config.tracking_file()`) → умолчание. Окружение сильнее файла,
    чтобы разовый большой прогон можно было разрешить, не правя настройку
    насовсем.
    """
    raw = config.env_int("YASEO_ARTICLES_MAX_CALLS")
    if raw is not None:
        return max(1, raw)
    try:
        return max(1, int(config.tracking_settings().get("articles_max_calls")))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CALLS

#: Граница «работает». Ниже — статья есть, но её не видят.
TOP = 10
#: Граница «на подходе». Чтобы её увидеть, снимать надо с --n 30.
NEAR = 30

WORKS = "работает"
COMING = "на подходе"
REPLACED = "подменилась"
MISSING = "не вышла"
NO_DATA = "нет данных"

VERDICTS = (WORKS, COMING, REPLACED, MISSING, NO_DATA)

#: Причины «замер не удался» и «замер не сохранён» печатаются человеку, и имя
#: питоновского класса («OperationalError: database is locked») в колонке
#: «доказательство» не объясняет ничего: оно называет внутренность
#: инструмента, а не то, что произошло с замером.
#:
#: Само сообщение ошибки остаётся — в нём и лежит объяснение. Убирается только
#: имя класса; у пустых сообщений (`TimeoutError()`, `KeyError('position')`)
#: вместо него подставляется человеческая фраза, и лишь если её нет ни там, ни
#: там, печатается класс — молчание было бы хуже жаргона.
БЕЗ_СЛОВ = {
    "TimeoutError": "истекло время ожидания",
    "ConnectionError": "не удалось соединиться",
    "ConnectionResetError": "соединение оборвалось",
    "BrokenPipeError": "соединение оборвалось",
    "OSError": "отказала сама машина",
    "MemoryError": "не хватило памяти",
}


def _сбой(e: BaseException) -> str:
    """Причина отказа словами человека, без имени питоновского класса."""
    имя = type(e).__name__
    текст = " ".join(str(e).split())
    if isinstance(e, KeyError):
        return f"в данных нет поля {текст}" if текст else БЕЗ_СЛОВ.get(имя, имя)
    return текст or БЕЗ_СЛОВ.get(имя, имя)


#: Честная формулировка того, что этот отчёт измеряет, а что нет.
BASIS = (
    "Эффективность здесь = позиция целевой страницы по целевому запросу. "
    "Клики и показы в этот отчёт не входят: их дают Вебмастер и Метрика."
)


@dataclass
class ArticleVerdict:
    article_id: int
    url: str
    title: str | None
    query: str
    position: int | None
    domain_position: int | None
    ranking_url: str | None
    competition: float | None
    is_target: bool
    verdict: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


# ──────────────────────────── адреса ────────────────────────────

def normalize_url(url: str | None) -> str:
    """
    Приводит адрес к сравнимому виду: без схемы, без www., без порта,
    без query-строки и фрагмента, без хвостового слеша, с раскодированным путём.

    https://example.ru/blog/x/ и http://www.example.ru/blog/x — одна страница.
    """
    if not url:
        return ""
    raw = url.strip()
    parts = urlsplit(raw if "//" in raw else "//" + raw)
    host = (parts.netloc or "").lower().rsplit("@", 1)[-1].split(":", 1)[0]
    host = host.removeprefix("www.")
    path = unquote(parts.path or "").rstrip("/")
    return f"{host}{path}"


def page_domain(url: str | None) -> str:
    """Домен берётся из адреса самой статьи: она может жить на поддомене."""
    return normalize_url(url).split("/", 1)[0]


def _short(url: str | None) -> str:
    """Путь без домена — так короче и видно, какая именно страница вышла."""
    if not url:
        return "—"
    host, _, path = normalize_url(url).partition("/")
    return f"/{path}" if path else host


def _doc_host(doc) -> str:
    """Хост документа выдачи. Адрес надёжнее поля domain: оно бывает групповым."""
    host = page_domain(doc.url)
    return host or (doc.domain or "").lower().removeprefix("www.")


# ──────────────────────────── разбор выдачи ────────────────────────────

def _measure_serp(article_url: str, res: SerpResult) -> tuple[int | None, int | None, str | None]:
    """Возвращает (позиция статьи, позиция любой нашей страницы, её URL)."""
    target = normalize_url(article_url)
    our = page_domain(article_url)

    position: int | None = None
    domain_position: int | None = None
    ranking_url: str | None = None

    for doc in res.docs:
        if _is_pseudo(doc):  # блоки колдунщиков — не страницы
            continue
        if _doc_host(doc) != our:
            continue
        if domain_position is None:
            domain_position, ranking_url = doc.position, doc.url
        if position is None and normalize_url(doc.url) == target:
            position = doc.position

    return position, domain_position, ranking_url


def classify(
    position: int | None,
    domain_position: int | None,
    ranking_url: str | None,
    found_all: int | None = None,
    depth: int | None = None,
) -> tuple[str, str]:
    """
    Вердикт и доказательство к нему. Отдельная функция, потому что вердикт
    считается и по живой выдаче, и по сохранённому замеру — правило одно.

    depth = None означает «глубина замера неизвестна»: в сохранённой строке её
    нет, и придумывать «топ-30» вместо неё — врать.
    """
    where = f"топ-{depth}" if depth else "снятом топе"

    if position is not None and position <= TOP:
        reason = f"в топе на {position} позиции"
        if domain_position is not None and domain_position < position:
            reason += f"; выше на {domain_position} наша {_short(ranking_url)}"
        return WORKS, reason

    if domain_position is not None and (position is None or domain_position < position):
        reason = f"вместо неё на {domain_position} позиции {_short(ranking_url)}"
        reason += (
            f"; сама статья на {position}"
            if position is not None
            else f"; самой статьи в {where} нет"
        )
        return REPLACED, reason

    if position is not None and position <= NEAR:
        return COMING, f"на {position} позиции, до топ-{TOP} не дотянула"

    if position is not None:
        return MISSING, f"нашлась только на {position} позиции, это дальше топ-{NEAR}"

    tail = f", всего найдено {found_all}" if found_all else ""
    return MISSING, f"в {where} нет ни статьи, ни другой нашей страницы{tail}"


# ──────────────────────────── замер ────────────────────────────

def _queries(article: dict) -> list[str]:
    """Целевой запрос плюс дополнительные, без повторов и пустых."""
    extra = article.get("extra_queries") or []
    if isinstance(extra, str):
        extra = [extra]
    out = [article["target_query"], *extra]
    return list(dict.fromkeys(q.strip() for q in out if q and q.strip()))


def _resolve(article) -> dict | None:
    """Принимает словарь статьи или её id."""
    if isinstance(article, dict):
        return article
    try:
        aid = int(article)
    except (TypeError, ValueError):
        return None
    return next((a for a in storage.list_articles(active_only=False) if a["id"] == aid), None)


def _save_snapshot(res: SerpResult, run_id: int | None) -> int | None:
    """Снимок кладём всегда: потом можно посмотреть, кто стоял рядом."""
    try:
        return storage.save_serp(res, run_id=run_id)
    except Exception as e:  # noqa: BLE001 — снимок не записался, замер всё равно нужен
        print(f"снимок выдачи не сохранён: {_сбой(e)}", file=sys.stderr)
        return None


def _record(
    article: dict,
    query: str,
    res: SerpResult,
    snapshot_id: int | None,
    n: int,
    run_id: int | None,
) -> ArticleVerdict:
    """Считает вердикт и сохраняет замер. Не сохранил — замера не было."""
    base = dict(
        article_id=article["id"],
        url=article["url"],
        title=article.get("title"),
        query=query,
    )

    if res.error:
        v = ArticleVerdict(
            **base, position=None, domain_position=None, ranking_url=None,
            competition=None, is_target=False,
            verdict=NO_DATA, reason=f"замер не удался: {res.error}",
        )
    else:
        position, domain_position, ranking_url = _measure_serp(article["url"], res)
        comp = score_serp(res, our_domain=page_domain(article["url"]), top=min(n, TOP))
        verdict, reason = classify(
            position, domain_position, ranking_url, found_all=res.found_all, depth=n
        )
        v = ArticleVerdict(
            **base,
            position=position,
            domain_position=domain_position,
            ranking_url=ranking_url,
            competition=None if comp.error else comp.score,
            is_target=position is not None and position == domain_position,
            verdict=verdict,
            reason=reason,
        )

    try:
        storage.save_article_check(
            article_id=v.article_id, query=v.query, position=v.position,
            domain_position=v.domain_position, ranking_url=v.ranking_url,
            competition=v.competition, is_target=v.is_target,
            run_id=run_id, snapshot_id=snapshot_id,
        )
    except Exception as e:  # noqa: BLE001 — наружу ошибку не бросаем
        return ArticleVerdict(
            **base, position=v.position, domain_position=v.domain_position,
            ranking_url=v.ranking_url, competition=v.competition, is_target=v.is_target,
            verdict=NO_DATA, reason=f"замер не сохранён: {_сбой(e)}",
        )
    return v


def check_article(
    article, region: int = RUSSIA, n: int = 10, run_id: int | None = None
) -> list[ArticleVerdict]:
    """Замер одной статьи по её целевому и дополнительным запросам."""
    storage.init_db()
    art = _resolve(article)
    if not art:
        return []

    out: list[ArticleVerdict] = []
    for q in _queries(art):
        try:
            res = serp(q, region=region, n=n)
        except Exception as e:  # noqa: BLE001
            res = SerpResult(
                query=q, region=region, fetched_at=storage.now(), found_all=0,
                error=_сбой(e),
            )
        out.append(_record(art, q, res, _save_snapshot(res, run_id), n, run_id))
    return out


def _unique_queries(project_id: int | None) -> tuple[list, list, list[str]]:
    """Статьи, пары «статья — запрос» и схлопнутый по запросу список для сметы и прогона."""
    arts = storage.list_articles(project_id)
    pairs = [(a, q) for a in arts for q in _queries(a)]
    queries = list(dict.fromkeys(q for _, q in pairs))
    return arts, pairs, queries


def check_estimate(project_id: int | None = None, region: int = RUSSIA,
                   n: int = 10, cap: int | None = None) -> dict:
    """
    Смета одного прогона `check_all` — до единого платного обращения.

    Считает по уникальным запросам, ровно как их снимает сам `check_all`
    (`serp_batch` по схлопнутому списку): запрос, целевой для нескольких
    статей, стоит один раз, а не по разу за каждую статью.

    Обращений на запрос больше одного, если глубина `n` больше 10 —
    `estimate.pages_for_depth`, тот же расчёт, что у трекера позиций.
    """
    storage.init_db()
    cap = int(cap if cap is not None else max_calls())
    arts, _, queries = _unique_queries(project_id)
    pages = smeta.pages_for_depth(n)
    calls = len(queries) * pages

    из_чего = f"{len(queries)} {smeta.plural(len(queries), 'запрос', 'запроса', 'запросов')}"
    if pages > 1:
        из_чего += (f" × {pages} {smeta.plural(pages, 'страница', 'страницы', 'страниц')} "
                    f"(глубина топ-{n}) = {calls} "
                    f"{smeta.plural(calls, 'обращение', 'обращения', 'обращений')}")

    накоплено = (f"статей на отслеживании: {len(arts)}" if arts
                else "статей на отслеживании ещё нет")

    return smeta.estimate(
        "search-api", calls,
        project=(f"проект {project_id}" if project_id is not None else "все проекты"),
        из_чего=из_чего if queries else "запросов нет — статей ещё нет или без целевых фраз",
        накоплено=накоплено, потолок=cap,
    )


@dataclass
class ArticlesRun:
    """
    Итог прогона `check_all` — тем же способом, что `tracker.TrackingRun`.

    `checked` пуст и при «замерять нечего», и при остановке по потолку:
    различает их поле `stopped` — заполнено, только когда прогон остановлен
    ДО первого запроса и деньги не потрачены.
    """
    checked: list[ArticleVerdict] = field(default_factory=list)
    #: Сколько обращений к Search API прогон собирался сделать.
    calls_planned: int = 0
    #: Заполнено — прогон остановлен до первой траты, и здесь сказано почему.
    stopped: str | None = None


def check_all(
    project_id: int | None = None,
    region: int = RUSSIA,
    n: int = 10,
    progress: bool = True,
    cap: int | None = None,
) -> ArticlesRun:
    """
    Замер всех активных статей пакетом. Выдача по одному запросу снимается
    один раз, даже если запрос целевой для нескольких статей: снимок один,
    замеров по нему столько, сколько статей.

    `cap` — потолок обращений к Search API за прогон (умолчание —
    `max_calls()`). Число статей и дополнительных запросов у них не
    ограничено ничем другим, и без потолка прогон растёт незаметно: `n=30`
    у бригады постраничной выдачи стоит три обращения на запрос, а не одно
    (`estimate.pages_for_depth`), и цена скачет вместе с глубиной. Прогон,
    который в потолок не укладывается, останавливается ДО первого запроса —
    как `tracker.run_tracking` останавливается по `cap`.
    """
    storage.init_db()
    _, pairs, queries = _unique_queries(project_id)
    if not pairs:
        return ArticlesRun()

    pages = smeta.pages_for_depth(n)
    planned = len(queries) * pages

    limit = max_calls() if cap is None else int(cap)
    if limit and planned > limit:
        why = (
            f"проверка остановлена до первого запроса: {len(queries)} "
            f"{smeta.plural(len(queries), 'запрос', 'запроса', 'запросов')}"
            + (f" × {pages} {smeta.plural(pages, 'страница', 'страницы', 'страниц')} "
               f"(глубина топ-{n})" if pages > 1 else "")
            + f" = {planned} {smeta.plural(planned, 'обращение', 'обращения', 'обращений')} "
              f"к Yandex Search API при потолке {limit}. Ничего не потрачено. "
              f"Поднять потолок — «articles_max_calls» в {config.tracking_file()} "
              f"или YASEO_ARTICLES_MAX_CALLS на один прогон"
        )
        if progress:
            print(why, file=sys.stderr)
        return ArticlesRun(calls_planned=planned, stopped=why)

    run_id = storage.start_run(
        "articles", region=region,
        note=f"статьи, проект {project_id if project_id is not None else 'все'}, топ-{n}",
    )

    results = {r.query: r for r in serp_batch(queries, region=region, n=n, progress=progress)}
    snapshots: dict[str, int | None] = {}

    out: list[ArticleVerdict] = []
    for art, q in pairs:
        res = results.get(q) or SerpResult(
            query=q, region=region, fetched_at=storage.now(), found_all=0,
            error="выдача по запросу не снята",
        )
        if q not in snapshots:
            snapshots[q] = _save_snapshot(res, run_id)
        out.append(_record(art, q, res, snapshots[q], n, run_id))

    storage.finish_run(run_id)
    return ArticlesRun(checked=out, calls_planned=planned)


# ──────────────────────────── материалы в работе ────────────────────────────
#
# Реестр `storage.articles` знает только вышедшие страницы: строка появляется,
# когда у статьи есть адрес. Пока материал пишется, адреса у него нет — и для
# реестра его не существует. Тогда проверка на каннибализацию отвечала бы
# «свободно» и разрешала завести вторую статью под запрос, который уже занят
# материалом в работе.
#
# Где живут материалы в работе, пакет не знает: у каждого свой редакционный
# процесс. Поэтому здесь точка подключения — `production_source`.

#: Источник материалов в работе: `project_id -> [{"art_id", "topic", "stage",
#: "status", "project_id"}, ...]`. По умолчанию пусто. Подключение:
#: `articles.production_source = my_reader`.
production_source: Callable[[int | None], list[dict]] = lambda pid: []

#: Человеческие имена шагов редакционного процесса: `{"draft": "черновик"}`.
#: Точка подключения; без словаря печатается сам код шага.
STEP_WORD: dict[str, str] = {}


def in_production(project_id: int | None = None) -> list[dict]:
    """
    Материалы в работе: то, что уже заявлено под запрос, но ещё не вышло.

    Единственный источник ответа «под этот запрос уже что-то идёт» —
    `production_source`. Запрос материала берётся из поля `topic`.
    Источник, который упал или вернул мусор, не роняет проверку: материалы
    без темы пропускаются.
    """
    out: list[dict] = []
    try:
        items = production_source(project_id) or []
    except Exception as exc:  # noqa: BLE001 — чужой источник не роняет проверку
        print(f"материалы в работе не прочитаны: {_сбой(exc)}", file=sys.stderr)
        return out
    for card in items:
        if not isinstance(card, dict):
            continue
        topic = str(card.get("topic") or "").strip()
        if not topic:
            continue
        owner = card.get("project_id")
        if project_id is not None and owner is not None and int(owner) != int(project_id):
            continue
        out.append({
            "art_id": card.get("art_id") or card.get("id"),
            "topic": topic,
            "stage": card.get("stage"),
            "status": card.get("status"),
            "project_id": owner,
        })
    return out


# ──────────────────────────── чтение ────────────────────────────

def report(project_id: int | None = None) -> list[dict]:
    """
    Последний замер по каждой статье с вердиктом и дельтой. Только чтение базы,
    к API не ходит: отчёт можно строить сколько угодно раз.
    """
    rows = storage.articles_latest(project_id)
    out = []
    for r in rows:
        if not r.get("checked_at"):
            verdict, reason = NO_DATA, "замеров ещё не было"
        else:
            verdict, reason = classify(r["position"], r["domain_position"], r["ranking_url"])
        out.append({**r, "verdict": verdict, "reason": reason})
    return out


def effectiveness_summary(project_id: int | None = None) -> dict:
    """Сводка: сколько статей работает, сколько мимо, сколько каннибализируется."""
    rows = report(project_id)
    counts = {v: 0 for v in VERDICTS}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    return {
        "articles": len(rows),
        "measured": sum(1 for r in rows if r.get("checked_at")),
        "works": counts[WORKS],
        "coming": counts[COMING],
        "replaced": counts[REPLACED],
        "missing": counts[MISSING],
        "no_data": counts[NO_DATA],
        "cannibalized": [
            {
                "article_id": r["id"],
                "url": r["url"],
                "query": r["target_query"],
                "ranking_url": r["ranking_url"],
                "domain_position": r["domain_position"],
            }
            for r in rows
            if r["verdict"] == REPLACED
        ],
        "basis": BASIS,
    }


# ──────────────────────────── вывод ────────────────────────────

def _fmt_pos(v) -> str:
    return "—" if v is None else str(v)


def _fmt_delta(v) -> str:
    if v is None:
        return "—"
    if v > 0:
        return f"↑ {v}"
    if v < 0:
        return f"↓ {abs(v)}"
    return "="


def _print_report(rows: list[dict], summary: dict) -> None:
    if not rows:
        print("Статей нет. Сначала: --add --project <id> --url <url> --query <запрос>")
        return

    print(f"\n  {'статья':<34} {'целевой запрос':<32} {'поз':>5} {'вердикт':<12} "
          f"{'дельта':>7}  дата")
    for r in sorted(rows, key=lambda r: (r["position"] is None, r["position"] or 999)):
        name = (r["title"] or _short(r["url"]))[:34]
        date = (r["checked_at"] or "")[:10] or "—"
        print(
            f"  {name:<34} {r['target_query'][:32]:<32} {_fmt_pos(r['position']):>5} "
            f"{r['verdict']:<12} {_fmt_delta(r['delta']):>7}  {date}"
        )

    print("\n  Доказательства:")
    for r in rows:
        print(f"  · {_short(r['url'])} — {r['reason']}")

    s = summary
    print(
        f"\n  Работает {s['works']}, подменилось {s['replaced']}, "
        f"на подходе {s['coming']}, не вышло {s['missing']} из всего {s['articles']}."
    )
    if s["no_data"]:
        print(f"  Без данных: {s['no_data']}.")
    if s["cannibalized"]:
        print("\n  Каннибализация — вместо целевой статьи выходит другая наша страница:")
        for c in s["cannibalized"]:
            print(
                f"  · {c['query']!r}: ждали {_short(c['url'])}, "
                f"вышла {_short(c['ranking_url'])} на {c['domain_position']}"
            )
    print(f"\n  {BASIS}")


def _print_checks(rows: list[ArticleVerdict]) -> None:
    print(f"\n  {'статья':<30} {'запрос':<32} {'поз':>5} {'дом':>5} {'вердикт':<12}  доказательство")
    for v in rows:
        print(
            f"  {_short(v.url)[:30]:<30} {v.query[:32]:<32} {_fmt_pos(v.position):>5} "
            f"{_fmt_pos(v.domain_position):>5} {v.verdict:<12}  {v.reason}"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Эффективность статей блога yaseo")
    p.add_argument("--add", action="store_true", help="завести статью")
    p.add_argument("--remove", type=int, help="снять статью с отслеживания по id")
    p.add_argument("--list", action="store_true", help="показать заведённые статьи")
    p.add_argument("--check", action="store_true", help="снять замер по статьям")
    p.add_argument("--report", action="store_true", help="отчёт по последним замерам")
    p.add_argument("--project", type=int, help="id проекта")
    p.add_argument("--url", help="адрес статьи")
    p.add_argument("--query", help="целевой запрос статьи")
    p.add_argument("--extra", action="append", default=[], help="дополнительный запрос")
    p.add_argument("--title", help="название статьи")
    p.add_argument("--published", help="дата публикации, ISO")
    p.add_argument("--region", type=int, default=RUSSIA)
    p.add_argument("--n", type=int, default=10, help="глубина снятия: 30 покажет «на подходе»")
    p.add_argument("--dry", action="store_true",
                   help="показать смету --check и ничего не потратить")
    p.add_argument("--max-calls", type=int, default=None,
                   help="потолок обращений за прогон --check; 0 — снять потолок")
    p.add_argument("--json", action="store_true", help="выдать JSON вместо таблицы")
    args = p.parse_args(argv)

    storage.init_db()

    if args.add:
        if not (args.project and args.url and args.query):
            p.error("для --add нужны --project, --url и --query")
        aid = storage.add_article(
            project_id=args.project, url=args.url, target_query=args.query,
            title=args.title, extra_queries=args.extra, published_at=args.published,
        )
        print(f"статья {aid}: {args.url} → {args.query!r}")

    if args.remove:
        storage.remove_article(args.remove)
        print(f"статья {args.remove} снята с отслеживания")

    if args.list:
        arts = storage.list_articles(args.project)
        print(f"Статей на отслеживании: {len(arts)}")
        for a in arts:
            extra = f"  +{len(a['extra_queries'])} доп." if a["extra_queries"] else ""
            print(f"  {a['id']:>3}  {a['url']}\n       {a['target_query']!r}{extra}")

    if args.check:
        cap = max_calls() if args.max_calls is None else int(args.max_calls)
        est = check_estimate(args.project, region=args.region, n=args.n, cap=cap)
        print(smeta.text(est))
        if args.dry:
            print("\n--dry: ничего не потрачено, проверка не запускалась.")
        else:
            res = check_all(args.project, region=args.region, n=args.n, cap=cap)
            if res.stopped:
                print(f"\n{res.stopped}", file=sys.stderr)
                return 1
            rows = res.checked
            if not rows:
                print("Статей нет — замерять нечего.", file=sys.stderr)
                return 1
            _print_checks(rows)
            print(f"\n  Снято замеров: {len(rows)}, глубина топ-{args.n}, регион {args.region}.")

    if args.report:
        rows = report(args.project)
        summary = effectiveness_summary(args.project)
        if args.json:
            print(json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False, indent=2))
        else:
            _print_report(rows, summary)

    if not any([args.add, args.remove, args.list, args.check, args.report]):
        p.print_help()
        return 1
    return 0


# ── занят ли запрос нашей же страницей ───────────────────────────────────────
#
# Функция стоит только на `articles`, `morph`, `storage` и отвечает «занят ли
# запрос нашей же страницей», без новых платных запросов.

def cannibalization(query: str, project_id: int | None = None,
                    region: int = RUSSIA,
                    exclude_art_id: str | None = None) -> dict:
    """
    Занят ли запрос нашей же страницей или нашим же материалом.

    Три проверки, все по накопленным данным, без новых платных запросов:

    1. Есть ли уже наша вышедшая статья, заявленная под этот запрос. Ключ
       сравнения — основы слов (`morph`), а не строка: «остекление балкона» и
       «остекления балконов» одна потребность.
    2. Есть ли материал в работе под этот же запрос (`in_production`).
    3. Кто из наших страниц реально стоит в выдаче по запросу. Если
       ранжируется страница услуги, новая статья будет отбирать у неё показы,
       а не добавлять свои.

    Реестр вышедших страниц создаёт строку по адресу — то есть после
    публикации. Без пункта 2 материал почти на выпуске не занимал бы запрос, и
    проверка разрешала бы вторую статью. Занятость — это про наши намерения
    тоже, а не только про наш индекс.

    `exclude_art_id` — материал, который спрашивает про СВОЙ запрос. Без него
    материал находит среди материалов в работе сам себя и получает «занято» на
    собственную тему. Кто ведёт материал, обязан назваться; кто
    спрашивает про новую тему, не передаёт ничего — и тогда любой материал в
    работе считается занявшим запрос.

    Вердикт `занято` — это «не писать», а не «имей в виду».
    """
    key = morph.stem_key(query)
    registry = storage.list_articles(project_id)

    claimed = [
        a for a in registry
        if morph.stem_key(a.get("target_query") or "") == key
    ]

    # Материал в работе без адреса в реестре не значится, но запрос уже занял.
    #
    # Стадия `done` отсюда НЕ вычитается, хотя вышедший материал должен был бы
    # найтись и в реестре: строки в реестре у него может и не быть, и тогда
    # запрос снова читался бы «свободно». Один и тот же
    # материал, названный дважды, — это повтор в тексте; материал, не названный
    # ни разу, — это вторая статья на занятую фразу.
    in_work = [
        m for m in in_production(project_id)
        if morph.stem_key(m["topic"]) == key
        and m["art_id"] != exclude_art_id
    ]

    # Поля берутся из `storage.articles_latest`: `ranking_url` — какая наша
    # страница реально стоит в выдаче, `domain_position` — на каком месте,
    # `is_target` — та ли это страница, под которую запрос заявляли.
    ranking: list[dict] = []
    for row in storage.articles_latest(project_id):
        if morph.stem_key(row.get("target_query") or "") != key:
            continue
        if row.get("ranking_url") or row.get("domain_position") is not None:
            ranking.append({
                "url": row.get("url"),
                "position": row.get("position"),
                "domain_position": row.get("domain_position"),
                "ranking_url": row.get("ranking_url"),
                "is_target": row.get("is_target"),
            })

    winners = {r["ranking_url"] for r in ranking if r.get("ranking_url")}

    # Причины перечисляются все, а не первая попавшаяся: страница в индексе и
    # материал в цехе — разные поводы не писать, и человеку нужны оба, чтобы
    # понимать, что именно делать — разводить страницы или ждать выпуска.
    reasons: list[str] = []
    if claimed:
        reasons.append(
            f"под этот запрос уже заявлено материалов: {len(claimed)} "
            f"({', '.join(a['url'] for a in claimed[:3])}). Вторая статья на ту же "
            "потребность делит показы, а не добавляет их."
        )
    if in_work:
        # Человеку печатается имя шага, а не машинный ключ («на стадии done»):
        # словарь имён — `STEP_WORD`.
        def шаг(код) -> str:
            код = str(код or "").strip()
            return (f"на шаге «{STEP_WORD.get(код, код)}»" if код
                    else "шаг не назван")

        named = "; ".join(
            f"{m['art_id']} «{m['topic']}» {шаг(m['stage'])}"
            for m in in_work[:3]
        )
        more = f" и ещё {len(in_work) - 3}" if len(in_work) > 3 else ""
        reasons.append(
            f"под этот запрос уже заведён материал в работе: {named}{more}. "
            "Вторая тема на ту же потребность делит показы с ним, а не "
            "добавляет их."
        )
    if winners:
        reasons.append(
            "по запросу уже ранжируется наша страница: "
            f"{', '.join(sorted(winners)[:3])}. Новая статья будет отбирать показы у неё."
        )

    verdict = "занято" if reasons else "свободно"
    why = " ".join(reasons) if reasons else (
        "ни одна наша страница на этот запрос не заявлена, в работе под него "
        "материала нет и в выдаче по нему наша страница не найдена"
    )

    return {
        "query": query,
        "stem_key": key,
        "verdict": verdict,
        "why": why,
        "claimed_by": [{"url": a["url"], "title": a.get("title")} for a in claimed],
        "in_production": in_work,
        "ranking": ranking,
        "checked": ("по реестру вышедших страниц, материалам в работе и "
                    "накопленным замерам, без новых запросов к API"),
    }


if __name__ == "__main__":
    sys.exit(main())
