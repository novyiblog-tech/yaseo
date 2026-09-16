#!/usr/bin/env python3
"""
Пути и настройки yaseo.

Каталог данных: `$XDG_DATA_HOME/yaseo` (по умолчанию `~/.local/share/yaseo`).
База: `$YASEO_DB` или `<каталог данных>/yaseo.db`.
Настройки трекера: `$YASEO_TRACKING_FILE` или `~/.config/yaseo/tracking.json`,
поверх — переменные окружения.

Домен по умолчанию (`default_domain`): аргумент → `YASEO_DOMAIN` → домен
единственного активного проекта в базе → понятная ошибка.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .env import config_home, read_all


class DomainNotSetError(ValueError):
    """Домен не передан и не выводится однозначно."""


def _setting(name: str) -> str:
    """Настройка YASEO_*: окружение процесса, потом файлы .env."""
    raw = os.environ.get(name)
    if raw:
        return raw.strip()
    values, _ = read_all()
    return (values.get(name) or "").strip()


# ──────────────────────────── пути ────────────────────────────

def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "yaseo"


def db_path() -> Path:
    raw = _setting("YASEO_DB")
    return Path(raw).expanduser() if raw else data_dir() / "yaseo.db"


def snapshots_dir() -> Path:
    """Снапшоты Wordstat (JSONL): одна строка — один ответ GetTop."""
    return data_dir() / "snapshots"


def reports_dir() -> Path:
    """Датированные отчёты ядра и брифов."""
    return data_dir() / "reports"


def tracking_file() -> Path:
    raw = _setting("YASEO_TRACKING_FILE")
    return Path(raw).expanduser() if raw else config_home() / "tracking.json"


# ──────────────────────────── домен ────────────────────────────

def normalize_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    for prefix in ("https://", "http://"):
        d = d.removeprefix(prefix)
    d = d.removeprefix("www.").split("/")[0]
    return d


def default_domain(domain: str | None = None) -> str:
    """
    Домен, по которому мерить «нашу» позицию.

    Порядок: аргумент → `YASEO_DOMAIN` → домен единственного активного проекта.
    Угадывать между несколькими проектами нельзя: позиция чужого сайта,
    записанная как своя, хуже отсутствия позиции.
    """
    if domain and domain.strip():
        return normalize_domain(domain)
    env = _setting("YASEO_DOMAIN")
    if env:
        return normalize_domain(env)
    try:
        from . import storage
        storage.init_db()
        projects = storage.list_projects(active_only=True)
    except Exception:  # noqa: BLE001 — база недоступна: домен не выводится
        projects = []
    domains = sorted({normalize_domain(p["domain"]) for p in projects if p.get("domain")})
    if len(domains) == 1:
        return domains[0]
    if len(domains) > 1:
        raise DomainNotSetError(
            "укажите domain: в базе несколько проектов (" + ", ".join(domains) + "), "
            "и какой из них «наш», не выводится. Передайте domain или задайте YASEO_DOMAIN")
    raise DomainNotSetError(
        "укажите domain (например, example.ru) или задайте переменную YASEO_DOMAIN")


# ──────────────────────────── трекер ────────────────────────────

#: Умолчания автозамера позиций. Каждое можно перекрыть файлом настроек,
#: а потолок и интервал — ещё и переменной окружения.
TRACKING_DEFAULTS: dict = {
    # Сколько обращений к Search API разрешено за один прогон. Прогон, который
    # собирается сделать больше, останавливается до первого запроса: пул фраз
    # растёт незаметно, и расписание не должно тратить в разы больше молча.
    "max_calls": 1000,
    # Сколько суток между платными прогонами по одному домену. За несколько
    # дней движение позиций обычно неотличимо от разброса внутри одного замера,
    # поэтому частый замер платит за шум. 13, а не 14: недельное расписание
    # через две недели попадает на границу и пропускалось бы из-за минут.
    "min_interval_days": 13,
    # Снимков выдачи на фразу; в историю идёт медиана.
    "repeats": 3,
    # Фраза была в снятом топе (или замеров ещё не было) — медиану есть из чего брать.
    "repeats_hot": 3,
    # Фразы не было в топе — медиана из трёх «нет в топе» та же пустота.
    "repeats_cold": 1,
    # Соперники, чьи позиции снимаются из той же выдачи: {наш домен: [домены]}.
    "competitors": {},
    # Чьими считать фразы, заведённые без проекта: id проекта или None (ничьи).
    "orphan_project": None,
    # Потолок обращений к Search API за один прогон `articles.check_all`.
    # Тот же принцип, что у `max_calls` трекера: число статей и дополнительных
    # запросов к ним не ограничено ничем, кроме этого потолка, и без него
    # прогон может незаметно вырасти и потратить в разы больше молча.
    "articles_max_calls": 100,
}


def tracking_settings() -> dict:
    """
    Настройки трекера: умолчания, поверх — файл. Битый файл не молчит:
    об этом говорится в stderr, и работа идёт по умолчаниям.
    """
    out = dict(TRACKING_DEFAULTS)
    path = tracking_file()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out.update({k: v for k, v in data.items() if k in TRACKING_DEFAULTS})
        except (OSError, ValueError) as exc:
            print(f"настройки трекера не прочитаны ({path}: {exc}) — работаю по умолчанию",
                  file=sys.stderr)
    return out


def env_int(name: str) -> int | None:
    raw = _setting(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"{name}={raw!r} — не число, беру из настроек", file=sys.stderr)
        return None


def domain_or_none(domain: str | None = None) -> str | None:
    """Как `default_domain`, но без ошибки: там, где «наш» домен необязателен."""
    try:
        return default_domain(domain)
    except DomainNotSetError:
        return None
