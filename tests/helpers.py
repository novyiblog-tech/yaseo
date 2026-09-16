"""
Общие помощники тестов yaseo.

Ничего здесь не ходит в сеть и не трогает ключи или базу реальной машины:
`IsolatedTestCase` подменяет HOME/XDG_CONFIG_HOME/XDG_DATA_HOME/YASEO_DB и
текущий каталог на временные на время каждого теста, а `FakeOpener` подменяет
`urllib.request` там, где код пакета ходит к Яндексу.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from pathlib import Path

#: Ключи, которые могут указывать на реальные секреты или настройки машины.
#: Чистятся перед каждым тестом, чтобы совпадение с окружением того, кто
#: запускает тесты, не подменяло собой фикстуру.
ENV_KEYS_TO_CLEAR = (
    "YC_FOLDER_ID",
    "YANDEX_AI_STUDIO_API_KEY",
    "YANDEX_OAUTH_CLIENT_ID",
    "YANDEX_OAUTH_CLIENT_SECRET",
    "YANDEX_OAUTH_TOKEN",
    "YANDEX_OAUTH_REFRESH",
    "YASEO_ENV_FILE",
    "YASEO_DB",
    "YASEO_DOMAIN",
    "YASEO_USE_PROXY",
    "YASEO_TRACKING_FILE",
    "YASEO_TRACKING_MAX_CALLS",
    "YASEO_TRACKING_MIN_INTERVAL_DAYS",
    "YASEO_RATES",
    "YASEO_RATE_SEARCH_API",
    "YASEO_RATE_WORDSTAT",
    "YASEO_ALLOW_PRIVATE",
    "PERPLEXITY_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)


@contextlib.contextmanager
def isolated_environment(tmp_path: Path):
    """Временные HOME/XDG_*/YASEO_DB и рабочий каталог на время блока."""
    home = tmp_path / "home"
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    cwd = tmp_path / "cwd"
    for p in (home, config_home, data_home, cwd):
        p.mkdir(parents=True, exist_ok=True)

    old_environ = dict(os.environ)
    old_cwd = os.getcwd()
    try:
        for key in ENV_KEYS_TO_CLEAR:
            os.environ.pop(key, None)
        os.environ["HOME"] = str(home)
        os.environ["XDG_CONFIG_HOME"] = str(config_home)
        os.environ["XDG_DATA_HOME"] = str(data_home)
        os.environ["YASEO_DB"] = str(data_home / "yaseo.db")
        # Тесты поднимают сайты на 127.0.0.1, а внутренние адреса пакет
        # открывает только с разрешения. Разрешение ставится здесь явно;
        # тесты отказа (tests/test_net_safety.py) снимают его сами.
        os.environ["YASEO_ALLOW_PRIVATE"] = "1"
        os.chdir(cwd)
        yield {"home": home, "config_home": config_home, "data_home": data_home, "cwd": cwd}
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_environ)


class IsolatedTestCase(unittest.TestCase):
    """Базовый класс: каждый тест получает свежие HOME/XDG_*/YASEO_DB и cwd."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        cm = isolated_environment(self.tmp_path)
        self.paths = cm.__enter__()
        self.addCleanup(lambda: cm.__exit__(None, None, None))


class FakeResponse:
    """Похож на объект, который возвращает `OpenerDirector.open()`."""

    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self, *_args) -> bytes:
        return self._payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeOpener:
    """
    Подмена `urllib.request.OpenerDirector` для тестов без сети.

    `responses` — очередь ответов в порядке ожидаемых запросов; элемент-
    исключение поднимается вместо ответа (для проверки обработки ошибок).
    Каждый запрос запоминается в `requests` — по нему проверяется тело.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list = []

    def open(self, req, timeout=None):  # noqa: A002 — сигнатура как у настоящего opener
        self.requests.append(req)
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
