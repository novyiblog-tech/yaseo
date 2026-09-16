#!/usr/bin/env python3
"""
Ключи и настройки из окружения. Одна точка на весь пакет.

Откуда берутся значения — первое найденное побеждает, по каждому ключу:

1. переменные окружения процесса;
2. файл из `$YASEO_ENV_FILE`;
3. `$XDG_CONFIG_HOME/yaseo/.env` (по умолчанию `~/.config/yaseo/.env`).

`./.env` текущего каталога в цепочке нет: MCP-сервер запускается из любого
проекта пользователя, и чужой `.env` (ключи другого проекта) молча
подхватывался бы под видом своих. Свой файл вне домашнего — только через
`YASEO_ENV_FILE`.

Ключи:
- `YC_FOLDER_ID`, `YANDEX_AI_STUDIO_API_KEY` — обязательны для Wordstat и выдачи;
- `YANDEX_OAUTH_CLIENT_ID`, `YANDEX_OAUTH_CLIENT_SECRET`, `YANDEX_OAUTH_TOKEN`,
  `YANDEX_OAUTH_REFRESH` — нужны только Вебмастеру и Метрике;
- ключи ИИ-провайдеров GEO/AEO (`PERPLEXITY_API_KEY` и другие, известные
  `yaseo.geo.providers`) — из окружения процесса читаются наравне с ключами
  Яндекса, список не дублируется здесь: см. `_geo_provider_keys()`;
- `YASEO_DB`, `YASEO_DOMAIN`, `YASEO_USE_PROXY` — необязательные настройки.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

#: Ключи, без которых не подписать обращение к Wordstat и Search API.
SEARCH_KEYS = ("YC_FOLDER_ID", "YANDEX_AI_STUDIO_API_KEY")
#: Ключи Яндекс ID — Вебмастер и Метрика.
OAUTH_KEYS = ("YANDEX_OAUTH_CLIENT_ID", "YANDEX_OAUTH_CLIENT_SECRET",
              "YANDEX_OAUTH_TOKEN", "YANDEX_OAUTH_REFRESH")

#: Совместимость: прежнее имя набора обязательных ключей.
REQUIRED = SEARCH_KEYS


def _geo_provider_keys() -> tuple[str, ...]:
    """
    Ключи ИИ-провайдеров GEO/AEO, если модуль `geo` установлен — иначе пусто.

    Список живёт у самих провайдеров (`geo/providers.py`, `required_env`
    каждого класса) — здесь он не переписывается второй раз, а просто
    запрашивается. Импорт внутри функции и лениво: `env` не обязан требовать
    модуль `geo`, а первый вызов случится не раньше, чем geo понадобится.
    """
    try:
        from .geo.providers import REGISTRY
    except Exception:  # noqa: BLE001 — модуль geo не обязателен
        return ()
    out: list[str] = []
    for p in REGISTRY.values():
        for k in p.required_env:
            if k not in out:
                out.append(k)
    return tuple(out)


WHERE_TO_GET = ("Ключ и каталог выдаёт Yandex AI Studio: https://aistudio.yandex.ru "
                "— сервисный аккаунт и API-ключ к нему.")

KEY_HINTS = {
    "YC_FOLDER_ID": "идентификатор каталога Яндекс Облака",
    "YANDEX_AI_STUDIO_API_KEY": "API-ключ сервисного аккаунта",
    "YANDEX_OAUTH_CLIENT_ID": "ClientID приложения на https://oauth.yandex.ru",
    "YANDEX_OAUTH_CLIENT_SECRET": "Client secret того же приложения",
    "YANDEX_OAUTH_TOKEN": "OAuth-токен: yaseo yandex --exchange <код>",
    "YANDEX_OAUTH_REFRESH": "refresh-токен, приходит вместе с OAuth-токеном",
}


class MissingKeysError(SystemExit):
    """
    Нужных ключей нет ни в окружении, ни в файлах.

    Наследник `SystemExit`: в терминале команда завершается одной понятной
    строкой без трассировки. MCP-сервер ловит его отдельно и отвечает
    текстом, а не падает.
    """

    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        super().__init__(missing_keys_message(self.missing))


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "yaseo"


def user_env_file() -> Path:
    """Файл ключей пользователя: сюда пишет `yaseo init`."""
    return config_home() / ".env"


def candidate_files() -> list[Path]:
    """
    Файлы с ключами в порядке старшинства (существующие и нет).

    `./.env` текущего каталога сюда намеренно не входит — сервер запускается
    из каталога чужого проекта пользователя, и его `.env` не наш файл.
    """
    out: list[Path] = []
    explicit = os.environ.get("YASEO_ENV_FILE", "").strip()
    if explicit:
        out.append(Path(explicit).expanduser())
    out.append(user_env_file())
    return out


def env_files() -> list[Path]:
    """Существующие файлы с ключами, от старшего к младшему."""
    seen: set[Path] = set()
    out: list[Path] = []
    for p in candidate_files():
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp in seen or not p.is_file():
            continue
        seen.add(rp)
        out.append(p)
    return out


def primary_env_file() -> Path | None:
    """Старший из существующих файлов — туда пишутся обновлённые токены."""
    files = env_files()
    return files[0] if files else None


def parse_env_file(path: Path) -> dict[str, str]:
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


def read_all() -> tuple[dict[str, str], dict[str, str]]:
    """
    Все значения и откуда пришло каждое: ({ключ: значение}, {ключ: источник}).

    Файлы читаются каждый раз заново: после продления токена кэш врал бы.
    """
    values: dict[str, str] = {}
    source: dict[str, str] = {}
    for path in reversed(env_files()):          # младший первым, старший перекрывает
        for k, v in parse_env_file(path).items():
            if v:
                values[k] = v
                source[k] = str(path)
    known = SEARCH_KEYS + OAUTH_KEYS + _geo_provider_keys()
    for k, v in os.environ.items():
        if v and (k in known or k.startswith("YASEO_")):
            values[k] = v
            source[k] = "окружение процесса"
    return values, source


def sources_for(keys) -> dict[str, str | None]:
    """
    Источник каждого ключа из `keys`: путь к файлу, «окружение процесса»
    или `None`, если ключа нет нигде. Для показа в `whoami`/`geo_providers` —
    ничего не бросает и не требует, чтобы ключ был обязательным.
    """
    _, source = read_all()
    return {k: source.get(k) for k in keys}


def missing_keys_message(missing: list[str]) -> str:
    names = ", ".join(missing)
    lines = [f"Не заданы ключи: {names}."]
    for k in missing:
        if k in KEY_HINTS:
            lines.append(f"  {k} — {KEY_HINTS[k]}")
    if any(k in SEARCH_KEYS for k in missing):
        lines.append(WHERE_TO_GET)
    lines.append(
        f"Куда положить: выполните `yaseo init` — он запишет {user_env_file()}; "
        f"или задайте переменные окружения, или файл через YASEO_ENV_FILE.")
    return "\n".join(lines)


def load_env(required: tuple[str, ...] | list[str] | None = SEARCH_KEYS) -> dict[str, str]:
    """
    Значения ключей. `required` — какие обязательны; пустой набор — без проверки.

    Отсутствие ключа — `MissingKeysError` с именем ключа, ссылкой, где его
    взять, и местом, куда его положить: отказ без имени ключа чинят перебором.
    """
    values, _ = read_all()
    missing = [k for k in (required or ()) if not values.get(k)]
    if missing:
        raise MissingKeysError(missing)
    return values


def describe() -> dict:
    """Откуда что взято — без самих секретов. Для `whoami` и `yaseo init`."""
    values, source = read_all()
    keys = {}
    for k in SEARCH_KEYS + OAUTH_KEYS:
        keys[k] = {"задан": bool(values.get(k)), "источник": source.get(k)}
    return {
        "файлы_по_старшинству": [str(p) for p in candidate_files()],
        "найденные_файлы": [str(p) for p in env_files()],
        "ключи": keys,
        "не_хватает_для_выдачи": [k for k in SEARCH_KEYS if not values.get(k)],
    }


def use_proxy() -> bool:
    """
    Ходить ли к Яндексу через системный прокси.

    По умолчанию нет: `urllib` молча подхватывает HTTPS_PROXY, и если прокси
    выводит трафик за пределы России, Яндекс может рвать соединение на
    рукопожатии (`SSL: UNEXPECTED_EOF_WHILE_READING`). Включить: YASEO_USE_PROXY=1.
    """
    return os.environ.get("YASEO_USE_PROXY", "").strip().lower() in ("1", "true", "yes")


def atomic_write_text(path: Path, text: str) -> None:
    """
    Атомарная запись текстового файла: временный файл рядом, fsync, `os.replace`.

    Файл с ключами обнулять и потом писать заново нельзя: продление токена
    (`yandex_auth.refresh`) может упасть между этими двумя шагами и оставить
    файл пустым, а параллельный MCP-сервер, читающий его в этот момент,
    увидит ноль ключей вместо старых. `os.replace` внутри одной файловой
    системы — атомарная операция ОС: снаружи виден либо старый файл целиком,
    либо новый, огрызка не бывает. Временный файл лежит в том же каталоге,
    что и цель, — на других файловых системах `os.replace` не гарантирован.

    Права: файл, которого не было, создаётся сразу с 0600 — ключи слишком
    чувствительны для умолчаний ОС (обычно 0644). У существующего файла права
    владельца сохраняются, а права группы и остальных снимаются: в файл
    сейчас ляжет секрет, и оставить его читаемым всем нельзя.

    Если путь — символическая ссылка, пишется файл, на который она ведёт:
    `os.replace` по самой ссылке заменил бы её обычным файлом, и настоящий
    файл с ключами молча остался бы старым.
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_mode: int | None = None
    if path.exists():
        try:
            existing_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            existing_mode = None

    tmp_path = path  # переопределится ниже; объявлено для видимости в except
    n = 0
    while True:
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{n}.tmp")
        try:
            fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            n += 1
            if n > 1000:  # практически недостижимо — защита от бага, не от жизни
                raise

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if existing_mode is not None:
            os.chmod(tmp_path, existing_mode & 0o700)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_env_value(key: str, value: str) -> Path:
    """
    Кладёт или обновляет один ключ, не трогая остальной файл.

    Пишет в старший найденный файл; если ключи пришли только из окружения —
    в `~/.config/yaseo/.env`. Запись идёт через `atomic_write_text`: падение
    между временным файлом и заменой не портит существующий файл ключей,
    и параллельный читатель (MCP-сервер) не увидит его пустым в процессе записи.
    """
    target = primary_env_file()
    if target is None:
        target = user_env_file()

    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    for i, raw in enumerate(lines):
        if raw.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")

    atomic_write_text(target, "\n".join(lines) + "\n")
    return target
