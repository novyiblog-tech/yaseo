"""
Журнал проверок видимости в ИИ-ответах: таблица ``geo_checks`` в базе yaseo.
"""
from __future__ import annotations

import json

from . import _core
from .providers import Answer

SCHEMA = """
CREATE TABLE IF NOT EXISTS geo_checks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    provider       TEXT NOT NULL,
    query          TEXT NOT NULL,
    domain         TEXT NOT NULL,
    cited          INTEGER NOT NULL DEFAULT 0,
    in_sources     INTEGER NOT NULL DEFAULT 0,
    mentioned      INTEGER NOT NULL DEFAULT 0,
    cited_url      TEXT,
    position       INTEGER,
    sources_count  INTEGER NOT NULL DEFAULT 0,
    used_count     INTEGER NOT NULL DEFAULT 0,
    rivals         TEXT,
    excerpt        TEXT,
    raw_json       TEXT,
    error          TEXT,
    checked_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS geo_checks_domain_idx ON geo_checks(domain, query, checked_at);
"""


def init() -> None:
    with _core.connect() as conn:
        conn.executescript(SCHEMA)


def save(a: Answer) -> int:
    init()
    rivals = json.dumps([d for d, _ in a.rivals(3)], ensure_ascii=False)
    with _core.connect() as conn:
        cur = conn.execute(
            "INSERT INTO geo_checks (provider, query, domain, cited, in_sources, mentioned,"
            " cited_url, position, sources_count, used_count, rivals, excerpt, raw_json,"
            " error, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a.provider, a.query, a.domain, int(a.cited), int(a.in_sources),
             int(a.mentioned_in_text), a.cited_url, a.cited_position, len(a.sources),
             a.used_count, rivals, a.text,
             json.dumps(a.raw, ensure_ascii=False) if a.raw is not None else None,
             a.error, a.checked_at))
        return int(cur.lastrowid)


def history(domain: str | None = None, query: str | None = None,
            provider: str | None = None, limit: int = 50) -> list[dict]:
    init()
    where, args = [], []
    if domain:
        where.append("domain = ?")
        args.append(domain)
    if query:
        where.append("query = ?")
        args.append(query)
    if provider:
        where.append("provider = ?")
        args.append(provider)
    sql = ("SELECT id, provider, query, domain, cited, in_sources, mentioned, cited_url,"
           " position, sources_count, used_count, rivals, error, checked_at FROM geo_checks")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY checked_at DESC, id DESC LIMIT ?"
    args.append(int(limit))
    with _core.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def last_error(provider: str) -> dict | None:
    init()
    with _core.connect() as conn:
        r = conn.execute(
            "SELECT error, checked_at FROM geo_checks WHERE provider = ?"
            " ORDER BY id DESC LIMIT 1", (provider,)).fetchone()
    if r is None or not r["error"]:
        return None
    return dict(r)
