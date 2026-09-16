#!/usr/bin/env python3
"""
Хранилище yaseo — SQLite.

Путь к базе: переменная `YASEO_DB`, по умолчанию
`$XDG_DATA_HOME/yaseo/yaseo.db` (`~/.local/share/yaseo/yaseo.db`).

Без истории инструмент показывает разовый срез. Здесь появляется время:
как менялась позиция, как менялась конкурентность, что было в топе месяц назад.

Публичный API:
- init_db()                          — создать схему, идемпотентно
- start_run(kind, region, note)      -> run_id
- finish_run(run_id)
- save_serp(result, run_id)          -> snapshot_id
- save_competition(comp, snapshot_id, run_id)
- save_frequency(freq, run_id)
- track(query, region) / untrack(...) / tracked(region)
- set_project_terms(project_id, brand_terms, seed_terms)
- save_core_entries(project_id, entries) / get_core(project_id, kind=None)
- position_history(query, domain)    -> список замеров
- latest_positions(domain, region)   -> текущие позиции с дельтой
- competition_history(query, region)
- stats()                            -> что накоплено

Запуск как скрипт:
    yaseo storage --init
    yaseo storage --stats
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import config


def db_path() -> Path:
    """Путь к базе читается при каждом обращении: переменную можно сменить в процессе."""
    return config.db_path()

SCHEMA = """
PRAGMA journal_mode = WAL;

-- Один прогон: сбор выдачи, замер позиций, снятие частотности.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,              -- serp | tracking | frequency | audit
    region      INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    note        TEXT
);

-- Снимок выдачи по одному запросу.
CREATE TABLE IF NOT EXISTS serp_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES runs(id),
    query      TEXT NOT NULL,
    region     INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    found_all  INTEGER,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_snap_query ON serp_snapshots(query, region, fetched_at);

-- Документы снимка.
CREATE TABLE IF NOT EXISTS serp_docs (
    snapshot_id      INTEGER NOT NULL REFERENCES serp_snapshots(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    url              TEXT,
    domain           TEXT,
    title            TEXT,
    title_hlwords    INTEGER,
    url_depth        INTEGER,
    domain_doccount  INTEGER,
    modtime          TEXT,
    PRIMARY KEY (snapshot_id, position)
);
CREATE INDEX IF NOT EXISTS ix_docs_domain ON serp_docs(domain);

-- Конкурентность запроса на момент снимка.
CREATE TABLE IF NOT EXISTS competition (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES runs(id),
    snapshot_id   INTEGER REFERENCES serp_snapshots(id) ON DELETE CASCADE,
    query         TEXT NOT NULL,
    region        INTEGER NOT NULL,
    calculated_at TEXT NOT NULL,
    score         REAL,
    band          TEXT,
    our_position  INTEGER,
    factors_json  TEXT,                     -- разбор по факторам: число остаётся проверяемым
    error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_comp_query ON competition(query, region, calculated_at);

-- Частотность из Wordstat.
CREATE TABLE IF NOT EXISTS keyword_freq (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES runs(id),
    phrase     TEXT NOT NULL,
    region     TEXT NOT NULL,
    freq       INTEGER,
    fetched_at TEXT NOT NULL,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_freq_phrase ON keyword_freq(phrase, region, fetched_at);

-- Замеры позиций: денормализовано ради быстрой истории.
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES runs(id),
    snapshot_id INTEGER REFERENCES serp_snapshots(id) ON DELETE CASCADE,
    query       TEXT NOT NULL,
    region      INTEGER NOT NULL,
    domain      TEXT NOT NULL,
    position    INTEGER,                    -- NULL = в снятом топе не найден
    url         TEXT,
    checked_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pos_lookup ON positions(domain, query, region, checked_at);

-- Что отслеживаем регулярно.
CREATE TABLE IF NOT EXISTS tracked_queries (
    query    TEXT NOT NULL,
    region   INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    active   INTEGER NOT NULL DEFAULT 1,
    note     TEXT,
    PRIMARY KEY (query, region)
);

-- Сверка с живой выдачей. Search API и браузер — разные пути обслуживания,
-- и совпадение надо не предполагать, а измерять. Человек называет, что видит;
-- инструмент в тот же момент снимает своё. Копится величина расхождения.
CREATE TABLE IF NOT EXISTS calibration (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    query         TEXT NOT NULL,
    region        INTEGER NOT NULL,
    domain        TEXT NOT NULL,
    seen_position INTEGER,               -- что видит человек, NULL = не нашёл
    api_position  INTEGER,               -- что показал замер, NULL = нет в топе
    api_samples   TEXT,                  -- выборки замера, JSON
    checked_at    TEXT NOT NULL,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS ix_calib ON calibration(query, region, checked_at);

-- Проект = сайт, который ведём: свой, клиентский, стартап.
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    domain     TEXT NOT NULL,
    region     INTEGER NOT NULL DEFAULT 225,
    note       TEXT,
    created_at TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1
);

-- Статья блога с целевым запросом. Нужна, чтобы мерить не сайт вообще,
-- а конкретный материал: вышла ли ИМЕННО эта страница по своему запросу.
CREATE TABLE IF NOT EXISTS articles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER REFERENCES projects(id),
    url          TEXT NOT NULL,
    title        TEXT,
    target_query TEXT NOT NULL,
    extra_queries TEXT,                     -- JSON-список дополнительных запросов
    published_at TEXT,
    created_at   TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    note         TEXT,
    UNIQUE (url, target_query)
);
CREATE INDEX IF NOT EXISTS ix_articles_project ON articles(project_id, active);

-- Замер по статье: вышла ли она сама, или вместо неё вышла другая наша страница.
CREATE TABLE IF NOT EXISTS article_checks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id   INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    run_id       INTEGER REFERENCES runs(id),
    snapshot_id  INTEGER REFERENCES serp_snapshots(id) ON DELETE SET NULL,
    checked_at   TEXT NOT NULL,
    query        TEXT NOT NULL,
    position     INTEGER,                   -- позиция ИМЕННО этой статьи, NULL = нет в топе
    domain_position INTEGER,                -- позиция любой нашей страницы по этому запросу
    ranking_url  TEXT,                      -- какая наша страница вышла по факту
    competition  REAL,
    is_target    INTEGER                    -- 1 = вышла целевая статья, 0 = вышла другая
);
CREATE INDEX IF NOT EXISTS ix_checks_article ON article_checks(article_id, checked_at);

-- Семантическое ядро проекта. Ядро принадлежит проекту, а не инструменту:
-- подключили другой сайт — собираем его семантику под него.
-- Решение по фразе хранится вместе с доказательством: состав топа, наша позиция,
-- частотность. Поэтому вердикт проверяем и оспорим, а не принимается на веру.
CREATE TABLE IF NOT EXISTS core_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id),
    phrase TEXT NOT NULL,
    kind TEXT NOT NULL,            -- brand | target | adjacent
    freq INTEGER,                  -- Wordstat; NULL = не измеряли, 0 = ниже порога
    source TEXT,                   -- seed | expansion | wordstat
    commercial INTEGER,            -- 1/0/NULL
    competition REAL,
    our_position INTEGER,
    top_domains TEXT,              -- JSON, доказательство
    verdict TEXT NOT NULL,         -- ядро | периферия | отбросить
    reason TEXT,
    collected_at TEXT NOT NULL,
    UNIQUE (project_id, phrase)
);
CREATE INDEX IF NOT EXISTS ix_core_project ON core_entries(project_id, kind, verdict);

-- Технический аудит. Результат хранится, чтобы сегодняшний прогон было с чем
-- сравнить: без истории аудит показывает только разовый срез.
CREATE TABLE IF NOT EXISTS audit_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id),
    url TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pages INTEGER,                 -- сколько страниц обошли
    findings INTEGER,              -- всего находок
    critical INTEGER,
    major INTEGER,
    minor INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_runs_url ON audit_runs(url, started_at DESC);

-- Находка хранится ровно в том виде, в каком её отдаёт `yaseo.audit.Issue`:
-- тип проблемы, страница, доказательство и что сделать. Доказательство —
-- процитированный тег или число, не пересказ: без него находку не проверить.
CREATE TABLE IF NOT EXISTS audit_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES audit_runs(id) ON DELETE CASCADE,
    severity TEXT NOT NULL,        -- critical | major | minor
    code TEXT NOT NULL,            -- kebab-case, напр. "missing-title"
    page_url TEXT,
    evidence TEXT,
    fix TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_findings_run ON audit_findings(run_id, severity);

-- Показы, клики и средняя позиция от владельца сайта — Яндекс.Вебмастер.
-- Это первичка поисковика, а не наш замер выдачи: она знает то, чего замер не
-- видит принципиально — сколько раз страницу показали и сколько раз по ней
-- кликнули. Позиция здесь тоже своя: средняя по показам за окно, а не медиана
-- снимков. Две позиции — две разные метрики с разными источниками, и
-- называются они по-разному.
--
-- Окно входит в ключ: выгрузка за другой период не затирает предыдущую, а
-- ложится рядом. Повторная выгрузка того же окна обновляет свои же строки.
CREATE TABLE IF NOT EXISTS search_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id TEXT NOT NULL,              -- как в Вебмастере: https:example.ru:443
    project_id INTEGER REFERENCES projects(id),
    query TEXT NOT NULL,
    query_id TEXT,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    shows INTEGER,
    clicks INTEGER,
    avg_show_position REAL,
    avg_click_position REAL,
    fetched_at TEXT NOT NULL,
    UNIQUE (host_id, query, date_from, date_to)
);
CREATE INDEX IF NOT EXISTS ix_sq_host ON search_queries(host_id, date_to DESC, shows DESC);

-- Какая страница собирает показы по запросу. Отвечает на вопрос, на который
-- замер позиции не отвечает: вышла страница вообще или вышла не та.
CREATE TABLE IF NOT EXISTS search_query_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id TEXT NOT NULL,
    project_id INTEGER REFERENCES projects(id),
    query TEXT NOT NULL,
    url TEXT,                           -- путь, как отдаёт Вебмастер: /blog/…/
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    shows INTEGER,
    clicks INTEGER,
    avg_position REAL,
    demand INTEGER,                     -- спрос по Вебмастеру: сколько раз запрос задали
    fetched_at TEXT NOT NULL,
    UNIQUE (host_id, query, date_from, date_to)
);
CREATE INDEX IF NOT EXISTS ix_sqp_host ON search_query_pages(host_id, date_to DESC);

-- Поведение на посадочной странице — Яндекс.Метрика. Показы и клики говорят,
-- дошёл ли человек до сайта; это говорит, что было дальше.
CREATE TABLE IF NOT EXISTS metrika_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    counter INTEGER NOT NULL,
    project_id INTEGER REFERENCES projects(id),
    url TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    source TEXT NOT NULL,               -- organic | all: разные совокупности
    visits INTEGER,
    bounce_rate REAL,
    page_depth REAL,
    avg_duration REAL,
    fetched_at TEXT NOT NULL,
    UNIQUE (counter, url, date_from, date_to, source)
);
CREATE INDEX IF NOT EXISTS ix_mp_counter ON metrika_pages(counter, date_to DESC, visits DESC);

"""

#: Колонки, добавленные после первого выпуска схемы. Ключ — таблица.
MIGRATIONS: dict[str, dict[str, str]] = {
    "tracked_queries": {"project_id": "INTEGER"},
    # Выборки замера: JSON-список позиций по каждому снимку. Без них разброс
    # неизвестен, и любое колебание внутри шума читается как движение.
    "positions": {"samples": "TEXT"},
    # Семена ядра лежат у проекта: брендовые фразы и фразы по роду деятельности.
    # Оба поля — JSON-списки.
    # `webmaster_host` и `metrika_counter` — привязка проекта к первичке Яндекса.
    # Хранятся у проекта, чтобы интерфейс не ходил в чужой API за адресом хоста
    # на каждый рендер страницы.
    # `service_stems` и `object_stems` — словарь ниши проекта. JSON-списки основ
    # слов: что проект продаёт и кому он это продаёт. Пусто значит «не задан», и
    # тогда фильтр ниши не применяется (`pool.niche_stems` говорит это вслух).
    # Словарь принадлежит проекту: у двух сайтов с разными продуктами общего
    # словаря быть не может, и чужой словарь отбрасывает законные фразы.
    "projects": {"brand_terms": "TEXT", "seed_terms": "TEXT",
                 "webmaster_host": "TEXT", "metrika_counter": "INTEGER",
                 "service_stems": "TEXT", "object_stems": "TEXT"},
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path: Path | None = None):
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path | None = None) -> Path:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    return Path(path) if path else db_path()


def _migrate(conn) -> None:
    """Догоняет схему на существующей базе. Идемпотентно."""
    for table, columns in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# ──────────────────────────── проекты ────────────────────────────

def add_project(name: str, domain: str, region: int = 225, note: str | None = None,
                path: Path | None = None) -> int:
    dom = domain.lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO projects (name, domain, region, note, created_at, active) VALUES (?,?,?,?,?,1) "
            "ON CONFLICT(name) DO UPDATE SET domain=excluded.domain, region=excluded.region, active=1",
            (name, dom, region, note, now()),
        )
        row = conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()
        return int(row["id"])


def list_projects(active_only: bool = True, path: Path | None = None) -> list[dict]:
    sql = "SELECT * FROM projects"
    if active_only:
        sql += " WHERE active=1"
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY name")]


def get_project(name_or_id, path: Path | None = None) -> dict | None:
    with connect(path) as conn:
        if isinstance(name_or_id, int) or str(name_or_id).isdigit():
            row = conn.execute("SELECT * FROM projects WHERE id=?", (int(name_or_id),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM projects WHERE name=?", (name_or_id,)).fetchone()
        return dict(row) if row else None


def remove_project(name_or_id, path: Path | None = None) -> None:
    p = get_project(name_or_id, path)
    if not p:
        return
    with connect(path) as conn:
        conn.execute("UPDATE projects SET active=0 WHERE id=?", (p["id"],))


def track_for_project(project_id: int, query: str, region: int,
                      path: Path | None = None, steal: bool = False) -> str:
    """
    Завести фразу проекту. Возвращает, что произошло: `добавлено` ·
    `оставлено` · `у чужого`.

    Ключ таблицы — (фраза, регион), то есть фраза принадлежит одному проекту.
    Простой `ON CONFLICT ... SET project_id` отдавал бы фразу тому, кто
    сохранил последним, молча и без следа — второй проект забирал бы фразы
    первого целиком.

    Поэтому чужое не забирается без явного слова: `steal=True` передаёт только
    тот путь, где человек уже увидел, у кого берут, и согласился.
    """
    with connect(path) as conn:
        row = conn.execute(
            "SELECT project_id FROM tracked_queries WHERE query=? AND region=?",
            (query, region),
        ).fetchone()
        чей = row["project_id"] if row else None
        if row and чей is not None and int(чей) != int(project_id) and not steal:
            return "у чужого"
        conn.execute(
            "INSERT INTO tracked_queries (query, region, added_at, active, project_id) VALUES (?,?,?,1,?) "
            "ON CONFLICT(query, region) DO UPDATE SET active=1, project_id=excluded.project_id",
            (query, region, now(), project_id),
        )
        return "оставлено" if row else "добавлено"


def set_project_terms(project_id: int, brand_terms: list[str] | None = None,
                      seed_terms: list[str] | None = None, path: Path | None = None) -> None:
    """
    Семена ядра проекта: брендовые фразы и фразы по роду деятельности.
    Лежат у проекта, потому что ядро принадлежит проекту: подключили другой
    сайт — у него свои семена, а инструмент один.
    """
    sets: list[str] = []
    args: list = []
    if brand_terms is not None:
        sets.append("brand_terms=?")
        args.append(json.dumps(brand_terms, ensure_ascii=False))
    if seed_terms is not None:
        sets.append("seed_terms=?")
        args.append(json.dumps(seed_terms, ensure_ascii=False))
    if not sets:
        return
    args.append(project_id)
    with connect(path) as conn:
        conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", args)


def project_stems(project_id: int, path: Path | None = None
                  ) -> tuple[list[str] | None, list[str] | None]:
    """
    Словарь ниши проекта: (основы услуг, основы объекта). `None` — не задан.

    Разница между «не задан» и «пустой» здесь такая же существенная, как у
    каналов. Пустой список означал бы проект, который не продаёт ничего, и всё
    его ядро законно уехало бы в «отбросить». Не задан — значит словарём никто
    не занимался, и фильтр ниши не применяется. Что именно решено, показывает
    `pool.niche_stems`, а не это место: здесь только хранение.
    """
    p = get_project(project_id, path)
    if not p:
        return None, None

    def _list(raw) -> list[str] | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(value, list):
            return None
        items = [str(v).strip().lower() for v in value if str(v).strip()]
        return items or None

    return _list(p.get("service_stems")), _list(p.get("object_stems"))


def set_project_stems(project_id: int, service_stems: list[str] | None = None,
                      object_stems: list[str] | None = None,
                      path: Path | None = None
                      ) -> tuple[list[str] | None, list[str] | None]:
    """
    Задать словарь ниши проекта. `None` или пусто — снять словарь.

    Основы пишутся как основы слов, без окончаний:
    сверка идёт по началу слова (`pool._stem_hits`), потому что русская
    морфология даёт «окна», «окон», «остекления», и список точных форм
    пришлось бы вести бесконечно.

    Записывается здесь, а не голым UPDATE из скрипта, по той же причине, по
    которой здесь живут семена и каналы: одно место знает, в каком виде поле
    лежит в базе. Скрипт, пишущий JSON руками, эту договорённость дублирует и
    рано или поздно разойдётся с ней.
    """
    sets: list[str] = []
    args: list = []
    for column, value in (("service_stems", service_stems),
                          ("object_stems", object_stems)):
        if value is None:
            continue
        clean = [str(v).strip().lower() for v in value if str(v).strip()]
        sets.append(f"{column}=?")
        args.append(json.dumps(clean, ensure_ascii=False) if clean else None)
    if not sets:
        return project_stems(project_id, path)

    args.append(int(project_id))
    with connect(path) as conn:
        conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", args)
    return project_stems(project_id, path)


def save_core_entries(project_id: int, entries, path: Path | None = None) -> int:
    """
    Кладёт строки ядра. Повторный прогон переписывает решение по той же фразе:
    ядро — текущее состояние, а не журнал. История решения остаётся в отчётах,
    которые датированы.
    """
    rows = []
    for e in entries:
        d = e if isinstance(e, dict) else asdict(e)
        commercial = d.get("commercial")
        rows.append((
            project_id, d["phrase"], d["kind"], d.get("freq"), d.get("source"),
            None if commercial is None else int(bool(commercial)),
            d.get("competition"), d.get("our_position"),
            json.dumps(d.get("top_domains") or [], ensure_ascii=False),
            d["verdict"], d.get("reason"), d.get("collected_at") or now(),
        ))

    with connect(path) as conn:
        conn.executemany(
            "INSERT INTO core_entries (project_id, phrase, kind, freq, source, commercial, "
            "competition, our_position, top_domains, verdict, reason, collected_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            # Проход, который чего-то НЕ мерил, не имеет права затирать то, что
            # было измерено. Сбор через Wordstat (`--collect`) выдачу не снимает,
            # и без этой защиты он обнулял бы конкурентность, нашу позицию и
            # состав топа, снятые платными запросами. Поэтому измеренные поля берутся новые только
            # тогда, когда они непустые.
            #
            # Вердикт — отдельный случай. «Отбросить» приходит от правил, а не от
            # выдачи, и правило обязано срабатывать всегда. А вот понижать вердикт,
            # заработанный проверкой выдачи, до «периферии» проход без проверки
            # не должен: это подмена измерения его отсутствием.
            "ON CONFLICT(project_id, phrase) DO UPDATE SET "
            "kind=excluded.kind, freq=excluded.freq, source=excluded.source, "
            "commercial=COALESCE(excluded.commercial, core_entries.commercial), "
            "competition=COALESCE(excluded.competition, core_entries.competition), "
            "our_position=COALESCE(excluded.our_position, core_entries.our_position), "
            "top_domains=CASE WHEN excluded.top_domains IN ('[]','') "
            "  THEN core_entries.top_domains ELSE excluded.top_domains END, "
            "verdict=CASE "
            "  WHEN excluded.verdict='отбросить' THEN excluded.verdict "
            "  WHEN excluded.competition IS NULL AND excluded.our_position IS NULL "
            "       AND core_entries.verdict='ядро' THEN core_entries.verdict "
            "  ELSE excluded.verdict END, "
            "reason=excluded.reason, collected_at=excluded.collected_at",
            rows,
        )
    return len(rows)


def get_core(project_id: int, kind: str | None = None, verdict: str | None = None,
             path: Path | None = None) -> list[dict]:
    """Ядро проекта. Частотность NULL — не измеряли, 0 — ниже порога отчётности."""
    sql = "SELECT * FROM core_entries WHERE project_id=?"
    args: list = [project_id]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if verdict:
        sql += " AND verdict=?"
        args.append(verdict)
    sql += " ORDER BY kind, freq IS NULL, freq DESC, phrase"

    with connect(path) as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]

    for r in rows:
        try:
            r["top_domains"] = json.loads(r.get("top_domains") or "[]")
        except json.JSONDecodeError:
            r["top_domains"] = []
        r["commercial"] = None if r.get("commercial") is None else bool(r["commercial"])
    return rows


def project_queries(project_id: int, path: Path | None = None) -> list[str]:
    with connect(path) as conn:
        return [
            r["query"]
            for r in conn.execute(
                "SELECT query FROM tracked_queries WHERE project_id=? AND active=1 ORDER BY query",
                (project_id,),
            )
        ]


# ──────────────────────────── статьи ────────────────────────────

def add_article(project_id: int, url: str, target_query: str, title: str | None = None,
                extra_queries: list[str] | None = None, published_at: str | None = None,
                note: str | None = None, path: Path | None = None) -> int:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO articles (project_id, url, title, target_query, extra_queries, "
            "published_at, created_at, active, note) VALUES (?,?,?,?,?,?,?,1,?) "
            "ON CONFLICT(url, target_query) DO UPDATE SET "
            "title=excluded.title, extra_queries=excluded.extra_queries, active=1",
            (project_id, url, title, target_query,
             json.dumps(extra_queries or [], ensure_ascii=False), published_at, now(), note),
        )
        row = conn.execute(
            "SELECT id FROM articles WHERE url=? AND target_query=?", (url, target_query)
        ).fetchone()
        return int(row["id"])


def list_articles(project_id: int | None = None, active_only: bool = True,
                  path: Path | None = None) -> list[dict]:
    sql = "SELECT * FROM articles"
    where, args = [], []
    if active_only:
        where.append("active=1")
    if project_id is not None:
        where.append("project_id=?")
        args.append(project_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    with connect(path) as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]
    for r in rows:
        try:
            r["extra_queries"] = json.loads(r.get("extra_queries") or "[]")
        except json.JSONDecodeError:
            r["extra_queries"] = []
    return rows


def set_article_published(url: str, published_at: str | None,
                          path: Path | None = None) -> list[dict]:
    """
    Дата выхода страницы. Единственное место, где она пишется.

    Без даты страницу нельзя судить по сроку: отсчитывать не от чего, и статья
    выпадает из итога вовсе — не в успех и не в брак, а в никуда.

    Ищет по нормализованному пути, а не по строке адреса: Вебмастер отдаёт
    `/blog/x/`, реестр хранит `https://example.ru/blog/x/`, человек называет
    `/blog/x`. Сравнение построчно нашло бы ноль строк и промолчало бы.

    Одному адресу может принадлежать несколько строк реестра — по строке на
    целевой запрос. Дата у страницы одна, поэтому проставляется всем сразу:
    вышла страница, а не запрос.

    Возвращает тронутые строки с прежним значением: вызывающий обязан иметь
    возможность сказать человеку, что именно изменилось.
    """
    key = normalize_path(url)
    if not key or key == "/":
        return []
    with connect(path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, url, target_query, published_at FROM articles WHERE active=1")]
        hit = [r for r in rows if normalize_path(r.get("url")) == key]
        if not hit:
            return []
        conn.executemany("UPDATE articles SET published_at=? WHERE id=?",
                         [(published_at, r["id"]) for r in hit])
    return [{"id": r["id"], "url": r["url"], "target_query": r["target_query"],
             "было": r["published_at"], "стало": published_at} for r in hit]


def remove_article(article_id: int, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute("UPDATE articles SET active=0 WHERE id=?", (article_id,))


def save_article_check(article_id: int, query: str, position: int | None,
                       domain_position: int | None, ranking_url: str | None,
                       competition: float | None, is_target: bool,
                       run_id: int | None = None, snapshot_id: int | None = None,
                       path: Path | None = None) -> int:
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO article_checks (article_id, run_id, snapshot_id, checked_at, query, "
            "position, domain_position, ranking_url, competition, is_target) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (article_id, run_id, snapshot_id, now(), query, position, domain_position,
             ranking_url, competition, 1 if is_target else 0),
        )
        return int(cur.lastrowid)


def article_history(article_id: int, query: str | None = None, limit: int = 50,
                    path: Path | None = None) -> list[dict]:
    sql = "SELECT * FROM article_checks WHERE article_id=?"
    args: list = [article_id]
    if query:
        sql += " AND query=?"
        args.append(query)
    sql += " ORDER BY checked_at DESC LIMIT ?"
    args.append(limit)
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def articles_latest(project_id: int | None = None, path: Path | None = None) -> list[dict]:
    """
    Последний замер по каждой статье и её целевому запросу, с дельтой к предыдущему.
    Отдельно видно, вышла ли сама статья или вместо неё другая наша страница.

    `measurements` — вся история проверок, а не длина среза с `LIMIT 2`:
    поле читают как «сколько проверок накоплено». Срез из двух нужен только
    для дельты.
    """
    with connect(path) as conn:
        накоплено = {
            (r["article_id"], r["query"]): r["n"]
            for r in conn.execute(
                "SELECT article_id, query, COUNT(*) AS n FROM article_checks "
                "GROUP BY article_id, query"
            )
        }
    out = []
    for art in list_articles(project_id, path=path):
        rows = article_history(art["id"], query=art["target_query"], limit=2, path=path)
        cur = rows[0] if rows else None
        prev = rows[1] if len(rows) > 1 else None
        delta = None
        if cur and prev and cur["position"] is not None and prev["position"] is not None:
            delta = prev["position"] - cur["position"]
        out.append({
            **art,
            "position": cur["position"] if cur else None,
            "domain_position": cur["domain_position"] if cur else None,
            "ranking_url": cur["ranking_url"] if cur else None,
            "competition": cur["competition"] if cur else None,
            "is_target": bool(cur["is_target"]) if cur else None,
            "checked_at": cur["checked_at"] if cur else None,
            "previous": prev["position"] if prev else None,
            "delta": delta,
            # Вся история проверок по этой статье и её целевому запросу, а не
            # длина среза, из которого посчитана дельта.
            "measurements": накоплено.get((art["id"], art["target_query"]), len(rows)),
        })
    return out


# ──────────────────────────── прогоны ────────────────────────────

def start_run(kind: str, region: int = 225, note: str | None = None,
              path: Path | None = None) -> int:
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO runs (kind, region, started_at, note) VALUES (?,?,?,?)",
            (kind, region, now(), note),
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute("UPDATE runs SET finished_at=? WHERE id=?", (now(), run_id))


# ──────────────────────────── запись ────────────────────────────

def save_serp(result, run_id: int | None = None, path: Path | None = None) -> int:
    """Кладёт снимок выдачи и его документы. Возвращает snapshot_id."""
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO serp_snapshots (run_id, query, region, fetched_at, found_all, error) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, result.query, result.region, result.fetched_at, result.found_all, result.error),
        )
        snap_id = int(cur.lastrowid)
        if result.docs:
            conn.executemany(
                "INSERT OR REPLACE INTO serp_docs "
                "(snapshot_id, position, url, domain, title, title_hlwords, url_depth, "
                " domain_doccount, modtime) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (snap_id, d.position, d.url, d.domain, d.title, d.title_hlwords,
                     d.url_depth, d.domain_doccount, d.modtime)
                    for d in result.docs
                ],
            )
        return snap_id


def save_competition(comp, snapshot_id: int | None = None, run_id: int | None = None,
                     path: Path | None = None) -> int:
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO competition (run_id, snapshot_id, query, region, calculated_at, "
            "score, band, our_position, factors_json, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, snapshot_id, comp.query, comp.region, comp.calculated_at,
                comp.score, comp.band, comp.our_position,
                json.dumps([asdict(f) for f in comp.factors], ensure_ascii=False),
                comp.error,
            ),
        )
        return int(cur.lastrowid)


def save_frequency(freq, run_id: int | None = None, path: Path | None = None) -> int:
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO keyword_freq (run_id, phrase, region, freq, fetched_at, error) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, freq.phrase, str(freq.region), freq.freq, now(), freq.error),
        )
        return int(cur.lastrowid)


def record_positions(result, domains: list[str], snapshot_id: int | None = None,
                     run_id: int | None = None, path: Path | None = None,
                     samples: dict[str, list] | None = None,
                     override: dict[str, int | None] | None = None) -> int:
    """
    Фиксирует позицию каждого отслеживаемого домена в снятой выдаче.
    Домена нет в топе — пишется NULL, а не пропуск: отсутствие тоже факт.
    """
    if result.error:
        return 0

    # Позиция берётся органическая — без блоков Яндекса. Иначе картинки
    # и видео двигают нас вниз и замер расходится с тем, что видит человек.
    found: dict[str, tuple[int, str]] = {}
    for d in result.docs:
        if getattr(d, "is_wizard", False):
            continue
        pos = getattr(d, "organic_position", None) or d.position
        dom = (d.domain or "").lower().removeprefix("www.")
        found.setdefault(dom, (pos, d.url))

    rows = []
    for target in domains:
        t = target.lower().removeprefix("www.")
        pos, url = found.get(t, (None, None))
        # Медиана по нескольким снимкам сильнее одиночного значения из этого снимка.
        if override and t in override:
            pos = override[t]
        smp = json.dumps(samples.get(t), ensure_ascii=False) if samples and t in samples else None
        rows.append(
            (run_id, snapshot_id, result.query, result.region, t, pos, url, result.fetched_at, smp)
        )

    with connect(path) as conn:
        conn.executemany(
            "INSERT INTO positions (run_id, snapshot_id, query, region, domain, position, url, "
            "checked_at, samples) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


# ──────────────────────────── отслеживание ────────────────────────────

def track(query: str, region: int = 225, note: str | None = None, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO tracked_queries (query, region, added_at, active, note) VALUES (?,?,?,1,?) "
            "ON CONFLICT(query, region) DO UPDATE SET active=1",
            (query, region, now(), note),
        )


def untrack(query: str, region: int = 225, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute("UPDATE tracked_queries SET active=0 WHERE query=? AND region=?", (query, region))


def tracked(region: int | None = None, path: Path | None = None) -> list[str]:
    sql = "SELECT query FROM tracked_queries WHERE active=1"
    args: tuple = ()
    if region is not None:
        sql += " AND region=?"
        args = (region,)
    with connect(path) as conn:
        return [r["query"] for r in conn.execute(sql + " ORDER BY query", args)]


# ──────────────────────────── чтение ────────────────────────────

def position_history(query: str, domain: str, region: int = 225, limit: int = 50,
                     path: Path | None = None) -> list[dict]:
    dom = domain.lower().removeprefix("www.")
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT checked_at, position, url FROM positions "
            "WHERE query=? AND domain=? AND region=? ORDER BY checked_at DESC LIMIT ?",
            (query, dom, region, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_positions(domain: str, region: int = 225, path: Path | None = None,
                     queries: list[str] | None = None) -> list[dict]:
    """
    Текущая позиция по каждому запросу и дельта к предыдущему замеру.
    Дельта положительная — поднялись, номер стал меньше.

    `queries` — ограничить набором запросов. Нужен, чтобы карточка проекта
    считала «в топе N из M» по тем же запросам, что показывает в строке
    «запросов отслеживается». Без ограничения сюда попадали все запросы
    региона, и отчёт показывал бы «18 запросов, в топе 2 из 23»: два числа
    про разные совокупности рядом.
    """
    dom = domain.lower().removeprefix("www.")
    allowed = {q.strip().lower() for q in queries} if queries is not None else None
    with connect(path) as conn:
        queries = [
            r["query"]
            for r in conn.execute(
                "SELECT DISTINCT query FROM positions WHERE domain=? AND region=? ORDER BY query",
                (dom, region),
            )
            if allowed is None or r["query"].strip().lower() in allowed
        ]
        # `measurements` — сколько замеров накоплено за всю историю, а не
        # `len(rows)` от выборки с `LIMIT 2`: иначе поле никогда не показывало
        # бы больше двух. Проверки вызывающих («есть ли с чем сравнивать»,
        # «мерили ли вообще») на честном числе дают тот же ответ.
        #
        # Счёт идёт одним запросом на весь домен, а не по запросу на фразу: фраз
        # здесь три с лишним сотни, и `COUNT(*)` на каждую превратил бы чтение
        # отчёта в триста обращений к базе.
        накоплено = {
            r["query"]: r["n"]
            for r in conn.execute(
                "SELECT query, COUNT(*) AS n FROM positions "
                "WHERE domain=? AND region=? GROUP BY query",
                (dom, region),
            )
        }
        out = []
        for q in queries:
            rows = conn.execute(
                "SELECT checked_at, position, samples FROM positions "
                "WHERE query=? AND domain=? AND region=? ORDER BY checked_at DESC LIMIT 2",
                (q, dom, region),
            ).fetchall()
            if not rows:
                continue
            cur_pos = rows[0]["position"]
            prev_pos = rows[1]["position"] if len(rows) > 1 else None
            delta = None if (cur_pos is None or prev_pos is None) else prev_pos - cur_pos

            # Разброс замера: движение внутри него — шум, а не динамика.
            spread = None
            try:
                smp = json.loads(rows[0]["samples"] or "null")
                found = [v for v in (smp or []) if v is not None]
                if len(found) > 1 and min(found) != max(found):
                    spread = (min(found), max(found))
            except (json.JSONDecodeError, TypeError):
                pass

            # Нулевая дельта — это отсутствие движения, а не шум. Шумом называется
            # только движение, укладывающееся в разброс собственного замера.
            noise = bool(spread and delta not in (None, 0) and abs(delta) <= (spread[1] - spread[0]))

            out.append({
                "query": q,
                "position": cur_pos,
                "previous": prev_pos,
                "delta": delta,
                "spread": spread,
                "within_noise": noise,
                "checked_at": rows[0]["checked_at"],
                # Сколько замеров по этой фразе лежит в базе — вся история, а не
                # длина среза, из которого посчитана дельта (см. `накоплено`).
                "measurements": накоплено.get(q, len(rows)),
            })
    return out


def competition_history(query: str, region: int = 225, limit: int = 20,
                        path: Path | None = None) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT calculated_at, score, band, our_position FROM competition "
            "WHERE query=? AND region=? AND error IS NULL ORDER BY calculated_at DESC LIMIT ?",
            (query, region, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_competition(queries: list[str] | None = None, region: int = 225,
                       path: Path | None = None) -> dict[str, dict]:
    """
    Последний расчёт конкурентности по каждому запросу — одним обращением.

    Конкурентность лежит в двух местах: замороженная копия в строке ядра
    (`core_entries.competition`, снята при сборе) и живая история расчётов
    (`competition`). Если разные отчёты читают разные места, по одной фразе
    они расходятся. Одна метрика — одно место, и это место здесь.
    """
    sql = ("SELECT query, score, band, our_position, calculated_at FROM competition "
           "WHERE region=? AND error IS NULL ORDER BY calculated_at DESC")
    with connect(path) as conn:
        rows = conn.execute(sql, (region,)).fetchall()
    want = {q.strip().lower() for q in (queries or [])} or None
    out: dict[str, dict] = {}
    for r in rows:
        ключ = str(r["query"] or "").strip().lower()
        if not ключ or ключ in out:
            continue          # строки идут от свежих к старым: первая и есть последняя
        if want is not None and ключ not in want:
            continue
        out[ключ] = dict(r)
    return out


def top_domains(region: int = 225, since: str | None = None, limit: int = 25,
                path: Path | None = None) -> list[dict]:
    """Кто чаще всего попадается в накопленных снимках выдачи."""
    sql = (
        "SELECT d.domain, COUNT(*) AS hits, MIN(d.position) AS best, "
        "       ROUND(AVG(d.position),1) AS avg_pos, COUNT(DISTINCT s.query) AS queries "
        "FROM serp_docs d JOIN serp_snapshots s ON s.id = d.snapshot_id "
        "WHERE s.region=? AND s.error IS NULL"
    )
    args: list = [region]
    if since:
        sql += " AND s.fetched_at >= ?"
        args.append(since)
    sql += " GROUP BY d.domain ORDER BY hits DESC, avg_pos ASC LIMIT ?"
    args.append(limit)
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def domain_queries(domain: str, region: int = 225, limit: int = 100,
                   path: Path | None = None) -> list[dict]:
    """
    По каким запросам домен появлялся в накопленных снимках.
    Это и есть карта слов конкурента — собранная из нашей же истории выдачи.
    """
    dom = domain.lower().removeprefix("www.")
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT s.query, MIN(d.position) AS best_position, "
            "       MAX(s.fetched_at) AS checked_at, COUNT(*) AS hits "
            "FROM serp_docs d JOIN serp_snapshots s ON s.id = d.snapshot_id "
            "WHERE s.region=? AND s.error IS NULL "
            "  AND lower(replace(d.domain,'www.','')) = ? "
            "GROUP BY s.query ORDER BY best_position, s.query LIMIT ?",
            (region, dom, limit),
        ).fetchall()

        out = []
        for r in rows:
            last = conn.execute(
                "SELECT d.position FROM serp_docs d JOIN serp_snapshots s ON s.id = d.snapshot_id "
                "WHERE s.query=? AND s.region=? AND lower(replace(d.domain,'www.','')) = ? "
                "ORDER BY s.fetched_at DESC LIMIT 1",
                (r["query"], region, dom),
            ).fetchone()
            out.append({
                "query": r["query"],
                "best_position": r["best_position"],
                "last_position": last["position"] if last else None,
                "checked_at": r["checked_at"],
                "hits": r["hits"],
            })
    return out


def competitor_gap(our_domain: str, competitor: str, region: int = 225,
                   path: Path | None = None) -> list[dict]:
    """
    Запросы, где конкурент есть в снятом топе, с нашей позицией рядом.
    Строки с пустой нашей позицией — темы, которые мы упускаем целиком.
    """
    ours = our_domain.lower().removeprefix("www.")
    rows = domain_queries(competitor, region=region, limit=500, path=path)

    out = []
    with connect(path) as conn:
        for row in rows:
            mine = conn.execute(
                "SELECT d.position FROM serp_docs d JOIN serp_snapshots s ON s.id = d.snapshot_id "
                "WHERE s.query=? AND s.region=? AND lower(replace(d.domain,'www.','')) = ? "
                "ORDER BY s.fetched_at DESC LIMIT 1",
                (row["query"], region, ours),
            ).fetchone()
            out.append({
                "query": row["query"],
                "their_position": row["last_position"] or row["best_position"],
                "our_position": mine["position"] if mine else None,
            })

    # Сначала запросы, где нас нет вообще, потом по их позиции.
    return sorted(out, key=lambda r: (r["our_position"] is not None, r["their_position"] or 999))


def save_calibration(query: str, domain: str, seen_position: int | None,
                     api_position: int | None, api_samples: list | None = None,
                     region: int = 225, note: str | None = None,
                     path: Path | None = None) -> int:
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO calibration (query, region, domain, seen_position, api_position, "
            "api_samples, checked_at, note) VALUES (?,?,?,?,?,?,?,?)",
            (query, region, domain.lower().removeprefix("www."), seen_position, api_position,
             json.dumps(api_samples, ensure_ascii=False) if api_samples else None, now(), note),
        )
        return int(cur.lastrowid)


def calibration_report(domain: str | None = None, region: int = 225,
                       path: Path | None = None) -> dict:
    """
    Насколько замер расходится с тем, что видит человек.
    Пока сверок мало, доверять числу нельзя — это тоже сказано в ответе.
    """
    sql = "SELECT * FROM calibration WHERE region=?"
    args: list = [region]
    if domain:
        sql += " AND domain=?"
        args.append(domain.lower().removeprefix("www."))

    with connect(path) as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY checked_at DESC", args)]

    both = [r for r in rows if r["seen_position"] and r["api_position"]]
    diffs = [r["api_position"] - r["seen_position"] for r in both]
    avg = round(sum(diffs) / len(diffs), 1) if diffs else None

    return {
        "checks": len(rows),
        "comparable": len(both),
        "avg_offset": avg,          # плюс — замер показывает позицию хуже, чем видит человек
        "max_offset": max(diffs, key=abs) if diffs else None,
        "rows": rows,
        "trustworthy": len(both) >= 5,
    }


#: Порядок разбора находок: от того, что ломает индексацию, к косметике.
SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}


def save_audit(url: str, findings: list[dict], pages: int,
               project_id: int | None = None, started_at: str | None = None,
               error: str | None = None, path: Path | None = None) -> int:
    """
    Сохраняет прогон аудита с находками. Каждая находка приезжает со своим
    доказательством — процитированным тегом или числом; без него находку
    нельзя перепроверить, поэтому поле сохраняется как есть.
    """
    counts = {"critical": 0, "major": 0, "minor": 0}
    for f in findings:
        sev = str(f.get("severity") or "minor").lower()
        counts[sev] = counts.get(sev, 0) + 1

    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO audit_runs (project_id, url, started_at, finished_at, pages, "
            "findings, critical, major, minor, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, url, started_at or now(), now(), pages, len(findings),
             counts.get("critical", 0), counts.get("major", 0), counts.get("minor", 0), error),
        )
        run_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO audit_findings (run_id, severity, code, page_url, evidence, fix) "
            "VALUES (?,?,?,?,?,?)",
            [
                (run_id, str(f.get("severity") or "minor").lower(),
                 str(f.get("code") or "без кода"), f.get("url") or f.get("page_url"),
                 f.get("evidence"), f.get("fix"))
                for f in findings
            ],
        )
        return run_id


def list_audits(url: str | None = None, limit: int = 20,
                path: Path | None = None) -> list[dict]:
    sql = "SELECT * FROM audit_runs"
    args: list = []
    if url:
        sql += " WHERE url=?"
        args.append(url)
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def audit_findings(run_id: int, path: Path | None = None) -> list[dict]:
    with connect(path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_findings WHERE run_id=?", (run_id,)
        )]
    return sorted(
        rows,
        key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["code"], r["page_url"] or ""),
    )


def get_audit(run_id: int, path: Path | None = None) -> dict | None:
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM audit_runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


# ──────────────────────── первичка Яндекса ────────────────────────
# Вебмастер и Метрика — данные владельца сайта. Замер выдачи их не заменяет и
# не дублирует: он не видит показов и кликов вообще. Держим их отдельными
# таблицами, чтобы каждое число оставалось прослеживаемым до своей службы.

def set_project_yandex(project_id: int, webmaster_host: str | None = None,
                       metrika_counter: int | None = None,
                       path: Path | None = None) -> None:
    """Привязка проекта к хосту Вебмастера и счётчику Метрики. None не затирает."""
    with connect(path) as conn:
        if webmaster_host is not None:
            conn.execute("UPDATE projects SET webmaster_host=? WHERE id=?",
                         (webmaster_host, project_id))
        if metrika_counter is not None:
            conn.execute("UPDATE projects SET metrika_counter=? WHERE id=?",
                         (int(metrika_counter), project_id))


def save_search_queries(host_id: str, date_from: str, date_to: str, rows: list[dict],
                        project_id: int | None = None, path: Path | None = None) -> int:
    """
    Кладёт выгрузку Вебмастера за окно. Ключ — хост, запрос и окно.

    Повторная выгрузка того же окна обновляет свои строки; выгрузка другого
    окна ложится рядом. Пустое значение прохода не затирает измеренное
    (то же правило, что в `save_core_entries`).
    """
    stamp = now()
    payload = [
        (host_id, project_id, r["query"], r.get("query_id"), date_from, date_to,
         r.get("shows"), r.get("clicks"), r.get("avg_show_position"),
         r.get("avg_click_position"), stamp)
        for r in rows
    ]
    with connect(path) as conn:
        conn.executemany(
            "INSERT INTO search_queries (host_id, project_id, query, query_id, date_from, "
            "date_to, shows, clicks, avg_show_position, avg_click_position, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(host_id, query, date_from, date_to) DO UPDATE SET "
            "project_id=COALESCE(excluded.project_id, search_queries.project_id), "
            "query_id=COALESCE(excluded.query_id, search_queries.query_id), "
            "shows=COALESCE(excluded.shows, search_queries.shows), "
            "clicks=COALESCE(excluded.clicks, search_queries.clicks), "
            "avg_show_position=COALESCE(excluded.avg_show_position, "
            "  search_queries.avg_show_position), "
            "avg_click_position=COALESCE(excluded.avg_click_position, "
            "  search_queries.avg_click_position), "
            "fetched_at=excluded.fetched_at",
            payload,
        )
    return len(payload)


def _latest_window(conn, table: str, where: str, args: tuple) -> tuple[str, str] | None:
    """
    Последнее окно выгрузки — ОДНО окно, а не все строки с последним `date_to`.

    Окно — пара (`date_from`, `date_to`), и она входит в ключ таблицы. У
    Вебмастера конец окна не зависит от его длины (всегда «сегодня минус
    LAG_DAYS»), у Метрики окно кончается сегодня. Поэтому две выгрузки за один
    день с разным `--days` дают два окна с одним концом, и выбор по одному
    `MAX(date_to)` смешивал их: запрос печатался дважды, суммы складывали
    28 дней с 7.

    Правило выбора: из окон с самым поздним концом берётся то, которое
    выгружено последним (`MAX(fetched_at)`) — это данные, которые человек
    запросил последними. При равной отметке — более длинное окно.
    Две выгрузки разной длины — разные совокупности; складывать их нельзя.
    """
    row = conn.execute(
        f"SELECT date_from, date_to FROM {table} WHERE {where} "
        "GROUP BY date_from, date_to "
        "ORDER BY date_to DESC, MAX(fetched_at) DESC, date_from ASC LIMIT 1",
        args,
    ).fetchone()
    return (row["date_from"], row["date_to"]) if row else None


def search_window(host_id: str, path: Path | None = None) -> dict | None:
    """Последнее выгруженное окно по хосту: за какие даты и когда снято."""
    with connect(path) as conn:
        win = _latest_window(conn, "search_queries", "host_id=?", (host_id,))
        if not win:
            return None
        row = conn.execute(
            "SELECT date_from, date_to, MAX(fetched_at) AS fetched_at, COUNT(*) AS queries, "
            "SUM(shows) AS shows, SUM(clicks) AS clicks FROM search_queries "
            "WHERE host_id=? AND date_from=? AND date_to=?",
            (host_id, *win),
        ).fetchone()
    if not row or not row["date_to"]:
        return None
    return dict(row)


def search_queries_latest(host_id: str, limit: int | None = None,
                          path: Path | None = None) -> list[dict]:
    """Строки последнего выгруженного окна (`_latest_window`), от самых показываемых."""
    with connect(path) as conn:
        win = _latest_window(conn, "search_queries", "host_id=?", (host_id,))
    if not win:
        return []
    sql = (
        "SELECT * FROM search_queries WHERE host_id=? AND date_from=? AND date_to=? "
        "ORDER BY shows DESC, query"
    )
    args: list = [host_id, *win]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def search_queries_map(host_id: str, path: Path | None = None) -> dict[str, dict]:
    """
    Последнее окно, разложенное по тексту запроса в нижнем регистре.

    Сверка строгая, без схлопывания словоформ: «маркетинг застройщика» и
    «маркетинг для застройщика» — разные запросы с разным спросом, и склеить
    их значило бы приписать одной фразе показы другой.
    """
    return {r["query"].strip().lower(): r for r in search_queries_latest(host_id, path=path)}


def save_search_query_pages(host_id: str, date_from: str, date_to: str, rows: list[dict],
                            project_id: int | None = None, path: Path | None = None) -> int:
    """Какая страница собирает показы по запросу — выгрузка `query-analytics`."""
    stamp = now()
    payload = [
        (host_id, project_id, r["query"], r.get("url"), date_from, date_to,
         r.get("shows"), r.get("clicks"), r.get("avg_position"), r.get("demand"), stamp)
        for r in rows
    ]
    with connect(path) as conn:
        conn.executemany(
            "INSERT INTO search_query_pages (host_id, project_id, query, url, date_from, "
            "date_to, shows, clicks, avg_position, demand, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(host_id, query, date_from, date_to) DO UPDATE SET "
            "url=COALESCE(excluded.url, search_query_pages.url), "
            "shows=COALESCE(excluded.shows, search_query_pages.shows), "
            "clicks=COALESCE(excluded.clicks, search_query_pages.clicks), "
            "avg_position=COALESCE(excluded.avg_position, search_query_pages.avg_position), "
            "demand=COALESCE(excluded.demand, search_query_pages.demand), "
            "fetched_at=excluded.fetched_at",
            payload,
        )
    return len(payload)


def search_pages_map(host_id: str, path: Path | None = None) -> dict[str, dict]:
    """Последнее окно `query-analytics` (`_latest_window`), разложенное по тексту запроса."""
    with connect(path) as conn:
        win = _latest_window(conn, "search_query_pages", "host_id=?", (host_id,))
        if not win:
            return {}
        rows = conn.execute(
            "SELECT * FROM search_query_pages WHERE host_id=? AND date_from=? AND date_to=?",
            (host_id, *win),
        ).fetchall()
    return {r["query"].strip().lower(): dict(r) for r in rows}


def save_metrika_pages(counter: int, date_from: str, date_to: str, source: str,
                       rows: list[dict], project_id: int | None = None,
                       path: Path | None = None) -> int:
    """Поведение по посадочным страницам за окно. `source` входит в ключ."""
    stamp = now()
    payload = [
        (int(counter), project_id, r["url"], date_from, date_to, source,
         r.get("visits"), r.get("bounce_rate"), r.get("page_depth"),
         r.get("avg_duration"), stamp)
        for r in rows
    ]
    with connect(path) as conn:
        conn.executemany(
            "INSERT INTO metrika_pages (counter, project_id, url, date_from, date_to, source, "
            "visits, bounce_rate, page_depth, avg_duration, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(counter, url, date_from, date_to, source) DO UPDATE SET "
            "visits=COALESCE(excluded.visits, metrika_pages.visits), "
            "bounce_rate=COALESCE(excluded.bounce_rate, metrika_pages.bounce_rate), "
            "page_depth=COALESCE(excluded.page_depth, metrika_pages.page_depth), "
            "avg_duration=COALESCE(excluded.avg_duration, metrika_pages.avg_duration), "
            "fetched_at=excluded.fetched_at",
            payload,
        )
    return len(payload)


def metrika_pages_latest(counter: int, source: str = "organic",
                         path: Path | None = None) -> list[dict]:
    """Строки последнего окна Метрики (`_latest_window`), от самых посещаемых."""
    with connect(path) as conn:
        win = _latest_window(conn, "metrika_pages", "counter=? AND source=?",
                             (int(counter), source))
        if not win:
            return []
        rows = conn.execute(
            "SELECT * FROM metrika_pages WHERE counter=? AND source=? "
            "AND date_from=? AND date_to=? ORDER BY visits DESC",
            (int(counter), source, *win),
        ).fetchall()
    return [dict(r) for r in rows]


def metrika_pages_map(counter: int, source: str = "organic",
                      path: Path | None = None) -> dict[str, dict]:
    """Последнее окно Метрики по адресу страницы, приведённому к пути без хвостов."""
    out: dict[str, dict] = {}
    for row in metrika_pages_latest(counter, source, path=path):
        out[normalize_path(row["url"])] = row
    return out


def normalize_path(url: str | None) -> str:
    """
    Путь без схемы, домена, параметров и хвостового слэша.

    Нужен, потому что одну и ту же страницу три источника называют по-разному:
    Вебмастер отдаёт `/blog/x/`, Метрика — `https://example.ru/blog/x/`,
    в статьях лежит полный адрес. Сверять их построчно нельзя.
    """
    if not url:
        return ""
    text = str(url).strip()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            text = text.partition("/")[2]
            text = "/" + text
            break
    text = text.partition("?")[0].partition("#")[0]
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/").lower() or "/"


def stats(path: Path | None = None) -> dict:
    # Показы и клики — по последнему окну КАЖДОГО хоста (`_latest_window`):
    # общий `MAX(date_to)` по всем хостам терял хост с другим концом окна и
    # складывал два окна одного хоста с общим концом.
    with connect(path) as conn:
        hosts = [r["host_id"] for r in conn.execute("SELECT DISTINCT host_id FROM search_queries")]
    by_host = []
    for h in hosts:
        w = search_window(h, path=path) or {}
        by_host.append({"host_id": h, "shows": int(w.get("shows") or 0),
                        "clicks": int(w.get("clicks") or 0)})
    by_host.sort(key=lambda r: -r["shows"])
    with connect(path) as conn:
        def one(sql: str):
            row = conn.execute(sql).fetchone()
            return row[0] if row else 0

        return {
            "db": str(path or db_path()),
            "audit_runs": one("SELECT COUNT(*) FROM audit_runs"),
            "projects": one("SELECT COUNT(*) FROM projects WHERE active=1"),
            "articles": one("SELECT COUNT(*) FROM articles WHERE active=1"),
            "article_checks": one("SELECT COUNT(*) FROM article_checks"),
            "runs": one("SELECT COUNT(*) FROM runs"),
            "snapshots": one("SELECT COUNT(*) FROM serp_snapshots"),
            "docs": one("SELECT COUNT(*) FROM serp_docs"),
            "competition_rows": one("SELECT COUNT(*) FROM competition"),
            "frequency_rows": one("SELECT COUNT(*) FROM keyword_freq"),
            "position_rows": one("SELECT COUNT(*) FROM positions"),
            "tracked_queries": one("SELECT COUNT(*) FROM tracked_queries WHERE active=1"),
            "core_entries": one("SELECT COUNT(*) FROM core_entries"),
            "core_in_core": one("SELECT COUNT(*) FROM core_entries WHERE verdict='ядро'"),
            "webmaster_queries": one("SELECT COUNT(*) FROM search_queries"),
            # Сумма показов и кликов идёт по ВСЕМ хостам разом, и называется
            # тем, чем является; рядом разбор по хостам. Без этого сумма по
            # двум сайтам читалась бы как «показы проекта».
            "webmaster_shows_all_hosts": sum(h["shows"] for h in by_host),
            "webmaster_clicks_all_hosts": sum(h["clicks"] for h in by_host),
            "webmaster_by_host": by_host,
            "metrika_pages": one("SELECT COUNT(*) FROM metrika_pages"),
            "first_snapshot": one("SELECT MIN(fetched_at) FROM serp_snapshots"),
            "last_snapshot": one("SELECT MAX(fetched_at) FROM serp_snapshots"),
        }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Хранилище yaseo")
    p.add_argument("--init", action="store_true", help="создать схему")
    p.add_argument("--stats", action="store_true", help="показать накопленное")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--project-add", metavar="ИМЯ", dest="project_add",
                   help="завести проект (сайт); вместе с --domain")
    p.add_argument("--domain", help="домен проекта, напр. example.ru")
    p.add_argument("--region", type=int, default=225, help="регион Яндекса, 225 — Россия")
    p.add_argument("--projects", action="store_true", help="показать проекты")
    args = p.parse_args(argv)

    if args.project_add:
        if not args.domain:
            p.error("для --project-add нужен --domain")
        init_db(args.db)
        pid = add_project(args.project_add, args.domain, region=args.region, path=args.db)
        dom = (get_project(pid, args.db) or {}).get("domain", args.domain)
        print(f"проект {pid}: {args.project_add} ({dom}), регион {args.region}")
        return 0

    if args.projects:
        init_db(args.db)
        rows = list_projects(path=args.db)
        if not rows:
            print("Проектов нет. Завести: yaseo storage --project-add ИМЯ --domain example.ru")
        for r in rows:
            print(f"  {r['id']:>3}  {r['name']:<30} {r['domain']:<28} регион {r['region']}")
        return 0

    if args.init:
        print(f"схема создана: {init_db(args.db)}")
        return 0

    if args.stats:
        for k, v in stats(args.db).items():
            print(f"  {k:<26} {v}")
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
