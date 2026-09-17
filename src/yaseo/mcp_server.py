#!/usr/bin/env python3
"""
MCP-сервер yaseo. Без внешних зависимостей — чистый JSON-RPC 2.0 поверх stdio.

Даёт агентам (Claude Code, Cursor и другим MCP-клиентам) доступ к источникам:
- Wordstat — частотность по РФ
- Yandex Search API — выдача, конкурентность, поисковые конкуренты

Имена инструментов повторяют контракт OpenSEO, чтобы скиллы переносились
без переписывания: whoami, research_keywords, get_keyword_metrics,
get_serp_results, find_serp_competitors.

Инструменты ядра — `CORE_TOOLS`. Дополнительные наборы подключаются
модулем `yaseo.geo` (`_extra_tools`), если он установлен.

Запуск (MCP-клиент делает это сам):
    yaseo-mcp

Ручная проверка:
    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | yaseo-mcp
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

from . import __version__, articles, config, net, storage
from .env import MissingKeysError
from .errors import UpstreamError
from .competition import WEIGHTS, score_batch, serp_competitors
from .tracker import min_interval_days, movers, report, run_tracking
from .untrusted import clean, with_notice
from .wordstat import frequencies, latest_snapshot
from .yandex_serp import RUSSIA, serp

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "yaseo"

DISCLAIMER = (
    "Частотность — Wordstat (Яндекс, РФ). Конкурентность — собственный расчёт "
    "по составу выдачи Яндекса; ссылочный вес в неё не входит. "
    "Веса факторов — проектное решение, не измерение."
)


#: Отказ источника: инструмент целиком не получил данных. Отдаётся с isError.
ОтказИсточника = UpstreamError


def _u(url, n: int = 300) -> str:
    """Адрес для таблицы: percent-кодированный (`net.safe_url(display=True)`) —
    точный и инертный в Markdown; `clean` только ограничивает длину."""
    return clean(net.safe_url(str(url or ""), display=True), n) if url else ""


def _first_line(text: str | None) -> str:
    return clean((str(text or "").strip().splitlines() or ["причина не названа"])[0], 300)


def _c(text, n: int = 160) -> str:
    """Внешняя строка в ячейку таблицы или строку вывода: без `|`, переводов
    строк, обратных кавычек и кавычек (`untrusted.clean`)."""
    return clean(text, n)


#: Верхние границы параметров. Сверх них инструмент отказывает по входу:
#: обход в тысячу страниц или выдача на сотню документов — это минуты и
#: деньги, которых человек не заказывал.
MAX_AUDIT_PAGES = 200
MAX_SERP_DOCS = 50
MAX_ARTICLE_DEPTH = 50
MAX_PLAN_LIMIT = 200


class ВходНеверен(ValueError):
    """Отказ инструмента по некорректному входу — не сбой сервера, а отказ,
    который агент на той стороне обязан увидеть как ошибку (`isError`),
    а не прочитать как обычный успешный ответ.

    Текст вида «Нужен domain.» без признака ошибки агент читает как нормальный
    ответ. Поэтому инструменты поднимают это исключение, а `handle()` ловит его
    отдельной веткой и метит `isError: True`, сохраняя человеческий текст.
    """


def _целое(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    """Целый параметр в границах [lo, hi]; иначе отказ по входу с подсказкой."""
    v = args.get(key, default)
    if v is None:
        return default
    if isinstance(v, bool):
        raise ВходНеверен(f"{key} — целое число от {lo} до {hi}.")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ВходНеверен(f"{key} — целое число от {lo} до {hi}, получено «{_c(v, 40)}».") from None
    if not lo <= n <= hi:
        raise ВходНеверен(f"{key} — от {lo} до {hi}, получено {n}.")
    return n


# ──────────────────────────── инструменты ────────────────────────────

def tool_whoami(_: dict) -> str:
    """Конфигурация: откуда ключи, где база, чего не хватает. Ничего не тратит."""
    from . import env as _env

    d = _env.describe()
    ключи = d["ключи"]

    def строка(k: str) -> str:
        v = ключи[k]
        return f"- {k}: " + (f"задан ({v['источник']})" if v["задан"] else "не задан")

    lines = [f"yaseo {__version__}", ""]
    missing = d["не_хватает_для_выдачи"]
    if missing:
        lines += ["Без ключей уже работают: проверка сайта (audit_site), готовность к ИИ-поиску "
                  "(geo_readiness) и план правок (get_action_plan). Ключи Яндекса нужны для выдачи, "
                  "Wordstat и позиций.", "",
                  "Выдача и Wordstat: не готово.", _env.missing_keys_message(missing), ""]
    else:
        values, _ = _env.read_all()
        lines += [f"Выдача и Wordstat: готово, folderId {values['YC_FOLDER_ID'][:6]}…", ""]

    lines.append("Ключи (сами значения не печатаются):")
    lines += [строка(k) for k in _env.SEARCH_KEYS]
    oauth_ready = ключи["YANDEX_OAUTH_TOKEN"]["задан"]
    lines.append("Вебмастер и Метрика (необязательно): "
                 + ("токен есть" if oauth_ready else "токена нет — эти отчёты недоступны"))
    lines += [строка(k) for k in _env.OAUTH_KEYS]
    lines += ["", "Где искались файлы с ключами (по старшинству):"]
    lines += [f"- {p}" + ("  ← найден" if p in d["найденные_файлы"] else "")
              for p in d["файлы_по_старшинству"]]

    lines += ["", f"База: {config.db_path()}"]
    try:
        dom = config.default_domain()
        lines.append(f"Домен по умолчанию: {dom}")
    except config.DomainNotSetError as e:
        lines.append(f"Домен по умолчанию: не задан — {e}")

    path, freqs = latest_snapshot()
    snap = f"{path.name}, сидов {len(freqs)}" if path else "снапшотов нет"
    lines += [f"Последний снапшот Wordstat: {snap}", "", DISCLAIMER]
    return "\n".join(lines)


def tool_research_keywords(args: dict) -> str:
    seeds = args.get("seeds") or []
    if not seeds:
        raise ВходНеверен("Нужен хотя бы один seed в поле seeds.")
    region = str(args.get("region", "225"))
    min_freq = int(args.get("min_freq", 30))
    limit = int(args.get("limit", 40))

    rows = frequencies(seeds[:5], region=region, num=200)
    if rows and all(f.error for f in rows):
        raise ОтказИсточника(
            f"Wordstat не ответил ни по одной из {len(rows)} фраз: "
            f"{_first_line(rows[0].error)}. Ничего не получено; проверьте ключи — whoami.")
    out: list[str] = [f"Расширение по {len(rows)} seed, регион {region}. {DISCLAIMER}", ""]

    for f in rows:
        if f.error:
            out.append(f"### {_c(f.phrase)} — ОШИБКА: {_first_line(f.error)}")
            continue
        out.append(f"### {_c(f.phrase)} — частотность {f.freq}")
        picked = sorted([e for e in f.expansions if e.freq >= min_freq], key=lambda e: -e.freq)[:limit]
        if not picked:
            out.append(f"нет расширений с частотностью ≥ {min_freq}")
        else:
            out.append("| фраза | частотность | тип |")
            out.append("|---|---:|---|")
            out += [f"| {_c(e.phrase)} | {e.freq} | {_c(e.kind, 40)} |" for e in picked]
        out.append("")
    return "\n".join(out)


def tool_get_keyword_metrics(args: dict) -> str:
    keywords = args.get("keywords") or []
    if not keywords:
        raise ВходНеверен("Нужен список keywords.")
    region = int(args.get("region", RUSSIA))
    with_competition = bool(args.get("with_competition", True))
    keywords = keywords[:25]

    freqs = {f.phrase: f for f in frequencies(keywords, region=str(region), num=1)}

    comps: dict[str, Any] = {}
    if with_competition:
        scored, _ = score_batch(keywords, region=region, n=10, progress=False)
        comps = {c.query: c for c in scored}

    freq_err = {kw: (freqs[kw].error if kw in freqs else "ответа нет") for kw in keywords
                if kw not in freqs or freqs[kw].error}
    comp_err = {kw: (comps[kw].error if kw in comps else "ответа нет") for kw in keywords
                if with_competition and (kw not in comps or comps[kw].error)}
    if len(freq_err) == len(keywords) and (not with_competition or len(comp_err) == len(keywords)):
        why = next(iter(freq_err.values()))
        raise ОтказИсточника(
            f"Ни по одному из {len(keywords)} запросов данных не получено: Wordstat — "
            f"{_first_line(why)}"
            + (f"; выдача — {_first_line(next(iter(comp_err.values())))}" if comp_err else "")
            + ". Проверьте ключи — whoami.")

    lines = [
        f"Метрики по {len(keywords)} запросам, регион {region}.",
        DISCLAIMER,
        "",
        "| запрос | частотность | конкурентность | полоса | наша позиция |",
        "|---|---:|---:|---|---:|",
    ]
    for kw in keywords:
        f = freqs.get(kw)
        freq = "нет данных" if not f or f.freq is None else str(f.freq)
        c = comps.get(kw)
        if not with_competition:
            comp, band, pos = "не запрошено", "—", "—"
        elif not c or c.error:
            comp, band, pos = "нет данных", "—", "—"
        else:
            comp = f"{c.score:.0f}"
            band = _c(c.band, 40)
            pos = str(c.our_position) if c.our_position else "нет в топе"
        lines.append(f"| {_c(kw)} | {freq} | {comp} | {band} | {pos} |")
    if freq_err:
        lines += ["", f"Частотность не получена по {len(freq_err)}: "
                  + "; ".join(f"{_c(k)} — {_first_line(v)}" for k, v in list(freq_err.items())[:5])]
    if comp_err:
        lines += ["", f"Выдача не получена по {len(comp_err)}: "
                  + "; ".join(f"{_c(k)} — {_first_line(v)}" for k, v in list(comp_err.items())[:5])]
    return "\n".join(lines)


def tool_get_serp_results(args: dict) -> str:
    query = args.get("query")
    if not query:
        raise ВходНеверен("Нужен query.")
    region = int(args.get("region", RUSSIA))
    n = _целое(args, "n", 10, 1, MAX_SERP_DOCS)

    r = serp(query, region=region, n=n)
    if r.error:
        raise ОтказИсточника(f"Выдача по запросу «{_c(query)}» не получена: {_first_line(r.error)}")

    lines = [
        f"Выдача Яндекса по «{_c(query)}», регион {region}. Всего найдено {r.found_all}. "
        f"Снято {_c(r.fetched_at, 40)}.",
        "",
        "| # | домен | глубина URL | слов в title | стр. домена | заголовок |",
        "|---:|---|---:|---:|---:|---|",
    ]
    for d in r.docs:
        lines.append(
            f"| {d.position} | {_c(d.domain, 80)} | {d.url_depth} | {d.title_hlwords} | "
            f"{d.domain_doccount} | {_c(d.title, 80)} |"
        )
    return "\n".join(lines)


def tool_find_serp_competitors(args: dict) -> str:
    queries = args.get("queries") or []
    if not queries:
        raise ВходНеверен("Нужен список queries.")
    region = int(args.get("region", RUSSIA))
    queries = queries[:10]

    _, serps = score_batch(queries, region=region, n=10, progress=False)
    stats = serp_competitors(serps)
    failed = [s.query for s in serps if s.error]
    if serps and len(failed) == len(serps):
        raise ОтказИсточника(
            f"Выдача не получена ни по одному из {len(serps)} запросов: "
            f"{_first_line(serps[0].error)}. Проверьте ключи — whoami.")

    lines = [
        f"Поисковые конкуренты по {len(queries) - len(failed)} запросам из {len(queries)}, регион {region}.",
        "Направленный срез, не измерение рынка. " + DISCLAIMER,
        "",
        "| домен | вхождений | лучшая | средняя | тип |",
        "|---|---:|---:|---:|---|",
    ]
    for s in stats[:25]:
        kind = "площадка" if s.is_portal else "сайт"
        lines.append(f"| {_c(s.domain, 80)} | {s.appearances} | {s.best_position} | {s.avg_position} | {kind} |")
    if failed:
        why = next(s.error for s in serps if s.error)
        lines += ["", f"Не удалось снять: {_c(', '.join(failed), 400)} — {_first_line(why)}"]
    return "\n".join(lines)


def tool_get_competition(args: dict) -> str:
    queries = args.get("queries") or ([args["query"]] if args.get("query") else [])
    if not queries:
        raise ВходНеверен("Нужен query или queries.")
    region = int(args.get("region", RUSSIA))
    comps, _ = score_batch(queries[:10], region=region, n=10, progress=False)
    if comps and all(c.error for c in comps):
        raise ОтказИсточника(
            f"Выдача не получена ни по одному из {len(comps)} запросов: "
            f"{_first_line(comps[0].error)}. Проверьте ключи — whoami.")

    lines = [
        f"Конкурентность, регион {region}. Веса: {json.dumps(WEIGHTS, ensure_ascii=False)}",
        DISCLAIMER,
    ]
    for c in comps:
        lines.append("")
        if c.error:
            lines.append(f"### {_c(c.query)} — нет данных: {_first_line(c.error)}")
            continue
        pos = f"наша позиция {c.our_position}" if c.our_position else "нас в топе нет"
        lines.append(f"### {_c(c.query)} — {c.score:.0f}/100, {_c(c.band, 40)}. {pos}.")
        lines.append("| фактор | значение | вес | вклад | доказательство |")
        lines.append("|---|---:|---:|---:|---|")
        for f in c.factors:
            lines.append(
                f"| {_c(f.name, 60)} | {f.value:.2f} | {f.weight:.2f} | {f.contribution:.1f} | {_c(f.evidence, 300)} |"
            )
    return "\n".join(lines)


def tool_build_brief(args: dict) -> str:
    query = args.get("query")
    if not query:
        raise ВходНеверен("Нужен query.")
    try:
        from .brief import build_brief, render_markdown
    except ImportError as e:
        return f"Модуль брифов недоступен: {e}"

    b = build_brief(
        query,
        region=int(args.get("region", RUSSIA)),
        n=_целое(args, "n", 10, 1, MAX_SERP_DOCS),
        our_domain=args.get("our_domain") or None,
    )
    if getattr(b, "error", None):
        raise ОтказИсточника(f"Разведка не выполнена: {_first_line(b.error)}")
    return render_markdown(_clean_brief(b))


def _clean_brief(b):
    """Копия брифа, в которой обезврежены все строки из выдачи и Wordstat:
    заголовки, сниппеты, адреса, домены, связанные запросы, паттерны."""
    import dataclasses

    def txt(v, n=300):
        return _c(v, n) if isinstance(v, str) else v

    def item(v):
        if isinstance(v, str):
            return txt(v)
        if isinstance(v, dict):
            return {k: txt(x) for k, x in v.items()}
        return v

    pages = [dataclasses.replace(p, url=txt(p.url), domain=txt(p.domain, 80),
                                 title=txt(p.title, 160), passage=txt(p.passage),
                                 modtime=txt(p.modtime, 40))
             for p in b.top_pages]
    return dataclasses.replace(
        b, query=txt(b.query), band=txt(b.band, 40), top_pages=pages,
        related_queries=[item(x) for x in b.related_queries],
        title_patterns=[item(x) for x in b.title_patterns],
        content_gaps=[item(x) for x in b.content_gaps],
        recommendations=[item(x) for x in b.recommendations])


def tool_expand_pool(args: dict) -> str:
    seed = args.get("seed")
    if not seed:
        raise ВходНеверен("Нужен seed.")
    try:
        from .pool import expand_pool
    except ImportError as e:
        return f"Модуль пула недоступен: {e}"

    res = expand_pool(
        args.get("project_id"),
        seed,
        min_freq=int(args.get("min_freq", 30)),
        limit=int(args.get("limit", 20)),
        region=int(args.get("region", RUSSIA)),
        apply=bool(args.get("apply", False)),
    )
    dead = [c for c in res["candidates"]
            if c["kind"] == "seed" and str(c["reason"]).startswith("Wordstat не ответил")]
    if dead:
        raise ОтказИсточника(f"Пул не расширен: {_first_line(dead[0]['reason'])}")
    acc = [c for c in res["candidates"] if c["accepted"]]
    rej = [c for c in res["candidates"] if not c["accepted"]]

    lines = [
        f"Расширение пула по seed «{_c(seed)}», регион {res['region']}.",
        f"Берём {len(acc)}, отброшено {len(rej)}. "
        + (f"Записано в пул: {res['added']}. Размер пула: {res['pool_size']}."
           if res["applied"] else "Показ без записи — повторить с apply=true."),
        "",
        "| фраза | частотность | тип | почему берём |",
        "|---|---:|---|---|",
    ]
    lines += [f"| {_c(c['phrase'])} | {c['freq']} | {_c(c['kind'], 40)} | {_c(c['reason'], 300)} |" for c in acc]
    if rej:
        lines += ["", "Отброшено:", "", "| фраза | частотность | почему |", "|---|---:|---|"]
        lines += [f"| {_c(c['phrase'])} | {c['freq']} | {_c(c['reason'], 300)} |" for c in rej[:20]]
    return "\n".join(lines)


def tool_competitor_keywords(args: dict) -> str:
    domain = args.get("domain")
    if not domain:
        raise ВходНеверен("Нужен domain.")
    our = config.default_domain(args.get("our_domain"))
    region = int(args.get("region", RUSSIA))

    rows = storage.competitor_gap(our, domain, region=region)
    if not rows:
        return (f"По домену {_c(domain, 80)} в накопленных снимках ничего нет. "
                "Сначала снимите выдачу по интересующим запросам.")

    missing = [r for r in rows if r["our_position"] is None]
    lines = [
        f"Запросы, по которым {_c(domain, 80)} есть в снятом топе, регион {region}. "
        f"Всего {len(rows)}, из них без нас {len(missing)}.",
        "Строится из накопленной истории выдачи: чем шире пул запросов, тем полнее карта.",
        "",
        "| запрос | их позиция | наша позиция | кто выше |",
        "|---|---:|---:|---|",
    ]
    for r in rows:
        theirs, ours = r["their_position"], r["our_position"]
        if ours is None:
            who = "нас нет"
        elif ours < theirs:
            who = "мы"
        elif ours > theirs:
            who = "они"
        else:
            who = "поровну"
        lines.append(f"| {_c(r['query'])} | {theirs or '—'} | {ours if ours else 'нет'} | {who} |")
    return "\n".join(lines)


def _pos(v) -> str:
    return "нет в топе" if v is None else str(v)


def tool_track_query(args: dict) -> str:
    queries = args.get("queries") or ([args["query"]] if args.get("query") else [])
    if not queries:
        raise ВходНеверен("Нужен query или queries.")
    region = int(args.get("region", RUSSIA))
    remove = bool(args.get("remove", False))

    storage.init_db()
    for q in queries:
        storage.untrack(q, region) if remove else storage.track(q, region)

    current = storage.tracked(region)
    verb = "снято с отслеживания" if remove else "поставлено на отслеживание"
    # Весь список отслеживаемых фраз при каждом вызове — это полотно в
    # контекст агента вместо ответа на вопрос «сработало ли». Печатается
    # сводка: первые 10 плюс остаток числом.
    ПОКАЗАТЬ = 10
    хвост = f"\n- …ещё {len(current) - ПОКАЗАТЬ}" if len(current) > ПОКАЗАТЬ else ""
    показ = "\n".join(f"- {_c(q)}" for q in current[:ПОКАЗАТЬ])
    return (f"{verb}: {len(queries)}. Всего отслеживается {len(current)}:\n"
            f"{показ}{хвост}")


def tool_run_tracking(args: dict) -> str:
    domain = config.default_domain(args.get("domain"))
    also = args.get("competitors") or []
    region = int(args.get("region", RUSSIA))
    queries = args.get("queries")
    now = bool(args.get("now", False))

    res = run_tracking([domain, *also], queries=queries, region=region, progress=False,
                       min_interval=min_interval_days(), now=now)
    if res.stopped:
        # Остановка до первого запроса — по потолку обращений или по сроку
        # между прогонами. Причина уже собрана по-человечески самим трекером
        # (сколько нужно, какой потолок/срок, как его снять) — печатать вместо
        # неё «Прогон -1: снято 0 из N» значило бы спрятать причину.
        return res.stopped
    if res.queries == 0:
        return "Отслеживаемых запросов нет. Сначала track_query."
    reasons = getattr(res, "reasons", {}) or {}
    why = _first_line(next(iter(reasons.values()), "причина не названа"))
    if res.ok == 0 and res.failed:
        raise ОтказИсточника(
            f"Прогон {res.run_id}: не снят ни один из {res.queries} запросов — {why}. "
            "Позиции не записаны; проверьте ключи — whoami.")

    lines = [
        f"Прогон {res.run_id}: снято {res.ok} из {res.queries} запросов, "
        f"замеров позиций {res.positions_recorded}.",
        f"Домены: {_c(', '.join([domain, *also]), 400)}. Регион {region}.",
    ]
    if res.failed:
        lines.append(f"Не удалось снять: {_c(', '.join(res.failed), 400)} — {why}")
    lines += ["", _tool_positions(domain, region)]
    return "\n".join(lines)


def tool_get_positions(args: dict) -> str:
    domain = config.default_domain(args.get("domain"))
    region = int(args.get("region", RUSSIA))
    return _tool_positions(domain, region)


def _tool_positions(domain: str, region: int) -> str:
    rows = report(domain, region)
    if not rows:
        return f"По домену {domain} замеров нет. Сначала run_tracking."

    lines = [
        f"Позиции {domain}, регион {region}. Дельта — к прошлому замеру, плюс значит рост.",
        "",
        "| запрос | сейчас | было | дельта | замеров |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in sorted(rows, key=lambda r: (r["position"] is None, r["position"] or 999)):
        was = "—" if r["measurements"] < 2 else _pos(r["previous"])
        delta = "—" if r["delta"] is None else (f"+{r['delta']}" if r["delta"] > 0 else str(r["delta"]))
        lines.append(f"| {_c(r['query'])} | {_pos(r['position'])} | {was} | {delta} | {r['measurements']} |")

    in_top = sum(1 for r in rows if r["position"] is not None)
    lines.append("")
    lines.append(f"В топе по {in_top} запросам из {len(rows)}.")

    up, down = movers(domain, region)
    if up:
        lines.append("Выросли: " + ", ".join(f"«{_c(r['query'])}» ({_pos(r['previous'])}→{_pos(r['position'])})" for r in up))
    if down:
        lines.append("Просели: " + ", ".join(f"«{_c(r['query'])}» ({_pos(r['previous'])}→{_pos(r['position'])})" for r in down))
    return "\n".join(lines)


def tool_get_position_history(args: dict) -> str:
    query = args.get("query")
    if not query:
        raise ВходНеверен("Нужен query.")
    domain = config.default_domain(args.get("domain"))
    region = int(args.get("region", RUSSIA))

    rows = storage.position_history(query, domain, region=region)
    if not rows:
        return f"История по «{_c(query)}» и домену {_c(domain, 80)} пуста."

    lines = [f"История позиций {_c(domain, 80)} по «{_c(query)}», регион {region}.", "",
             "| дата замера | позиция | URL |", "|---|---:|---|"]
    for r in rows:
        lines.append(f"| {_c(r['checked_at'], 19)} | {_pos(r['position'])} | {_c(r['url'], 300) or '—'} |")

    comp = storage.competition_history(query, region=region, limit=5)
    if comp:
        lines += ["", "Конкурентность запроса во времени:", "", "| дата | балл | полоса |", "|---|---:|---|"]
        for c in comp:
            lines.append(f"| {_c(c['calculated_at'], 19)} | {c['score']:.0f} | {_c(c['band'], 40)} |")
    return "\n".join(lines)


def tool_audit_site(args: dict) -> str:
    url = args.get("url")
    if not url:
        raise ВходНеверен("Нужен url.")
    if not isinstance(url, str):
        raise ВходНеверен("url — адрес сайта строкой, например https://example.ru")
    max_pages = _целое(args, "max_pages", 30, 1, MAX_AUDIT_PAGES)

    try:
        from .audit import audit_site
    except ImportError as e:
        return f"Модуль аудита недоступен: {e}"

    res = audit_site(url, max_pages=max_pages)
    if getattr(res, "error", None):
        raise ОтказИсточника(f"Аудит не выполнен: {_first_line(res.error)}")

    lines = [
        f"Аудит {_u(res.root)}. Обойдено страниц: {res.pages_crawled}. "
        f"robots.txt: {'есть' if res.robots_txt else 'нет'}. URL в sitemap: {res.sitemap_urls}.",
        f"Снято {_c(res.crawled_at, 19)}.",
        "",
    ]
    order = {"critical": 0, "major": 1, "minor": 2}
    issues = sorted(res.issues, key=lambda i: order.get(i.severity, 9))
    if not issues:
        lines.append("Проблем не найдено.")
        return "\n".join(lines)

    counts: dict[str, int] = {}
    for i in issues:
        counts[i.severity] = counts.get(i.severity, 0) + 1
    lines.append("Найдено: " + ", ".join(f"{k} — {v}" for k, v in counts.items()))
    lines += ["", "| тяжесть | код | страница | доказательство | что делать |", "|---|---|---|---|---|"]
    for i in issues[:60]:
        lines.append(f"| {_c(i.severity, 20)} | {_c(i.code, 60)} | {_u(i.url)} | "
                     f"{_c(i.evidence, 300)} | {_c(i.fix, 300)} |")
    if len(issues) > 60:
        lines.append(f"\nПоказано 60 из {len(issues)} проблем.")
    return "\n".join(lines)


def tool_storage_stats(_: dict) -> str:
    storage.init_db()
    s = storage.stats()
    lines = ["Накоплено в базе yaseo:", "", "| показатель | значение |", "|---|---|"]
    lines += [f"| {_c(k, 60)} | {_c(v, 60)} |" for k, v in s.items()]

    top = storage.top_domains(limit=10)
    if top:
        lines += ["", "Чаще всего в накопленной выдаче:", "",
                  "| домен | попаданий | запросов | лучшая | средняя |", "|---|---:|---:|---:|---:|"]
        for d in top:
            lines.append(f"| {_c(d['domain'], 80)} | {d['hits']} | {d['queries']} | {d['best']} | {d['avg_pos']} |")
    return "\n".join(lines)


def _article_rows(rows: list[dict]) -> list[str]:
    lines = ["| статья | целевой запрос | позиция | вердикт | дельта | доказательство |",
             "|---|---|---:|---|---:|---|"]
    for r in sorted(rows, key=lambda r: (r["position"] is None, r["position"] or 999)):
        delta = "—" if r["delta"] is None else (f"+{r['delta']}" if r["delta"] > 0 else str(r["delta"]))
        lines.append(
            f"| {_c(r['title'] or r['url'], 300)} | {_c(r['target_query'])} | {_pos(r['position'])} | "
            f"{_c(r['verdict'], 40)} | {delta} | {_c(r['reason'], 300)} |"
        )
    return lines


def tool_check_articles(args: dict) -> str:
    """Замер статей: тратит запросы к Search API."""
    project_id = args.get("project_id")
    region = int(args.get("region", RUSSIA))
    n = _целое(args, "depth", 30, 1, MAX_ARTICLE_DEPTH)

    run = articles.check_all(
        int(project_id) if project_id is not None else None,
        region=region, n=n, progress=False,
    )
    # `check_all` отдаёт ArticlesRun: остановка по потолку обращений — до первой
    # траты, причина уже написана по-человечески (как у run_tracking).
    if getattr(run, "stopped", None):
        return str(run.stopped)
    rows = list(getattr(run, "checked", run) or [])
    if not rows:
        return "Статей на отслеживании нет. Сначала заведите их: yaseo articles --add."
    if all(v.verdict == articles.NO_DATA for v in rows):
        raise ОтказИсточника(
            f"Ни одна из {len(rows)} пар «статья — запрос» не замерена: "
            f"{_first_line(rows[0].reason)}. Проверьте ключи — whoami.")

    lines = [f"Замер {len(rows)} пар «статья — запрос», глубина топ-{n}, регион {region}.", "",
             "| статья | запрос | позиция | наш домен | вердикт | доказательство |",
             "|---|---|---:|---:|---|---|"]
    for v in rows:
        lines.append(
            f"| {_c(v.url, 300)} | {_c(v.query)} | {_pos(v.position)} | {_pos(v.domain_position)} | "
            f"{_c(v.verdict, 40)} | {_c(v.reason, 300)} |"
        )
    lines += ["", articles.BASIS]
    return "\n".join(lines)


def tool_get_article_effectiveness(args: dict) -> str:
    """Отчёт по последним замерам. Новых запросов к API не делает."""
    project_id = args.get("project_id")
    pid = int(project_id) if project_id is not None else None

    rows = articles.report(pid)
    if not rows:
        return "Статей нет. Сначала заведите их и снимите замер через check_articles."

    s = articles.effectiveness_summary(pid)
    lines = ["Эффективность статей блога.", ""] + _article_rows(rows)
    lines += ["", f"Работает {s['works']}, подменилось {s['replaced']}, на подходе {s['coming']}, "
                  f"не вышло {s['missing']} из всего {s['articles']}."]
    if s["cannibalized"]:
        lines += ["", "Каннибализация — вместо целевой статьи выходит другая наша страница:"]
        for c in s["cannibalized"]:
            lines.append(
                f"- «{_c(c['query'])}»: ждали {_c(c['url'], 300)}, вышла {_c(c['ranking_url'], 300)} "
                f"на {c['domain_position']}"
            )
    lines += ["", s["basis"]]
    return "\n".join(lines)


def tool_get_action_plan(args: dict) -> str:
    """План правок сайта из накопленного и бесплатно проверяемого. Не тратит."""
    from . import plan as _plan

    sections = args.get("sections")
    if isinstance(sections, str):
        sections = [s for s in sections.replace(",", " ").split() if s]
    if sections is not None and not isinstance(sections, list):
        raise ВходНеверен("sections — список: " + ", ".join(_plan.SECTIONS))
    fresh = args.get("fresh_audit")
    if fresh is not None and not isinstance(fresh, bool):
        raise ВходНеверен("fresh_audit — true или false.")
    url = args.get("url")
    if url is not None and (not isinstance(url, str) or not url.strip()):
        raise ВходНеверен("url — адрес сайта, например https://example.ru")

    # Вход проверяется целиком до обхода сайта: отказ по limit после минутного
    # аудита — потраченное время человека.
    limit = _целое(args, "limit", 10, 1, MAX_PLAN_LIMIT)
    max_pages = _целое(args, "max_pages", 30, 1, MAX_AUDIT_PAGES)
    try:
        p = _plan.build_plan(
            domain=args.get("domain"),
            url=url,
            max_pages=max_pages,
            sections=sections or None,
            fresh_audit=fresh,
        )
    except config.DomainNotSetError:
        raise
    except ValueError as e:
        raise ВходНеверен(str(e)) from e
    return _plan.render(p, limit=limit)


CORE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "whoami",
        "description": "Проверить конфигурацию yaseo: откуда взяты ключи, каких не хватает и где их взять, путь к базе, домен по умолчанию. Ничего не тратит. Вызывать первым, прежде чем тратить запросы.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_whoami,
    },
    {
        "name": "research_keywords",
        "description": "Расширить 1–5 seed-фраз через Wordstat: частотность и связанные запросы по РФ. Основной инструмент подбора семантики.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seeds": {"type": "array", "items": {"type": "string"}, "description": "1–5 seed-фраз"},
                "region": {"type": "string", "description": "код региона Яндекса, по умолчанию 225 (Россия)"},
                "min_freq": {"type": "integer", "description": "отсечка по частотности, по умолчанию 30"},
                "limit": {"type": "integer", "description": "сколько расширений на seed, по умолчанию 40"},
            },
            "required": ["seeds"],
        },
        "handler": tool_research_keywords,
    },
    {
        "name": "get_keyword_metrics",
        "description": "Частотность и конкурентность для списка запросов (до 25). Конкурентность считается по составу выдачи Яндекса, ссылочный вес не учитывается.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "array", "items": {"type": "string"}},
                "region": {"type": "integer", "description": "по умолчанию 225"},
                "with_competition": {"type": "boolean", "description": "считать конкурентность, по умолчанию true"},
            },
            "required": ["keywords"],
        },
        "handler": tool_get_keyword_metrics,
    },
    {
        "name": "get_serp_results",
        "description": "Живая выдача Яндекса по одному запросу: позиции, домены, заголовки, глубина URL, число страниц домена по теме.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "region": {"type": "integer"},
                "n": {"type": "integer", "minimum": 1, "maximum": MAX_SERP_DOCS,
                      "description": f"сколько документов, по умолчанию 10, не больше {MAX_SERP_DOCS}"},
            },
            "required": ["query"],
        },
        "handler": tool_get_serp_results,
    },
    {
        "name": "find_serp_competitors",
        "description": "Кто повторяется в выдаче по набору запросов (до 10) — поисковые конкуренты с разделением на сайты и крупные площадки.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "queries": {"type": "array", "items": {"type": "string"}},
                "region": {"type": "integer"},
            },
            "required": ["queries"],
        },
        "handler": tool_find_serp_competitors,
    },
    {
        "name": "get_competition",
        "description": "Конкурентность запросов с полным разбором по факторам и доказательствами. Использовать, когда нужно понять, ПОЧЕМУ запрос сложный.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "queries": {"type": "array", "items": {"type": "string"}},
                "region": {"type": "integer"},
            },
        },
        "handler": tool_get_competition,
    },
    {
        "name": "build_brief",
        "description": "Разведка перед написанием статьи: частотность, конкурентность, состав топ-10, повторяющиеся форматы заголовков, разрывы и связанные запросы. Вызывать до того, как писать бриф копирайтеру.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "целевой запрос статьи"},
                "region": {"type": "integer"},
                "n": {"type": "integer", "minimum": 1, "maximum": MAX_SERP_DOCS,
                      "description": f"глубина выдачи, по умолчанию 10, не больше {MAX_SERP_DOCS}; "
                                     "каждые 10 документов — отдельное платное обращение"},
                "our_domain": {"type": "string"},
            },
            "required": ["query"],
        },
        "handler": tool_build_brief,
    },
    {
        "name": "track_query",
        "description": "Поставить запросы на регулярное отслеживание позиций или снять их. Отслеживаемые запросы использует run_tracking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "queries": {"type": "array", "items": {"type": "string"}},
                "region": {"type": "integer"},
                "remove": {"type": "boolean", "description": "снять с отслеживания вместо добавления"},
            },
        },
        "handler": tool_track_query,
    },
    {
        "name": "run_tracking",
        "description": "Снять текущие позиции по отслеживаемым запросам и записать замер в историю. Здесь тратятся запросы к Search API. Прогон останавливается ДО первого запроса и ничего не тратит, если обращений вышло бы больше потолка (YASEO_TRACKING_MAX_CALLS) или домен уже мерили недавно (YASEO_TRACKING_MIN_INTERVAL_DAYS) — в обоих случаях ответ говорит, сколько нужно и как снять ограничение на этот раз (параметр now).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "основной домен; по умолчанию YASEO_DOMAIN или домен единственного проекта"},
                "competitors": {"type": "array", "items": {"type": "string"}, "description": "домены конкурентов для замера"},
                "queries": {"type": "array", "items": {"type": "string"}, "description": "разовый набор вместо отслеживаемых"},
                "region": {"type": "integer"},
                "now": {"type": "boolean", "description": "true — снять срок между прогонами (YASEO_TRACKING_MIN_INTERVAL_DAYS) на этот один раз, по умолчанию false"},
            },
        },
        "handler": tool_run_tracking,
    },
    {
        "name": "get_positions",
        "description": "Текущие позиции домена с дельтой к прошлому замеру и списком выросших и просевших запросов. Данные из истории, новых запросов к API не делает.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "region": {"type": "integer"},
            },
        },
        "handler": tool_get_positions,
    },
    {
        "name": "get_position_history",
        "description": "История позиций домена по одному запросу и динамика конкурентности этого запроса.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "domain": {"type": "string"},
                "region": {"type": "integer"},
            },
            "required": ["query"],
        },
        "handler": tool_get_position_history,
    },
    {
        "name": "audit_site",
        "description": "Технический аудит сайта: индексация, метаданные, заголовки, канонические адреса, разметка, перелинковка. Каждая находка с доказательством.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "адрес сайта, например https://example.ru"},
                "max_pages": {"type": "integer", "minimum": 1, "maximum": MAX_AUDIT_PAGES,
                              "description": f"предел обхода, по умолчанию 30, не больше {MAX_AUDIT_PAGES}"},
            },
            "required": ["url"],
        },
        "handler": tool_audit_site,
    },
    {
        "name": "check_articles",
        "description": "Снять позиции статей блога по их целевым запросам: вышла ли ИМЕННО эта статья, или вместо неё другая наша страница (каннибализация), или ничего. Здесь тратятся запросы к Search API.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "id проекта, без него — все статьи"},
                "region": {"type": "integer"},
                "depth": {"type": "integer", "minimum": 1, "maximum": MAX_ARTICLE_DEPTH,
                          "description": f"глубина топа, по умолчанию 30 — иначе не видно «на подходе»; не больше {MAX_ARTICLE_DEPTH}"},
            },
        },
        "handler": tool_check_articles,
    },
    {
        "name": "get_article_effectiveness",
        "description": "Отчёт по статьям блога: вердикт и дельта по последнему замеру, отдельно список каннибализаций. Данные из истории, новых запросов к API не делает.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
        },
        "handler": tool_get_article_effectiveness,
    },
    {
        "name": "get_storage_stats",
        "description": "Что накоплено в базе yaseo: снимки, замеры позиций, история конкурентности, самые частые домены выдачи.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_storage_stats,
    },
    {
        "name": "expand_query_pool",
        "description": "Предложить новые запросы в пул отслеживания по seed-фразе через Wordstat. По умолчанию показывает кандидатов с причиной по каждому; записывает при apply=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seed": {"type": "string"},
                "project_id": {"type": "integer"},
                "min_freq": {"type": "integer", "description": "порог частотности, по умолчанию 30"},
                "limit": {"type": "integer", "description": "сколько взять, по умолчанию 20"},
                "region": {"type": "integer"},
                "apply": {"type": "boolean", "description": "записать в пул, по умолчанию false"},
            },
            "required": ["seed"],
        },
        "handler": tool_expand_pool,
    },
    {
        "name": "get_competitor_keywords",
        "description": "По каким запросам конкурент выходит в топ и где нас рядом нет. Строится из накопленных снимков выдачи, новых запросов к API не делает.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "домен конкурента"},
                "our_domain": {"type": "string", "description": "наш домен для сравнения"},
                "region": {"type": "integer"},
            },
            "required": ["domain"],
        },
        "handler": tool_competitor_keywords,
    },
    {
        "name": "get_action_plan",
        "description": "План правок сайта по приоритету: индексация, быстрые выигрыши (по Вебмастеру позиция 4–15, по трекеру 4–10: трекер снимает топ-10), сниппет (показы без кликов), каннибализация, ИИ-поиск, разрывы с конкурентами, остальное. Каждый пункт — с доказательством и датой замера, готовой инструкцией для Claude или разработчика, способом проверки и сроком, раньше которого эффект не проверять. Строится из аудита и проверки готовности (HTTP к самому сайту) и из того, что уже лежит в базе: позиции, выгрузки Вебмастера, статьи, снимки выдачи. Платных запросов не делает; чего нет в базе, того нет в плане — вместо этого называет инструмент и смету.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "домен сайта; по умолчанию YASEO_DOMAIN или домен единственного проекта"},
                "url": {"type": "string", "description": "адрес для аудита, по умолчанию https://<domain>"},
                "max_pages": {"type": "integer", "minimum": 1, "maximum": MAX_AUDIT_PAGES,
                              "description": f"предел обхода аудита, по умолчанию 30, не больше {MAX_AUDIT_PAGES}"},
                "sections": {"type": "array", "items": {"type": "string", "enum": ["indexing", "quick_wins", "snippet", "cannibalization", "ai_search", "competitor_gaps", "other"]},
                             "description": "какие разделы собрать, по умолчанию все"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PLAN_LIMIT,
                          "description": f"сколько пунктов показать, по умолчанию 10, не больше {MAX_PLAN_LIMIT}; остальное — счётчиком по разделам"},
                "fresh_audit": {"type": "boolean", "description": "true — новый обход; false — последний аудит из базы. По умолчанию новый, если в базе аудита нет или ему больше 7 дней"},
            },
        },
        "handler": tool_get_action_plan,
    },
]



def _extra_tools() -> list[dict[str, Any]]:
    """Инструменты дополнительных модулей. Модуля нет — набор пуст."""
    try:
        from .geo import tools as geo_tools  # type: ignore[attr-defined]
    except ImportError:
        return []
    extra = getattr(geo_tools, "TOOLS", geo_tools)
    if not isinstance(extra, (list, tuple)):
        return []
    # Модули сообщают об ошибке входа обычным ValueError — импортировать
    # ВходНеверен им нельзя (кольцо). Переводим здесь, чтобы отказ ушёл
    # клиенту текстом, а не трассировкой.
    return [{**t, "handler": _as_input_error(t["handler"])} for t in extra]


def _as_input_error(fn: Callable[[dict], str]) -> Callable[[dict], str]:
    def wrapped(args: dict) -> str:
        try:
            return fn(args)
        except ВходНеверен:
            raise
        except ValueError as e:
            raise ВходНеверен(str(e)) from e
    return wrapped


#: Инструменты, в выводе которых нет внешнего текста: только конфигурация машины.
NO_EXTERNAL_TEXT = frozenset({"whoami"})


def _with_notice(fn: Callable[[dict], str]) -> Callable[[dict], str]:
    """Первая строка вывода — пометка, что ниже данные из внешних источников.
    Отказ источника (UpstreamError) несёт текст провайдера — помечается тоже."""
    def wrapped(args: dict) -> str:
        try:
            return with_notice(fn(args))
        except UpstreamError as e:
            raise UpstreamError(with_notice(str(e))) from e
    wrapped.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapped


def _marked(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t if t["name"] in NO_EXTERNAL_TEXT else {**t, "handler": _with_notice(t["handler"])}
            for t in tools]


TOOLS: list[dict[str, Any]] = _marked(CORE_TOOLS + _extra_tools())

HANDLERS: dict[str, Callable[[dict], str]] = {t["name"]: t["handler"] for t in TOOLS}
TOOL_SPECS = [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


# ──────────────────────────── транспорт ────────────────────────────

def _result(req_id: Any, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def _error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _crash_text(e: BaseException, frames: int = 3) -> str:
    """
    Текст сбоя без данных сайта.

    Сообщение исключения и строки трассировки могут нести то, что пришло
    снаружи (адрес со страницы, заголовок ответа). Поэтому наружу идут только
    тип исключения и последние кадры — файл пакета, строка, функция, без
    значений аргументов и текста исходника; каждая строка — через `clean`,
    и весь текст — под пометкой о внешних данных.
    """
    tb = traceback.extract_tb(e.__traceback__)[-frames:]
    lines = [f"Сбой инструмента: {clean(type(e).__name__, 80)}.",
             "Последние кадры (файл:строка, функция):"]
    for f in tb:
        name = f.filename.replace("\\", "/").rsplit("/", 1)[-1]
        lines.append("- " + clean(f"{name}:{f.lineno}, {f.name}", 200))
    return with_notice("\n".join(lines))


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    req_id = msg.get("id")

    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": TOOL_SPECS})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return _error(req_id, -32601, f"неизвестный инструмент: {name}")
        try:
            text = fn(args)
            return _result(req_id, {"content": [{"type": "text", "text": text}]})
        except (ВходНеверен, config.DomainNotSetError, UpstreamError) as e:
            # Отказ по входу или отказ источника целиком — не сбой сервера, но и
            # не тихий успех: агент обязан увидеть isError. Текст человеческий,
            # без трейсбека.
            return _result(req_id, {
                "content": [{"type": "text", "text": str(e)}],
                "isError": True,
            })
        except MissingKeysError as e:
            # Наследник SystemExit: `except Exception` его не ловит, и без этой
            # ветки сервер завершился бы на первом вызове без ключей.
            return _result(req_id, {
                "content": [{"type": "text", "text": str(e.code)}],
                "isError": True,
            })
        except SystemExit as e:
            # Модули говорят с человеком через SystemExit("текст"); серверу
            # умирать из-за этого нельзя.
            return _result(req_id, {
                "content": [{"type": "text", "text": str(e.code)}],
                "isError": True,
            })
        except Exception as e:  # noqa: BLE001 — ошибка уезжает клиенту, сервер живёт
            return _result(req_id, {
                "content": [{"type": "text", "text": _crash_text(e)}],
                "isError": True,
            })

    if req_id is None:
        return None
    return _error(req_id, -32601, f"метод не поддерживается: {method}")


def main(argv: list[str] | None = None) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
