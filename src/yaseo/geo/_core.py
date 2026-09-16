"""
Точки опоры на ядро yaseo: ключи и база.

Договорённый интерфейс ядра:
- ``yaseo.env.load_env(required: tuple[str, ...] = ()) -> dict[str, str]``;
- ``yaseo.storage.connect()`` — соединение с базой из ``$YASEO_DB``.

TODO(ядро): пока ядро пишется параллельно, здесь стоит прокладка с тем же
интерфейсом. Как только ``yaseo.env.load_env`` примет ``required``, а
``yaseo.storage.connect`` будет смотреть в ``$YASEO_DB``, прокладка сама
уступит место ядру (проверка ниже), и её можно будет удалить.
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path


class КлючейНет(RuntimeError):
    """Не хватает ключей провайдера. Текст говорит, каких именно."""


# ────────────────────────────── ключи ──────────────────────────────

def _config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "yaseo"


def _env_files() -> list[Path]:
    """
    Файлы с ключами, от старшего к младшему. Без ``./.env`` текущего каталога:
    сервер запускается из каталога чужого проекта пользователя, и его
    ``.env`` (ключи другого проекта) — не наш файл. Совпадает с цепочкой
    ``yaseo.env`` (см. её докстринг) — эта прокладка используется, только
    пока ядро недоступно.
    """
    files = []
    if os.environ.get("YASEO_ENV_FILE"):
        files.append(Path(os.environ["YASEO_ENV_FILE"]).expanduser())
    files.append(_config_home() / ".env")
    return files


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _load_env_local(required: tuple[str, ...] = ()) -> dict[str, str]:
    """Цепочка: окружение → $YASEO_ENV_FILE → $XDG_CONFIG_HOME/yaseo/.env.
    Ранний источник побеждает поздний."""
    env: dict[str, str] = {}
    for f in reversed(_env_files()):
        env.update({k: v for k, v in _read_env_file(f).items() if v})
    env.update({k: v for k, v in os.environ.items() if v})
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise КлючейНет(
            "не заданы ключи: " + ", ".join(missing)
            + ". Впишите их в окружение или в файл ключей "
            "(порядок поиска: переменные окружения, $YASEO_ENV_FILE, "
            f"{_config_home() / '.env'}).")
    return env


def _core_load_env():
    try:
        from yaseo.env import load_env as core
    except Exception:  # noqa: BLE001 — ядра ещё нет
        return None
    try:
        params = inspect.signature(core).parameters
    except (TypeError, ValueError):
        return None
    return core if "required" in params else None


def load_env(required: tuple[str, ...] = ()) -> dict[str, str]:
    core = _core_load_env()
    if core is None:
        return _load_env_local(required)
    try:
        return dict(core(required=tuple(required)))
    except BaseException as e:  # noqa: BLE001 — ядро может бросать SystemExit
        if isinstance(e, KeyboardInterrupt):
            raise
        raise КлючейНет(str(e)) from None


def key_sources(keys: tuple[str, ...]) -> dict[str, str | None]:
    """
    Источник каждого ключа из ``keys``: путь к файлу, «окружение процесса»
    или ``None``. Не бросает — для показа в ``geo_providers``/``whoami``,
    рядом с ``configured()``.
    """
    try:
        from yaseo.env import sources_for as core
    except Exception:  # noqa: BLE001 — ядра ещё нет
        core = None
    if core is not None:
        try:
            return dict(core(keys))
        except Exception:  # noqa: BLE001 — показ статуса не должен падать
            pass
    files_values: dict[str, str] = {}
    for f in reversed(_env_files()):
        files_values.update(_read_env_file(f))
    out: dict[str, str | None] = {}
    for k in keys:
        if os.environ.get(k):
            out[k] = "окружение процесса"
        elif files_values.get(k):
            out[k] = "файл"
        else:
            out[k] = None
    return out


def use_proxy() -> bool:
    """Ходить ли к Яндексу через системный прокси. По умолчанию — напрямую."""
    return os.environ.get("YASEO_USE_PROXY", "").strip().lower() in ("1", "true", "yes")


def configured(required: tuple[str, ...]) -> tuple[bool, list[str]]:
    """Настроен ли провайдер: (да/нет, каких ключей нет). Ничего не бросает."""
    try:
        env = load_env(())
    except КлючейНет:
        env = dict(os.environ)
    missing = [k for k in required if not env.get(k)]
    return (not missing, missing)


# ────────────────────────────── база ──────────────────────────────

def _local_db_path() -> Path:
    if os.environ.get("YASEO_DB"):
        return Path(os.environ["YASEO_DB"]).expanduser()
    return Path.home() / ".local" / "share" / "yaseo" / "yaseo.db"


def _db_file(conn: sqlite3.Connection) -> str:
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                return row[2] or ""
    except sqlite3.Error:
        pass
    return ""


@contextmanager
def _local_connect():
    p = _local_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def connect():
    """Соединение с базой yaseo. Берёт ``yaseo.storage.connect()``, если ядро
    уже смотрит в ``$YASEO_DB``; иначе — своё соединение с тем же файлом."""
    try:
        from yaseo import storage as core
        got = core.connect()
    except Exception:  # noqa: BLE001 — ядра ещё нет или оно другое
        got = None

    if got is None:
        with _local_connect() as conn:
            yield conn
        return

    if isinstance(got, sqlite3.Connection):
        conn, ctx = got, None
    else:  # контекст-менеджер
        ctx = got
        conn = ctx.__enter__()

    want = os.environ.get("YASEO_DB")
    if want and os.path.realpath(_db_file(conn)) != os.path.realpath(
            str(Path(want).expanduser())):
        # TODO(ядро): ядро ещё не смотрит в $YASEO_DB — не пишем в чужую базу.
        if ctx is not None:
            ctx.__exit__(None, None, None)
        else:
            conn.close()
        with _local_connect() as local:
            yield local
        return

    conn.row_factory = sqlite3.Row
    if ctx is None:
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
        return

    try:
        yield conn
    except BaseException:
        if not ctx.__exit__(*sys.exc_info()):
            raise
    else:
        ctx.__exit__(None, None, None)
