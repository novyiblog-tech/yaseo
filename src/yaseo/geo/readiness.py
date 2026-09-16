"""
Готовность сайта к ИИ-поиску — без ключей и без трат.

Что проверяется:
- ``/llms.txt`` — по спецификации https://llmstxt.org/: H1 (единственный
  обязательный раздел), блок-цитата с кратким описанием, разделы H2 со
  списками ссылок ``[название](адрес)``; заголовки до первого H2 запрещены;
- ``/llms-full.txt`` — наличие (соглашение, в саму спецификацию не входит);
- ``robots.txt`` — пускает ли он ИИ-ботов (решение — ``urllib.robotparser``,
  доказательство — строки самого файла);
- главная и до двух внутренних страниц — JSON-LD schema.org (типы),
  разметка FAQ, ``<link rel="alternate" type="text/markdown">`` (или
  заголовок ``Link``), как советует llmstxt.org.

Каждая находка — ``Issue(severity, code, url, evidence, fix)``:
доказательство — процитированная строка или число, не пересказ.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from .. import net
from .domains import registrable

USER_AGENT = f"Mozilla/5.0 (compatible; {net.user_agent('geo')}; +readiness)"
MAX_BYTES = 2_000_000
TIMEOUT = 15
#: Повторов при сетевом сбое (обрыв, TLS, таймаут). На ответ HTTP и на отказ
#: по адресу не повторяем.
RETRIES = 1

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}


@dataclass
class Issue:
    """Найденная проблема. Без доказательства проблема не записывается."""

    severity: str      # critical | major | minor
    code: str          # kebab-case
    url: str
    evidence: str      # процитированная строка или число
    fix: str           # что сделать


@dataclass
class Bot:
    token: str
    owner: str
    #: search — показ в ИИ-ответах; user — заход по просьбе пользователя;
    #: training — обучение моделей.
    role: str
    severity: str
    note: str


#: ИИ-боты и что значит их запрет. Роли — по справке владельцев.
BOTS: tuple[Bot, ...] = (
    Bot("OAI-SearchBot", "OpenAI", "search", "critical",
        "показ сайта в поиске ChatGPT; запрещённые сайты в ответах поиска ChatGPT не "
        "показываются (developers.openai.com/api/docs/bots)"),
    Bot("ChatGPT-User", "OpenAI", "user",  "major",
        "заходы по запросу пользователя ChatGPT; OpenAI предупреждает, что robots.txt "
        "к ним может не применяться"),
    Bot("GPTBot", "OpenAI", "training", "minor",
        "только обучение моделей OpenAI; на показ в поиске ChatGPT не влияет — за него "
        "отвечает OAI-SearchBot"),
    Bot("PerplexityBot", "Perplexity", "search", "critical",
        "показ сайта в ответах Perplexity (docs.perplexity.ai/docs/resources/perplexity-crawlers)"),
    Bot("Perplexity-User", "Perplexity", "user", "major",
        "заходы по запросу пользователя; Perplexity пишет, что этот агент обычно "
        "не соблюдает robots.txt"),
    Bot("Claude-SearchBot", "Anthropic", "search", "critical",
        "индексация для поиска Claude; запрет снижает видимость в ответах "
        "(privacy.claude.com, статья 8896518)"),
    Bot("Claude-User", "Anthropic", "user", "major",
        "заходы по запросу пользователя Claude; запрет не даёт подтянуть страницу в ответ"),
    Bot("ClaudeBot", "Anthropic", "training", "minor",
        "сбор данных для обучения моделей Anthropic"),
    Bot("Google-Extended", "Google", "training", "major",
        "не влияет на Google Поиск и не является фактором ранжирования; управляет "
        "обучением Gemini и grounding в приложениях Gemini и Vertex AI "
        "(developers.google.com/crawling/docs/crawlers-fetchers/google-common-crawlers)"),
    Bot("YandexAdditional", "Яндекс", "search", "critical",
        "ограничивает показ контента в быстрых ответах с YandexGPT и в ответах Поиска "
        "с Алисой (yandex.ru/support/webmaster/robot-workings/check-yandex-robots); "
        "страницы не сканирует"),
    Bot("YandexAdditionalBot", "Яндекс", "search", "critical",
        "второй токен того же назначения, что YandexAdditional (та же справка Яндекса)"),
)


@dataclass
class BotVerdict:
    token: str
    owner: str
    role: str
    allowed: bool
    evidence: str
    note: str


@dataclass
class Readiness:
    root: str
    checked_at: str
    llms_txt: str = "нет"
    llms_full_txt: str = "нет"
    robots_txt: str = "нет"
    bots: list[BotVerdict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    good: list[str] = field(default_factory=list)


# ─────────────────────────────── сеть ───────────────────────────────

#: Транспорт общий, из `yaseo.net`: только http/https на входе и на каждом шаге
#: перенаправления, внутренние адреса — только с разрешения (allow_private=True
#: или YASEO_ALLOW_PRIVATE=1). Локальные адреса — напрямую, остальные — через
#: прокси из окружения.


def _is_local(host: str) -> bool:
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


@dataclass
class Fetched:
    url: str
    status: int
    ctype: str
    text: str
    link_header: str = ""
    error: str = ""


def fetch(url: str, allow_private: bool | None = None) -> Fetched:
    """Один запрос. Не бросает: сбой уезжает в `error` одной строкой, без трейсбека.

    Адрес приводится к виду браузера (`net.safe_url`): пробел и кириллица
    в пути кодируются, иначе `http.client` отказал бы живой странице."""
    try:
        target = net.safe_url(url)
    except ValueError as e:
        return Fetched(url, 0, "", "", error=f"адрес не разобран: {e}")
    try:
        net.check_url(target, allow_private)
    except net.UnsafeURL as e:
        return Fetched(url, 0, "", "", error=str(e.reason))
    url = target
    host = urlsplit(url).hostname or ""
    opener = net.site_opener(proxy=not _is_local(host), follow=True,
                             allow_private=net.private_allowed(allow_private))
    got, transient = _fetch_once(opener, url)
    for _ in range(RETRIES):
        if not transient:
            break
        # Обрыв соединения или TLS бывает сбоем сети проверяющего (прокси),
        # а не сайта: повтор, чтобы не записать живой странице «page-unavailable».
        got, transient = _fetch_once(opener, url)
    return got


def _fetch_once(opener, url: str) -> tuple[Fetched, bool]:
    """Один запрос → (результат, был ли это сетевой сбой, который стоит повторить)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "*/*"})
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            raw = r.read(MAX_BYTES)
            ctype = r.headers.get("Content-Type", "")
            charset = r.headers.get_content_charset() or "utf-8"
            return Fetched(r.geturl(), r.status, ctype, raw.decode(charset, "replace"),
                           ", ".join(r.headers.get_all("Link") or [])), False
    except urllib.error.HTTPError as e:
        ctype = e.headers.get("Content-Type", "") if e.headers else ""
        e.close()
        return Fetched(url, e.code, ctype, ""), False
    except net.UnsafeURL as e:
        return Fetched(url, 0, "", "", error=str(e.reason)), False
    except urllib.error.URLError as e:
        return Fetched(url, 0, "", "", error=str(e.reason)), True
    except (TimeoutError, socket.timeout):
        return Fetched(url, 0, "", "", error=f"таймаут {TIMEOUT} с"), True
    except http.client.HTTPException as e:
        # Сервер ответил не по протоколу (оборванный ответ, кривые заголовки).
        return Fetched(url, 0, "", "",
                       error=f"ответ не по протоколу HTTP: {type(e).__name__}"), True
    except OSError as e:
        return Fetched(url, 0, "", "",
                       error=f"сетевая ошибка: {type(e).__name__}: {e}"), True
    except ValueError as e:
        return Fetched(url, 0, "", "", error=f"{type(e).__name__}: {e}"), False


def _root(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("Нужен url — адрес сайта, например https://example.ru")
    if "://" not in url:
        url = "https://" + url
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"Не похоже на адрес сайта: «{url}». Нужен http(s)://домен")
    try:
        return net.safe_url(urlunsplit((p.scheme, p.netloc, "/", "", "")))
    except ValueError as e:
        raise ValueError(f"Не похоже на адрес сайта: «{url}» ({e})") from None


def _q(line: str, n: int = 160) -> str:
    """Строка-доказательство: в кавычках, без переводов строк и вертикальных черт."""
    line = line.replace("\n", " ").replace("|", "¦").strip()
    if len(line) > n:
        line = line[:n] + "…"
    return f"«{line}»"


def _looks_html(f: Fetched) -> bool:
    head = f.text.lstrip()[:200].lower()
    return "text/html" in f.ctype.lower() or head.startswith("<!doctype html") or head.startswith("<html")


# ───────────────────────────── llms.txt ─────────────────────────────

_H1 = re.compile(r"^#\s+\S")
_H2 = re.compile(r"^##\s+\S")
_HX = re.compile(r"^#{3,6}\s+\S")
_LIST = re.compile(r"^\s*[-*+]\s+")
_MDLINK = re.compile(r"\[[^\]]+\]\([^)\s]+\)")


def check_llms_txt(text: str, url: str) -> list[Issue]:
    """Структура llms.txt по llmstxt.org. Возвращает находки."""
    issues: list[Issue] = []
    lines = text.lstrip("\ufeff").splitlines()
    nonempty = [(i, ln) for i, ln in enumerate(lines) if ln.strip()]
    if not nonempty:
        return [Issue("major", "llms-txt-empty", url, "файл пуст (0 непустых строк)",
                      "Заполните llms.txt: H1 с названием сайта, блок-цитату с кратким "
                      "описанием и разделы H2 со ссылками.")]

    first_i, first = nonempty[0]
    if not _H1.match(first):
        issues.append(Issue(
            "major", "llms-txt-no-h1", url, f"первая строка {_q(first)}",
            "Первой строкой поставьте заголовок H1 с названием сайта: «# Название». "
            "По llmstxt.org это единственный обязательный раздел."))
    h1s = [ln for _, ln in nonempty if _H1.match(ln)]
    if len(h1s) > 1:
        issues.append(Issue("minor", "llms-txt-many-h1", url,
                            f"H1 в файле: {len(h1s)}, второй — {_q(h1s[1])}",
                            "Оставьте один H1 в начале файла, остальные сделайте H2."))

    # Описание ищется сразу после H1; без H1 место описания не определено,
    # и отдельной находкой это не считается — дефект один, он уже назван.
    after = [ln for i, ln in nonempty if i > first_i]
    if _H1.match(first) and (not after or not after[0].lstrip().startswith(">")):
        ev = f"после заголовка идёт {_q(after[0])}" if after else "после заголовка ничего нет"
        issues.append(Issue("minor", "llms-txt-no-summary", url, ev,
                            "Сразу после H1 добавьте блок-цитату «> …» с кратким описанием сайта."))

    h2_idx = [i for i, ln in nonempty if _H2.match(ln)]
    pre = [ln for i, ln in nonempty if i > first_i and (not h2_idx or i < h2_idx[0])]
    for ln in pre:
        if _HX.match(ln):
            issues.append(Issue("minor", "llms-txt-heading-before-sections", url, _q(ln),
                                "До первого H2 допускаются абзацы и списки, но не заголовки. "
                                "Превратите заголовок в H2-раздел или в абзац."))
            break

    if not h2_idx:
        issues.append(Issue("minor", "llms-txt-no-sections", url,
                            "разделов H2 в файле: 0",
                            "Добавьте разделы «## …» со списками ссылок "
                            "«- [название](адрес): пояснение» на ключевые страницы."))
        return issues

    links_total = 0
    for n, start in enumerate(h2_idx):
        end = h2_idx[n + 1] if n + 1 < len(h2_idx) else len(lines)
        body = [ln for i, ln in nonempty if start < i < end]
        items = [ln for ln in body if _LIST.match(ln)]
        good = [ln for ln in items if _MDLINK.search(ln)]
        links_total += len(good)
        bad = [ln for ln in items if not _MDLINK.search(ln)]
        if bad:
            issues.append(Issue("minor", "llms-txt-item-without-link", url, _q(bad[0]),
                                "Каждый пункт списка в разделе H2 начинается со ссылки "
                                "«[название](адрес)», пояснение — после двоеточия."))
        if not items:
            issues.append(Issue("minor", "llms-txt-section-without-list", url,
                                f"раздел {_q(nonempty_line(lines, start))} без списка ссылок",
                                "В разделе H2 должен быть список ссылок «- [название](адрес)»."))
    if links_total == 0:
        issues.append(Issue("major", "llms-txt-no-links", url, "ссылок вида [название](адрес): 0",
                            "Добавьте ссылки на главные страницы — ради них файл и читают."))
    return issues


def nonempty_line(lines: list[str], i: int) -> str:
    return lines[i] if 0 <= i < len(lines) else ""


def llms_links(text: str, base: str) -> list[str]:
    out = []
    for m in re.finditer(r"\[[^\]]+\]\(([^)\s]+)\)", text):
        out.append(urljoin(base, m.group(1)))
    return out


# ───────────────────────────── robots.txt ─────────────────────────────

def _groups(text: str) -> list[tuple[list[tuple[int, str]], list[tuple[int, str]]]]:
    """Группы robots.txt: (строки User-agent, строки правил), с номерами строк."""
    groups: list[tuple[list[tuple[int, str]], list[tuple[int, str]]]] = []
    agents: list[tuple[int, str]] = []
    rules: list[tuple[int, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key = line.split(":", 1)[0].strip().lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append((n, raw.strip()))
        elif key in ("allow", "disallow") and agents:
            rules.append((n, raw.strip()))
    if agents:
        groups.append((agents, rules))
    return groups


def _agent_value(line: str) -> str:
    return line.split(":", 1)[1].split("#", 1)[0].strip().lower()


def _robots_evidence(text: str, token: str, path: str) -> str:
    """Строки robots.txt, из-за которых бот получил своё решение.
    Ищется группа так же, как в urllib.robotparser: первая, чей токен
    входит в имя бота; иначе группа «*»."""
    tok = token.lower()
    chosen = None
    for agents, rules in _groups(text):
        vals = [_agent_value(a) for _, a in agents]
        if any(v != "*" and v and v in tok for v in vals):
            chosen = (agents, rules)
            break
    if chosen is None:
        for agents, rules in _groups(text):
            if any(_agent_value(a) == "*" for _, a in agents):
                chosen = (agents, rules)
                break
    if chosen is None:
        return "в robots.txt нет ни группы для этого бота, ни группы «User-agent: *»"
    agents, rules = chosen
    agent_line = next((f"стр. {n}: {a}" for n, a in agents
                       if _agent_value(a) != "*" and _agent_value(a) in tok),
                      f"стр. {agents[0][0]}: {agents[0][1]}")
    hit = None
    for n, r in rules:
        val = r.split(":", 1)[1].split("#", 1)[0].strip()
        if val and path.startswith(val.rstrip("*").rstrip("$") or "/"):
            hit = f"стр. {n}: {r}"
            break
    return _q(agent_line + (" → " + hit if hit else " (правил для этого адреса нет)"), 200)


def check_robots(root: str, res: Readiness, sample_paths: list[str],
                 allow_private: bool | None = None) -> None:
    url = urljoin(root, "/robots.txt")
    f = fetch(url, allow_private)
    rp = urllib.robotparser.RobotFileParser(url)
    text = ""
    if f.status == 200 and not _looks_html(f):
        text = f.text
        rp.parse(text.splitlines())
        res.robots_txt = f"есть, {len(text.splitlines())} строк"
    elif f.status in (401, 403):
        rp.disallow_all = True
        res.robots_txt = f"закрыт, HTTP {f.status}"
        res.issues.append(Issue("critical", "robots-txt-forbidden", url, f"HTTP {f.status}",
                                "Отдавайте robots.txt с кодом 200: при 401/403 роботы "
                                "считают весь сайт закрытым."))
    elif f.status >= 500 or f.status == 0:
        rp.allow_all = True
        res.robots_txt = f"недоступен ({f.error or 'HTTP ' + str(f.status)})"
        res.issues.append(Issue("major", "robots-txt-unavailable", url,
                                f.error or f"HTTP {f.status}",
                                "Почините отдачу robots.txt: при ошибке сервера часть "
                                "роботов откладывает обход всего сайта."))
    else:
        rp.allow_all = True
        res.robots_txt = "нет (всё разрешено)" if f.status == 404 else f"HTTP {f.status}"

    paths = ["/"] + [p for p in sample_paths if p != "/"]
    for bot in BOTS:
        blocked = [p for p in paths if not rp.can_fetch(bot.token, urljoin(root, p))]
        allowed = not blocked
        if text:
            ev = _robots_evidence(text, bot.token, blocked[0] if blocked else "/")
        else:
            ev = res.robots_txt
        res.bots.append(BotVerdict(bot.token, bot.owner, bot.role, allowed, ev, bot.note))
        if allowed:
            continue
        where = "весь сайт" if "/" in blocked else "страницы " + ", ".join(blocked)
        if bot.role == "training":
            fix = (f"Если запрет {bot.token} — осознанное решение, оставьте. Иначе уберите "
                   f"Disallow для него. Смысл запрета: {bot.note}.")
        else:
            fix = (f"Разрешите {bot.token}: добавьте группу «User-agent: {bot.token}» с «Allow: /» "
                   f"или уберите Disallow. Запрет означает: {bot.note}.")
        res.issues.append(Issue(bot.severity, f"robots-blocks-{bot.token.lower()}",
                                url, f"{bot.token} закрыт ({where}): {ev}", fix))


# ───────────────────────────── страницы ─────────────────────────────

class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        self._in_ld = False
        self._buf: list[str] = []
        self.alternates: list[str] = []
        self.links: list[str] = []
        self.itemtypes: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and a.get("type", "").split(";")[0].strip().lower() == "application/ld+json":
            self._in_ld = True
            self._buf = []
        elif tag == "link":
            rels = a.get("rel", "").lower().split()
            if "alternate" in rels and a.get("type", "").lower().startswith("text/markdown"):
                self.alternates.append(a.get("href", ""))
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if a.get("itemtype"):
            self.itemtypes.append(a["itemtype"])

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.jsonld.append("".join(self._buf))

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)


def _types(node, out: list[str]) -> None:
    if isinstance(node, list):
        for x in node:
            _types(x, out)
    elif isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, list):
            out.extend(x for x in t if isinstance(x, str))
        for k, v in node.items():
            if k != "@type" and isinstance(v, (list, dict)):
                _types(v, out)


def _md_alternates_from_header(link_header: str) -> list[str]:
    out = []
    for part in link_header.split(","):
        if re.search(r'rel="?[^";]*\balternate\b', part, re.I) and "text/markdown" in part.lower():
            m = re.search(r"<([^>]+)>", part)
            if m:
                out.append(m.group(1))
    return out


def check_page(url: str, res: Readiness, is_home: bool,
               allow_private: bool | None = None) -> list[str]:
    """Проверяет страницу, возвращает найденные внутренние ссылки."""
    f = fetch(url, allow_private)
    info = {"url": url, "status": f.status, "jsonld_types": [], "faq": False,
            "markdown_alternate": ""}
    res.pages.append(info)
    if f.status != 200:
        res.issues.append(Issue("critical" if is_home else "major", "page-unavailable", url,
                                f.error or f"HTTP {f.status}",
                                "Страница должна отдаваться с кодом 200 — иначе ИИ-боты её не прочитают."))
        return []
    p = _PageParser()
    try:
        p.feed(f.text)
    except Exception:  # noqa: BLE001 — кривой HTML не повод падать
        pass

    types: list[str] = []
    for block in p.jsonld:
        try:
            _types(json.loads(block), types)
        except json.JSONDecodeError as e:
            res.issues.append(Issue("major", "jsonld-invalid", url,
                                    f"{_q(block.strip(), 80)} — {e.msg}, позиция {e.pos}",
                                    "Исправьте JSON в блоке application/ld+json: с ошибкой "
                                    "разметку не прочитает ни поиск, ни ИИ."))
    micro = [t.rstrip("/").rsplit("/", 1)[-1] for t in p.itemtypes]
    info["jsonld_types"] = sorted(set(types))
    faq = "FAQPage" in types or "FAQPage" in micro
    info["faq"] = faq

    if not p.jsonld:
        if micro:
            res.good.append(f"{url}: разметка microdata ({', '.join(sorted(set(micro)))}), JSON-LD нет")
        res.issues.append(Issue(
            "major" if is_home else "minor", "jsonld-missing", url,
            "блоков <script type=\"application/ld+json\"> на странице: 0",
            "Добавьте JSON-LD schema.org: на главной — Organization (или LocalBusiness) и "
            "WebSite, на страницах услуг — Service, на страницах с вопросами — FAQPage."))
    else:
        res.good.append(f"{url}: JSON-LD — {', '.join(info['jsonld_types']) or 'без @type'}")
        if is_home and not ({"Organization", "LocalBusiness", "Corporation"} & set(types)):
            res.issues.append(Issue(
                "minor", "jsonld-no-organization", url,
                f"типы JSON-LD на главной: {', '.join(info['jsonld_types']) or 'нет @type'}",
                "Опишите компанию типом Organization (name, url, logo, sameAs) — по нему ИИ "
                "связывает сайт с брендом."))
    if faq:
        res.good.append(f"{url}: есть разметка FAQPage")

    alts = [a for a in p.alternates if a] + _md_alternates_from_header(f.link_header)
    if alts:
        md_url = urljoin(url, alts[0])
        info["markdown_alternate"] = md_url
        if not _own_site(md_url, url):
            # Чужой адрес не запрашиваем: страница не решает, куда ходит проверка.
            res.issues.append(Issue(
                "minor", "markdown-alternate-foreign", url,
                f"rel=alternate text/markdown → {md_url}: другой сайт, не запрашивался",
                "Markdown-версия страницы должна лежать на том же сайте, что и страница."))
            return [urljoin(url, h) for h in p.links]
        mf = fetch(md_url, allow_private)
        if mf.status != 200 or _looks_html(mf):
            res.issues.append(Issue(
                "major", "markdown-alternate-broken", url,
                f"rel=alternate text/markdown → {md_url}: "
                + (f"HTTP {mf.status}" if mf.status != 200 else f"отдаётся как {mf.ctype or 'HTML'}"),
                "Markdown-версия, на которую ссылается страница, должна отдаваться с кодом 200 "
                "и не быть HTML."))
        else:
            res.good.append(f"{url}: Markdown-версия {md_url} отдаётся")
    elif is_home:
        res.issues.append(Issue(
            "minor", "markdown-alternate-missing", url,
            "<link rel=\"alternate\" type=\"text/markdown\"> и заголовка Link с ним: 0",
            "Отдавайте Markdown-версию страниц (page.md или index.html.md) и ссылайтесь на неё "
            "<link rel=\"alternate\" type=\"text/markdown\" href=\"…\"> — так советует llmstxt.org."))
    return [urljoin(url, h) for h in p.links]


def _own_site(url: str, root: str) -> bool:
    """Тот же регистрируемый домен и http(s): blog.example.ru ~ example.ru."""
    a, b = urlsplit(url), urlsplit(root)
    if a.scheme not in ("http", "https") or not a.hostname or not b.hostname:
        return False
    return registrable(a.hostname) == registrable(b.hostname)


def _same_site(url: str, root: str) -> bool:
    a, b = urlsplit(url), urlsplit(root)
    return a.scheme in ("http", "https") and a.netloc == b.netloc


def _page_candidates(root: str, links: list[str], limit: int) -> list[str]:
    out: list[str] = []
    skip = re.compile(r"\.(txt|md|xml|json|pdf|jpe?g|png|gif|svg|webp|zip|css|js)$", re.I)
    for u in links:
        p = urlsplit(u)
        clean = urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))
        try:
            # `/о-компании/` и `/%D0%BE-…/` — одна страница.
            clean = net.safe_url(clean)
        except ValueError:
            continue
        if not _same_site(clean, root) or skip.search(p.path) or (p.path or "/") == "/":
            continue
        if clean not in out:
            out.append(clean)
        if len(out) >= limit:
            break
    return out


# ───────────────────────────── сборка ─────────────────────────────

def check(url: str, extra_pages: int = 2, allow_private: bool | None = None) -> Readiness:
    """
    Проверка готовности сайта.

    `allow_private` — разрешить внутренние адреса (localhost, 10/8, 192.168/16 …).
    По умолчанию решает YASEO_ALLOW_PRIVATE; без разрешения такой адрес
    отклоняется с ValueError и понятным текстом.
    """
    root = _root(url)
    allow = net.private_allowed(allow_private)
    try:
        net.check_url(root, allow)
    except net.UnsafeURL as e:
        raise ValueError(str(e.reason)) from None
    res = Readiness(root=root, checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    home = fetch(root, allow)
    if home.status == 0:
        raise ValueError(f"Сайт {root} не отвечает: {home.error}")

    llms_url = urljoin(root, "/llms.txt")
    lf = fetch(llms_url, allow)
    llms_link_list: list[str] = []
    if lf.status == 200 and not _looks_html(lf):
        res.llms_txt = f"есть, {len(lf.text.splitlines())} строк"
        found = check_llms_txt(lf.text, llms_url)
        res.issues.extend(found)
        if not found:
            res.good.append("llms.txt соответствует структуре llmstxt.org")
        llms_link_list = llms_links(lf.text, root)
    elif lf.status == 200:
        res.llms_txt = "вместо файла отдаётся HTML"
        res.issues.append(Issue("major", "llms-txt-is-html", llms_url,
                                f"Content-Type {lf.ctype or '—'}, начало {_q(lf.text.lstrip()[:60])}",
                                "Сервер отвечает страницей вместо файла. Положите в корень "
                                "текстовый /llms.txt в формате Markdown."))
    else:
        res.llms_txt = f"нет (HTTP {lf.status or lf.error})"
        res.issues.append(Issue("major", "llms-txt-missing", llms_url,
                                f"HTTP {lf.status}" if lf.status else lf.error,
                                "Создайте /llms.txt: «# Название», «> краткое описание», "
                                "разделы «## …» со ссылками на ключевые страницы (llmstxt.org)."))

    full_url = urljoin(root, "/llms-full.txt")
    ff = fetch(full_url, allow)
    if ff.status == 200 and not _looks_html(ff):
        res.llms_full_txt = f"есть, {len(ff.text)} символов"
        res.good.append("llms-full.txt отдаётся")
    else:
        res.llms_full_txt = "нет" if ff.status != 200 else "вместо файла отдаётся HTML"
        res.issues.append(Issue("minor", "llms-full-txt-missing", full_url,
                                f"HTTP {ff.status}" if ff.status != 200 else f"Content-Type {ff.ctype}",
                                "По желанию: соберите /llms-full.txt — полный текст ключевых "
                                "страниц одним файлом. Это соглашение, а не часть спецификации."))

    links = check_page(root, res, is_home=True, allow_private=allow)
    candidates = _page_candidates(root, links + llms_link_list, extra_pages)
    for u in candidates:
        check_page(u, res, is_home=False, allow_private=allow)

    if not any(pg["faq"] for pg in res.pages):
        res.issues.append(Issue(
            "minor", "faq-schema-missing", root,
            f"FAQPage на проверенных страницах: 0 из {len(res.pages)}",
            "На страницах с вопросами и ответами добавьте разметку FAQPage — ИИ-ответы охотно "
            "берут готовые пары «вопрос — ответ»."))

    check_robots(root, res, [urlsplit(u).path or "/" for u in candidates], allow_private=allow)
    res.issues.sort(key=lambda i: SEVERITY_ORDER.get(i.severity, 9))
    return res


def render(res: Readiness) -> str:
    lines = [f"Готовность к ИИ-поиску: {res.root}", f"Снято {res.checked_at}. Ключи не нужны, "
             "ничего не тратится.", "",
             "| что | состояние |", "|---|---|",
             f"| /llms.txt | {res.llms_txt} |",
             f"| /llms-full.txt | {res.llms_full_txt} |",
             f"| robots.txt | {res.robots_txt} |"]
    for pg in res.pages:
        lines.append(f"| {pg['url']} | HTTP {pg['status']}; JSON-LD: "
                     f"{', '.join(pg['jsonld_types']) or 'нет'}; FAQ: {'да' if pg['faq'] else 'нет'}; "
                     f"Markdown-версия: {pg['markdown_alternate'] or 'нет'} |")

    lines += ["", "ИИ-боты в robots.txt:", "",
              "| бот | чей | назначение | доступ | доказательство |", "|---|---|---|---|---|"]
    role = {"search": "показ в ИИ-ответах", "user": "заход по просьбе пользователя",
            "training": "обучение моделей"}
    for b in res.bots:
        lines.append(f"| {b.token} | {b.owner} | {role.get(b.role, b.role)} | "
                     f"{'разрешён' if b.allowed else 'ЗАКРЫТ'} | {b.evidence} |")
    lines += ["", "Google-Extended не влияет на Google Поиск и ранжирование, но управляет "
              "обучением Gemini и grounding в Gemini и Vertex AI. YandexAdditional управляет "
              "показом контента в быстрых ответах с YandexGPT и в ответах Поиска с Алисой.",
              "Решение по robots.txt принимает urllib.robotparser: он берёт первую подходящую "
              "группу и первое подходящее правило, а не самое длинное, как Google."]

    lines.append("")
    if not res.issues:
        lines.append("Проблем не найдено.")
    else:
        counts: dict[str, int] = {}
        for i in res.issues:
            counts[i.severity] = counts.get(i.severity, 0) + 1
        lines.append("Найдено: " + ", ".join(f"{k} — {v}" for k, v in counts.items()))
        lines += ["", "| тяжесть | код | адрес | доказательство | что сделать |", "|---|---|---|---|---|"]
        for i in res.issues:
            lines.append(f"| {i.severity} | {i.code} | {i.url} | {i.evidence} | {i.fix} |")
    if res.good:
        lines += ["", "Что уже хорошо:"] + [f"- {g}" for g in res.good]
    return "\n".join(lines)
