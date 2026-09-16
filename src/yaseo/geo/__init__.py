"""
yaseo.geo — видимость сайта в ИИ-поиске (GEO/AEO).

Инструменты MCP: ``tools()`` возвращает список словарей
``{"name", "description", "inputSchema", "handler"}``; ``handler(args) -> str``
(Markdown). Отказ по входу — ``ValueError`` с человеческим текстом.
"""
from __future__ import annotations

import dataclasses
import json
import time
from collections import Counter, defaultdict

from .. import net
from ..errors import UpstreamError
from ..untrusted import clean
from . import _core
from . import providers as P
from . import readiness as R
from . import storage as S
from .domains import MATCH_MODES, host_of

MAX_QUERIES = 10
MAX_REPEATS = 5

HONESTY = ("Ответ ИИ меняется от запроса к запросу: один прогон — снимок, а не приговор, "
           "как одиночный снимок выдачи. Для устойчивой картины повторите проверку "
           "(параметр repeats) и смотрите долю цитирований в geo_history.")


# ───────────────────────────── вход ─────────────────────────────

def _domain(args: dict, required: bool = True) -> str:
    raw = args.get("domain")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise ValueError("Нужен domain — домен сайта, например example.ru.")
        return ""
    if not isinstance(raw, str):
        raise ValueError("domain должен быть строкой, например example.ru.")
    d = host_of(raw)
    if not d or "." not in d:
        raise ValueError(f"Не похоже на домен: «{raw}». Нужен вид example.ru.")
    return d


def _queries(args: dict) -> list[str]:
    q = args.get("queries")
    if isinstance(q, str):
        q = [q]
    if not isinstance(q, list) or not q:
        raise ValueError("Нужен список queries — от 1 до 10 запросов, как их задал бы человек.")
    out = []
    for x in q:
        if not isinstance(x, str) or not x.strip():
            raise ValueError("Каждый элемент queries — непустая строка.")
        x = " ".join(x.split())
        if x not in out:
            out.append(x)
    if len(out) > MAX_QUERIES:
        raise ValueError(f"Запросов {len(out)}, допустимо не больше {MAX_QUERIES} за вызов.")
    return out


def _int(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = args.get(key, default)
    if v is None:
        return default
    if isinstance(v, bool):
        raise ValueError(f"{key} — целое число от {lo} до {hi}.")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{key} — целое число от {lo} до {hi}, получено «{v}».") from None
    if not lo <= n <= hi:
        raise ValueError(f"{key} — от {lo} до {hi}, получено {n}.")
    return n


def _bool(args: dict, key: str) -> bool:
    v = args.get(key, False)
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "1", "yes", "да"):
        return True
    if v in (None, "", 0) or (isinstance(v, str) and v.strip().lower() in ("false", "0", "no", "нет")):
        return False
    raise ValueError(f"{key} — true или false.")


def _providers(args: dict) -> tuple[list[str], dict[str, list[str]]]:
    """(провайдеры к запуску, {провайдер: каких ключей нет})."""
    raw = args.get("providers")
    if raw in (None, "", []):
        names = list(P.REGISTRY)
    else:
        if isinstance(raw, str):
            raw = [x for x in raw.replace(",", " ").split() if x]
        if not isinstance(raw, list):
            raise ValueError("providers — список имён: " + ", ".join(P.REGISTRY))
        names = []
        for n in raw:
            name = str(n).strip().lower()
            if name not in P.REGISTRY:
                raise ValueError(f"Неизвестный провайдер «{n}». Есть: {', '.join(P.REGISTRY)}.")
            if name not in names:
                names.append(name)
    run, missing = [], {}
    for n in names:
        ok, miss = P.REGISTRY[n].configured()
        if ok:
            run.append(n)
        else:
            missing[n] = miss
    return run, missing


def _c(text, n: int = 160) -> str:
    """Внешняя строка (ответ провайдера, адрес, домен, строка сайта) — в одну
    строку без символов разметки (`untrusted.clean`)."""
    return clean(text, n)


def _block(text, lines: int = 12, n: int = 300) -> str:
    """Многострочный внешний текст для блока кода: каждая строка обезврежена,
    обратная кавычка блок не закроет."""
    rows = [_c(ln, n) for ln in str(text or "").splitlines()]
    return "\n".join([r for r in rows if r][:lines])


def _rub(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


# ─────────────────────────── инструменты ───────────────────────────

def tool_geo_providers(_: dict) -> str:
    lines = ["Провайдеры ИИ-ответов с источниками. Этот вызов ничего не тратит.", "",
             "| провайдер | что это | настроен | источник ключа | каких ключей нет | цена вызова |",
             "|---|---|---|---|---|---|"]
    for p in P.REGISTRY.values():
        ok, miss = p.configured()
        src = _core.key_sources(p.required_env)
        src_txt = ", ".join(f"{k}: {v}" for k, v in src.items() if v) or "—"
        lines.append(f"| {p.name} | {p.title} | {'да' if ok else 'нет'} | {src_txt} | "
                     f"{', '.join(miss) or '—'} | {p.price_note} |")
    lines += ["", "Где взять ключи:"]
    for p in P.REGISTRY.values():
        lines.append(f"- **{p.name}** ({', '.join(p.required_env)}): {p.where_to_get}")
    lines += ["", "Оговорки:"]
    for p in P.REGISTRY.values():
        if p.honest_note:
            lines.append(f"- **{p.name}**: {p.honest_note}")

    try:
        err = S.last_error("yandex")
    except Exception:  # noqa: BLE001 — база недоступна: подсказку просто не показываем
        err = None
    if err and ("HTTP 403" in err["error"] or "HTTP 401" in err["error"]):
        y = P.REGISTRY["yandex"]
        lines += ["", f"Последняя проверка Яндекса ({_c(err['checked_at'], 40)}) получила отказ доступа:",
                  "", "```", _block(err["error"].split("\nПодсказка:")[0][:600]), "```", "",
                  y.hint_403()]
    lines += ["", "GigaChat не подключён: поиска с источниками в его API нет."]
    return "\n".join(lines)


def tool_geo_check_visibility(args: dict) -> str:
    queries = _queries(args)
    domain = _domain(args)
    repeats = _int(args, "repeats", 1, 1, MAX_REPEATS)
    confirm = _bool(args, "confirm")
    mode = str(args.get("match") or "site").strip().lower()
    if mode not in MATCH_MODES:
        raise ValueError("match — site (весь сайт с поддоменами) или host (ровно этот хост).")
    if confirm and args.get("providers") in (None, "", []):
        # Ключи разных провайдеров часто лежат в окружении по другим поводам.
        # Трата без явного списка ушла бы ко всем сразу — человек этого не выбирал.
        raise ValueError("С confirm=true укажите providers явно, например [\"yandex\"]. "
                         "Без confirm вызов покажет смету по всем настроенным.")
    run, missing = _providers(args)

    lines: list[str] = []
    if missing:
        lines += ["Без ключей, пропущены:"]
        for n, miss in missing.items():
            lines.append(f"- {n}: нет {', '.join(miss)} — где взять, покажет geo_providers")
        lines.append("")
    if not run:
        lines.append("Ни один из выбранных провайдеров не настроен — проверять нечем. "
                     "Добавьте ключи и повторите.")
        return "\n".join(lines)

    per = len(queries) * repeats
    if not confirm:
        lines += [f"Смета проверки домена {domain}: {len(queries)} запрос(ов) × {repeats} "
                  f"повтор(ов) × {len(run)} провайдер(ов). Ничего не потрачено.", "",
                  "| провайдер | вызовов | цена |", "|---|---:|---|"]
        total = 0.0
        for n in run:
            p = P.REGISTRY[n]
            if p.rub_per_request is not None:
                cost = p.rub_per_request * per
                total += cost
                lines.append(f"| {n} | {per} | {_rub(cost)} ({p.price_note}) |")
            else:
                lines.append(f"| {n} | {per} | {P.FOREIGN_PRICE_NOTE} |")
        lines += ["", f"Известная часть суммы: {_rub(total)}. Цены зарубежных провайдеров "
                  "зависят от модели и тарифа аккаунта и здесь не выдумываются."]
        if "yandex" in run:
            lines.append(f"Яндекс принимает не больше 1 запроса в секунду — "
                         f"{per} вызов(ов) займут не меньше {per} с.")
        lines += ["", "Чтобы выполнить, повторите вызов с confirm=true.", "", HONESTY]
        return "\n".join(lines)

    answers: list[P.Answer] = []
    for q in queries:
        for n in run:
            for _ in range(repeats):
                a = P.ask(n, q, domain, mode=mode)
                try:
                    S.save(a)
                except Exception as e:  # noqa: BLE001 — сбой базы не отменяет ответ
                    a.error = (a.error + "; " if a.error else "") + f"не записано в базу: {e}"
                answers.append(a)

    lines += [f"Видимость {domain} в ИИ-ответах. Снято {time.strftime('%Y-%m-%d %H:%M')}.", "",
              "| запрос | провайдер | процитирован | каким адресом | место среди источников | "
              "источников (использовано) | кого цитируют вместо |",
              "|---|---|---|---|---:|---:|---|"]
    for a in answers:
        if a.error:
            lines.append(f"| {_c(a.query)} | {_c(a.provider, 40)} | ошибка | — | — | — | "
                         f"{_c((a.error.splitlines() or [''])[0], 200)} |")
            continue
        verdict = "да" if a.cited else ("в источниках, не использован" if a.in_sources else "нет")
        if a.mentioned_in_text and not a.cited:
            verdict += "; домен назван в тексте"
        if a.rejected:
            verdict += "; модель отказалась отвечать"
        rivals = ", ".join(f"{_c(d, 80)} ({c})" for d, c in a.rivals(3)) or "—"
        lines.append(f"| {_c(a.query)} | {_c(a.provider, 40)} | {verdict} | {_c(a.cited_url, 300) or '—'} | "
                     f"{a.cited_position or '—'} | {len(a.sources)} ({a.used_count}) | {rivals} |")

    ok = [a for a in answers if not a.error]
    if answers and not ok:
        # Ни один провайдер не ответил: это отказ целиком, а не таблица «ошибка».
        raise UpstreamError("\n".join(
            [f"Ни один из {len(answers)} вызовов не вернул ответа. Отказы дословно:"]
            + [f"- {_c(a.provider, 40)}, «{_c(a.query)}»: {_c((a.error.splitlines() or [''])[0], 300)}"
               for a in answers[:5]]))
    if ok:
        cited = sum(a.cited for a in ok)
        lines += ["", f"Процитирован в {cited} из {len(ok)} ответов."]
    errs = [a for a in answers if a.error]
    if errs:
        lines += ["", "Отказы провайдеров дословно:"]
        for a in errs[:5]:
            lines += ["", f"**{_c(a.provider, 40)}**, «{_c(a.query)}»:", "```", _block(a.error[:1500]), "```"]
    lines += ["", "Место — порядковый номер нашего адреса в списке источников провайдера "
              "(Яндекс отдаёт и неиспользованные документы, их used=false). "
              "«Использовано» у Perplexity считается по маркерам [n] в тексте; "
              "если маркеров нет, использованными считаются все источники.", "", HONESTY]
    for n in run:
        if P.REGISTRY[n].honest_note:
            lines.append(f"- {n}: {P.REGISTRY[n].honest_note}")
    return "\n".join(lines)


def tool_geo_history(args: dict) -> str:
    domain = _domain(args, required=False)
    query = args.get("query")
    if query is not None and not isinstance(query, str):
        raise ValueError("query — строка.")
    query = " ".join(query.split()) if query else None
    provider = args.get("provider")
    if provider:
        provider = P.get(str(provider)).name
    limit = _int(args, "limit", 50, 1, 500)
    if not domain and not query:
        raise ValueError("Нужен domain или query — чью историю показать.")
    rows = S.history(domain or None, query, provider, limit)
    if not rows:
        return "Проверок по этому условию ещё не было. Запустите geo_check_visibility."

    agg: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        agg[(r["query"], r["provider"])].append(r)
    lines = [f"История видимости{' ' + _c(domain, 80) if domain else ''} в ИИ-ответах. "
             f"Строк: {len(rows)}. Новых запросов не делалось.", "",
             "| запрос | провайдер | прогонов | процитирован | доля | последний раз | "
             "частые конкуренты |", "|---|---|---:|---:|---:|---|---|"]
    for (q, prov), rs in agg.items():
        good = [r for r in rs if not r["error"]]
        c = sum(r["cited"] for r in good)
        share = f"{round(100 * c / len(good))}%" if good else "—"
        rivals: Counter = Counter()
        for r in good:
            try:
                rivals.update(json.loads(r["rivals"] or "[]"))
            except ValueError:
                pass
        top = ", ".join(_c(d, 80) for d, _ in rivals.most_common(3)) or "—"
        lines.append(f"| {_c(q)} | {_c(prov, 40)} | {len(rs)}{f' (ошибок {len(rs) - len(good)})' if len(good) != len(rs) else ''} | "
                     f"{c} | {share} | {_c(rs[0]['checked_at'], 16)} | {top} |")
    lines += ["", "Последние прогоны:", "",
              "| когда | запрос | провайдер | итог | адрес | место |", "|---|---|---|---|---|---:|"]
    for r in rows[:20]:
        verdict = ("ошибка" if r["error"] else "процитирован" if r["cited"]
                   else "в источниках" if r["in_sources"] else "нет")
        lines.append(f"| {_c(r['checked_at'], 16)} | {_c(r['query'])} | {_c(r['provider'], 40)} | "
                     f"{verdict} | {_c(r['cited_url'], 300) or '—'} | {_c(r['position'], 10) or '—'} |")
    lines += ["", HONESTY]
    return "\n".join(lines)


def tool_geo_readiness(args: dict) -> str:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Нужен url — адрес сайта, например https://example.ru")
    pages = _int(args, "pages", 2, 0, 5)
    return R.render(_clean_readiness(R.check(url, extra_pages=pages)))


def _clean_readiness(res):
    """Копия результата проверки, в которой обезврежены строки сайта:
    доказательства (строки robots.txt, llms.txt, JSON-LD), адреса, типы
    разметки. Рендер остаётся за `readiness.render`."""
    def t(v, n=300):
        return _c(v, n) if isinstance(v, str) else v

    def u(v):
        # Адрес — percent-кодированный: точный и инертный в Markdown.
        return _c(net.safe_url(v, display=True), 300) if isinstance(v, str) and v else v

    issues = [dataclasses.replace(i, severity=t(i.severity, 20), code=t(i.code, 60),
                                  url=u(i.url), evidence=t(i.evidence), fix=t(i.fix, 400))
              for i in res.issues]
    bots = [dataclasses.replace(b, token=t(b.token, 60), owner=t(b.owner, 60),
                                evidence=t(b.evidence), note=t(b.note, 400))
            for b in res.bots]
    pages = []
    for pg in res.pages:
        pg = dict(pg)
        pg["url"] = u(pg.get("url"))
        pg["status"] = t(pg.get("status"), 20)
        pg["markdown_alternate"] = u(pg.get("markdown_alternate"))
        pg["jsonld_types"] = [t(x, 60) for x in pg.get("jsonld_types") or []]
        pages.append(pg)
    return dataclasses.replace(
        res, root=u(res.root), checked_at=t(res.checked_at, 40), llms_txt=t(res.llms_txt),
        llms_full_txt=t(res.llms_full_txt), robots_txt=t(res.robots_txt), bots=bots,
        pages=pages, issues=issues, good=[t(g) for g in res.good])


# ─────────────────────────── реестр MCP ───────────────────────────

_TOOLS = [
    {
        "name": "geo_providers",
        "description": "Какие провайдеры ИИ-ответов настроены (Яндекс, Perplexity, OpenAI, Gemini, Claude), каких ключей не хватает и где их взять, сколько стоит вызов. Ничего не тратит. Вызывать первым перед проверкой видимости.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_geo_providers,
    },
    {
        "name": "geo_check_visibility",
        "description": "Цитирует ли ИИ-поиск сайт: задаёт запросы провайдерам и смотрит, есть ли домен среди источников ответа, каким адресом и на каком месте, кого цитируют вместо. Без confirm=true возвращает только смету (Яндекс 5,08 ₽ за запрос, зарубежные — по тарифу аккаунта). С confirm=true тратит деньги и пишет результат в историю.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "queries": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                            "maxItems": MAX_QUERIES,
                            "description": "1–10 запросов так, как их задал бы человек"},
                "domain": {"type": "string", "description": "домен сайта, например example.ru"},
                "providers": {"type": "array", "items": {"type": "string", "enum": list(P.REGISTRY)},
                              "description": "кого спросить; по умолчанию все настроенные"},
                "repeats": {"type": "integer", "minimum": 1, "maximum": MAX_REPEATS,
                            "description": "сколько раз повторить каждый запрос, по умолчанию 1"},
                "match": {"type": "string", "enum": list(MATCH_MODES),
                          "description": "site — весь сайт с поддоменами (по умолчанию), host — ровно этот хост"},
                "confirm": {"type": "boolean",
                            "description": "true — выполнить и потратить; без него только смета"},
            },
            "required": ["queries", "domain"],
        },
        "handler": tool_geo_check_visibility,
    },
    {
        "name": "geo_history",
        "description": "История проверок видимости в ИИ-ответах по домену или запросу: сколько раз процитирован, доля, частые конкуренты. Ничего не тратит.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "домен сайта"},
                "query": {"type": "string", "description": "точный текст запроса"},
                "provider": {"type": "string", "enum": list(P.REGISTRY)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500,
                          "description": "сколько последних строк взять, по умолчанию 50"},
            },
        },
        "handler": tool_geo_history,
    },
    {
        "name": "geo_readiness",
        "description": "Готовность сайта к ИИ-поиску без ключей: llms.txt по спецификации llmstxt.org, llms-full.txt, доступ ИИ-ботов в robots.txt (GPTBot, OAI-SearchBot, PerplexityBot, ClaudeBot, Google-Extended, YandexAdditional и др.), JSON-LD, FAQ-разметка, Markdown-версии страниц. Каждая находка — с доказательством и тем, что сделать. Ничего не тратит.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "адрес сайта, например https://example.ru"},
                "pages": {"type": "integer", "minimum": 0, "maximum": 5,
                          "description": "сколько внутренних страниц проверить кроме главной, по умолчанию 2"},
            },
            "required": ["url"],
        },
        "handler": tool_geo_readiness,
    },
]


class _Tools(list):
    """Список инструментов, который можно и вызвать: ``tools()``.
    Ядро берёт его как ``geo.tools`` (список) — работают оба способа."""

    def __call__(self) -> list[dict]:
        return list(self)


tools = _Tools(_TOOLS)
TOOLS = tools

__all__ = ["tools", "TOOLS"]
