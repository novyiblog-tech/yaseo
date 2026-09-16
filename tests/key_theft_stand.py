"""
Стенд «перенаправитель + вор» для проверки утечки ключа при 3xx.

Перенаправитель (https://127.0.0.1:P) на любой запрос отвечает 302 на вора
по другому имени и порту: https://localhost:Q/steal, а для путей /h/… —
на вора без шифрования http://localhost:R/steal. Воры записывают каждый
пришедший запрос с заголовками и телом.

`run_clients(stand)` прогоняет все клиенты пакета, которые ходят с ключом
или токеном, направив их на перенаправитель. Функция пишется так, чтобы
работать и с текущим кодом, и с копией кода до правки: на старом коде вор
получает ключи — это положительный контроль стенда.

Сертификат самоподписанный, создаётся `openssl` во временном каталоге;
доверие ему даётся через SSL_CERT_FILE на время прогона.
"""
from __future__ import annotations

import http.server
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest import mock

SECRETS = {
    "YANDEX_AI_STUDIO_API_KEY": "FAKE-YC-KEY-5f1e",
    "PERPLEXITY_API_KEY": "pplx-FAKE-77aa",
    "OPENAI_API_KEY": "sk-FAKE-OPENAI-31",
    "GEMINI_API_KEY": "FAKE-GEMINI-9c2d",
    "ANTHROPIC_API_KEY": "sk-ant-FAKE-44e0",
    "OAUTH": "y0_FAKE-OAUTH-TOKEN",
    "CLIENT_SECRET": "FAKE-CLIENT-SECRET-81",
}
ENV = {"YC_FOLDER_ID": "folder-test", **SECRETS}


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


class _Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # журнал в вывод тестов не нужен
        pass


def _redirector(https_thief: str, http_thief: str):
    class H(_Quiet):
        def _go(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            target = http_thief if self.path.startswith("/h/") else https_thief
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = do_HEAD = _go
    return H


def _thief(log: list):
    class H(_Quiet):
        def _take(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            log.append({"path": self.path, "headers": dict(self.headers.items()),
                        "body": body.decode("utf-8", "replace")})
            payload = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_HEAD = _take
    return H


class Stand:
    def __init__(self) -> None:
        self.stolen: list[dict] = []
        self._servers: list[http.server.ThreadingHTTPServer] = []
        self._threads: list[threading.Thread] = []
        self._tmp = tempfile.TemporaryDirectory()
        self._old_env: dict[str, str | None] = {}

    # ── подъём ──
    def _cert(self) -> tuple[str, str]:
        d = Path(self._tmp.name)
        cert, key = d / "cert.pem", d / "key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(cert), "-days", "1",
             "-subj", "/CN=localhost",
             "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
            check=True, capture_output=True,
        )
        return str(cert), str(key)

    def _serve(self, handler, host: str, tls: ssl.SSLContext | None) -> int:
        srv = http.server.ThreadingHTTPServer((host, 0), handler)
        if tls is not None:
            srv.socket = tls.wrap_socket(srv.socket, server_side=True)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self._servers.append(srv)
        self._threads.append(t)
        return srv.server_address[1]

    def __enter__(self) -> "Stand":
        cert, key = self._cert()
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        q = self._serve(_thief(self.stolen), "127.0.0.1", tls)
        r = self._serve(_thief(self.stolen), "127.0.0.1", None)
        p = self._serve(_redirector(f"https://localhost:{q}/steal",
                                    f"http://localhost:{r}/steal"), "127.0.0.1", tls)
        self.base = f"https://127.0.0.1:{p}"
        # Доверие самоподписанному сертификату и прокси, который локальные
        # адреса обходит: зарубежные клиенты читают прокси из окружения.
        for k, v in (("SSL_CERT_FILE", cert), ("https_proxy", "http://127.0.0.1:9"),
                     ("http_proxy", "http://127.0.0.1:9"),
                     ("no_proxy", "127.0.0.1,localhost"), ("NO_PROXY", "127.0.0.1,localhost")):
            self._old_env[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc) -> None:
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
        for t in self._threads:
            t.join(timeout=5)
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    # ── вывод ──
    def leaked(self) -> list[str]:
        """Какие секреты вор увидел хоть где-то — в заголовках или в теле."""
        found = set()
        for req in self.stolen:
            blob = " ".join(f"{k}: {v}" for k, v in req["headers"].items()) + " " + req["body"]
            for name, value in SECRETS.items():
                if value in blob:
                    found.add(name)
        return sorted(found)


def run_clients(stand: Stand) -> dict[str, str]:
    """Прогоняет клиенты пакета через перенаправитель. Возвращает текст исхода по каждому."""
    from yaseo import wordstat_client, yandex_auth, yandex_serp
    from yaseo.geo import providers as P

    out: dict[str, str] = {}

    def attempt(name: str, fn) -> None:
        try:
            out[name] = f"вернул: {str(fn())[:300]}"
        except BaseException as e:  # noqa: BLE001 — нужен текст любого исхода
            out[name] = f"{type(e).__name__}: {getattr(e, 'reason', e)}"

    for prefix, tag in (("", "https"), ("/h", "http")):
        base = stand.base + prefix
        with mock.patch.object(yandex_serp, "SEARCH_ENDPOINT", f"{base}/v2/web/searchAsync"), \
                mock.patch.object(yandex_serp, "OPERATION_ENDPOINT", f"{base}/operations"):
            if hasattr(yandex_serp, "_opener"):
                yandex_serp._opener = None
            attempt(f"serp.search_async→{tag}", lambda: yandex_serp.search_async("q", env=ENV))
            attempt(f"serp.wait_operation→{tag}",
                    lambda: yandex_serp.wait_operation("op", env=ENV, timeout=0.3, poll=0.05))
        with mock.patch.object(wordstat_client, "ENDPOINT", f"{base}/v2/wordstat/topRequests"):
            if hasattr(wordstat_client, "_opener"):
                wordstat_client._opener = None
            attempt(f"wordstat→{tag}", lambda: wordstat_client.top_requests("q", env=ENV))

        for cls in (P.YandexGen, P.Perplexity, P.OpenAIWeb, P.AnthropicWeb):
            prov = cls()
            prov.endpoint = f"{base}/{prov.name}"
            attempt(f"geo.{prov.name}→{tag}", lambda prov=prov: prov.call("q", ENV))
        attempt(f"geo.gemini→{tag}", lambda: P._post_json(
            f"{base}/v1beta/models/m:generateContent", {},
            {"x-goog-api-key": ENV["GEMINI_API_KEY"]}, direct=False, timeout=10))

        host_ok = (mock.patch.object(yandex_auth, "_yandex_host", return_value=True)
                   if hasattr(yandex_auth, "_yandex_host") else mock.patch.dict({}, {}))
        with host_ok, mock.patch.object(yandex_auth, "refresh", return_value=ENV["OAUTH"]):
            attempt(f"oauth.api_json→{tag}",
                    lambda: yandex_auth.api_json(f"{base}/management/v1/counters",
                                                 _tok=ENV["OAUTH"]))
        with mock.patch.object(yandex_auth, "TOKEN_URL", f"{base}/token"):
            attempt(f"oauth.token→{tag}", lambda: yandex_auth._post_token(
                {"grant_type": "refresh_token", "client_secret": ENV["CLIENT_SECRET"]}))
    return out
