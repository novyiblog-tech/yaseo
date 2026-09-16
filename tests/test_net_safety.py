"""
Безопасность сети: ключ не уходит по перенаправлению, аудит и проверка
готовности не открывают file:// и внутренние адреса без разрешения,
карта сайта не распаковывается без предела и не читается с чужого домена.

Все серверы — локальные, на 127.0.0.1 / localhost.
"""
from __future__ import annotations

import gzip
import http.server
import os
import tempfile
import threading
import tracemalloc
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from tests import key_theft_stand as stand_mod
from tests.helpers import IsolatedTestCase
from yaseo import __version__, audit, net
from yaseo.geo import providers, readiness


# ─────────────────────────── утечка ключа ───────────────────────────


@unittest.skipUnless(stand_mod.openssl_available(), "нет openssl для тестового сертификата")
class KeyNotLeakedOnRedirectTests(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.stand = stand_mod.Stand().__enter__()
        self.addCleanup(self.stand.__exit__, None, None, None)
        # Opener держит TLS-контекст с момента создания: пересоздаём под стенд.
        net.keyed_opener.cache_clear()
        net.site_opener.cache_clear()
        self.addCleanup(net.keyed_opener.cache_clear)
        self.addCleanup(net.site_opener.cache_clear)

    def test_stand_positive_control(self) -> None:
        """Стандартный opener urllib идёт по 302 и отдаёт вору заголовок с ключом.
        Без этого молчание вора в следующем тесте ничего бы не доказывало."""
        plain = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            self.stand.base + "/v2/web/searchAsync", data=b"{}", method="POST",
            headers={"Authorization": "Api-Key " + stand_mod.SECRETS["YANDEX_AI_STUDIO_API_KEY"]})
        with plain.open(req, timeout=10) as r:
            r.read()
        self.assertIn("YANDEX_AI_STUDIO_API_KEY", self.stand.leaked())

    def test_thief_gets_nothing_from_any_client(self) -> None:
        outcomes = stand_mod.run_clients(self.stand)
        self.assertEqual(self.stand.leaked(), [], f"утечка: {self.stand.stolen}")
        self.assertEqual(self.stand.stolen, [], "вор не должен получить ни одного запроса")
        # Каждый клиент сказал человеческим текстом, что было перенаправление.
        for name, text in outcomes.items():
            with self.subTest(client=name):
                self.assertIn("перенаправлени", text, text)
        self.assertGreaterEqual(len(outcomes), 20)

    def test_keyed_opener_refuses_plain_http(self) -> None:
        req = urllib.request.Request("http://127.0.0.1:9/x", headers={"Authorization": "k"})
        with self.assertRaises(net.UnsafeURL) as ctx:
            net.keyed_opener(proxy=False).open(req, timeout=2)
        self.assertIn("только по https", str(ctx.exception.reason))


class OAuthHostTests(IsolatedTestCase):
    def test_token_is_not_sent_outside_yandex(self) -> None:
        from yaseo import yandex_auth
        opened = []
        with mock.patch.object(urllib.request.OpenerDirector, "open",
                               lambda *a, **k: opened.append(a)):
            with self.assertRaises(yandex_auth.YandexAuthError) as ctx:
                yandex_auth.api_json("https://evil.example/management", _tok="t")
        self.assertIn("не принадлежит Яндексу", str(ctx.exception))
        self.assertEqual(opened, [])


# ─────────────────────── адреса и схемы ───────────────────────


class PrivateAddressTests(unittest.TestCase):
    def test_private_networks(self) -> None:
        for host in ("localhost", "a.localhost", "127.0.0.1", "127.8.9.10", "10.1.2.3",
                     "172.16.0.1", "172.31.255.254", "192.168.1.1", "169.254.169.254",
                     "::1", "[::1]", "fc00::1", "fd12::5", "::ffff:127.0.0.1", "0.0.0.0"):
            with self.subTest(host=host):
                self.assertIsNotNone(net.private_address(host))

    def test_public_addresses(self) -> None:
        # 198.18.0.0/15 — диапазон fake-ip локальных прокси; внешние имена
        # под ним не должны считаться внутренними.
        for host in ("77.88.55.88", "8.8.8.8", "172.32.0.1", "198.18.0.5", "2a02:6b8::2:242"):
            with self.subTest(host=host):
                self.assertIsNone(net.private_address(host))

    def test_name_resolving_to_loopback_is_private(self) -> None:
        with mock.patch("socket.getaddrinfo",
                        return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
            self.assertEqual(net.private_address("rebind.example"), "127.0.0.1")

    def test_hanging_dns_refuses_instead_of_skipping(self) -> None:
        """Зависший резолвер: отказ за DNS_TIMEOUT, а не пропуск проверки."""
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(*_a, **_k):
            release.wait(5)
            return [(2, 1, 6, "", ("127.0.0.1", 0))]

        with mock.patch.object(net, "DNS_TIMEOUT", 0.2), \
                mock.patch("socket.getaddrinfo", side_effect=hang):
            with self.assertRaises(net.UnsafeURL) as ctx:
                net.check_url("http://slow.example/")
        self.assertIn("DNS не ответил", str(ctx.exception.reason))

    def test_dns_failure_still_left_to_request(self) -> None:
        import socket
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(8, "nodename")):
            self.assertIsNone(net.private_address("nope.example"))

    def test_check_url_texts(self) -> None:
        with self.assertRaises(net.UnsafeURL) as ctx:
            net.check_url("file:///etc/passwd")
        self.assertIn("только http и https", str(ctx.exception.reason))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(net.ALLOW_PRIVATE_ENV, None)
            with self.assertRaises(net.UnsafeURL) as ctx:
                net.check_url("http://127.0.0.1:8000/")
            self.assertIn("allow_private=True", str(ctx.exception.reason))
            net.check_url("http://127.0.0.1:8000/", allow_private=True)

    def test_user_agent_carries_package_version(self) -> None:
        self.assertIn(f"/{__version__}", providers.USER_AGENT)
        self.assertIn(f"/{__version__}", readiness.USER_AGENT)
        self.assertIn(f"/{__version__}", audit.USER_AGENT)
        self.assertNotIn("/0.1;", readiness.USER_AGENT)


class _Site:
    """Локальный сайт с заданными ответами: путь → (код, заголовки, тело)."""

    def __init__(self, routes: dict[str, tuple[int, dict, bytes]]) -> None:
        self.routes = routes
        self.hits: list[str] = []
        site = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                site.hits.append(self.path)
                code, headers, body = site.routes.get(self.path, (404, {}, b"nope"))
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v.replace("__PORT__", str(site.port)))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_HEAD = do_GET

            def log_message(self, *a) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.root = f"http://127.0.0.1:{self.port}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_Site":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


HTML = ("<!doctype html><html><head><title>Главная тестового сайта про окна</title>"
        "</head><body><h1>Окна</h1></body></html>").encode("utf-8")


class AuditSchemeAndPrivateTests(IsolatedTestCase):
    def test_file_scheme_refused_on_input(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".html") as f:
            res = audit.audit_site(f"file://localhost{f.name}", delay=0, timeout=2)
        self.assertIsNotNone(res.error)
        self.assertIn("только http и https", res.error)
        self.assertEqual(res.pages, [])

    def test_redirect_to_file_is_refused(self) -> None:
        routes = {"/go": (302, {"Location": "file:///etc/passwd"}, b"")}
        with _Site(routes) as site:
            got = audit._fetch(site.root + "go", timeout=5, allow_private=True)
        self.assertIsNone(got.status)
        self.assertIn("только http и https", got.error)
        self.assertEqual(got.chain, ["file:///etc/passwd"])

    def test_private_address_needs_explicit_flag(self) -> None:
        os.environ.pop(net.ALLOW_PRIVATE_ENV, None)
        with _Site({"/": (200, {"Content-Type": "text/html"}, HTML)}) as site:
            refused = audit.audit_site(site.root, delay=0, timeout=5, use_sitemap=False)
            self.assertEqual(site.hits, [], "без разрешения запрос не уходит")
            allowed = audit.audit_site(site.root, delay=0, timeout=5, use_sitemap=False,
                                       allow_private=True)
        self.assertIn("внутренний адрес", refused.error or "")
        self.assertIn("allow_private=True", refused.error or "")
        self.assertIsNone(allowed.error)
        self.assertEqual(allowed.pages_crawled, 1)

    def test_redirect_to_private_is_refused_without_flag(self) -> None:
        os.environ.pop(net.ALLOW_PRIVATE_ENV, None)
        routes = {"/": (200, {"Content-Type": "text/html"}, HTML)}
        public = [(2, 1, 6, "", ("77.88.55.88", 0))]
        with _Site(routes) as site, \
                mock.patch("socket.getaddrinfo", return_value=public), \
                mock.patch.object(audit, "_request",
                                  return_value=(302, {"Location": site.root}, b"", None)) as req:
            got = audit._fetch("https://public.example/", timeout=5)
        self.assertEqual(req.call_count, 1, "второй шаг цепочки не запрашивался")
        self.assertIn("внутренний адрес", got.error or "")
        self.assertEqual(site.hits, [])


class SitemapTests(IsolatedTestCase):
    def test_gzip_bomb_is_stopped(self) -> None:
        bomb = gzip.compress(b"\0" * (10 * 1024 * 1024))
        self.assertLess(len(bomb), 64 * 1024)
        tracemalloc.start()
        try:
            with self.assertRaises(audit.SitemapTooLarge) as ctx:
                audit._gunzip_limited(bomb, limit=1024 * 1024)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertIn("после распаковки больше 1 МБ", str(ctx.exception))
        self.assertLess(peak, 4 * 1024 * 1024, f"пик памяти {peak} байт")

    def test_normal_gzip_still_reads(self) -> None:
        data = b"<urlset>" + b"<url><loc>x</loc></url>" * 100 + b"</urlset>"
        self.assertEqual(audit._gunzip_limited(gzip.compress(data)), data)

    def test_bomb_in_audit_becomes_finding(self) -> None:
        bomb = gzip.compress(b"\0" * (10 * 1024 * 1024))
        routes = {
            "/": (200, {"Content-Type": "text/html"}, HTML),
            "/robots.txt": (200, {"Content-Type": "text/plain"},
                            b"User-agent: *\nAllow: /\nSitemap: http://127.0.0.1:__PORT__/sm.xml.gz\n"),
            "/sm.xml.gz": (200, {"Content-Type": "application/gzip"}, bomb),
        }
        # Порт в robots.txt подставляется после старта сервера.
        with _Site(routes) as site:
            body = routes["/robots.txt"][2].replace(b"__PORT__", str(site.port).encode())
            site.routes["/robots.txt"] = (200, {"Content-Type": "text/plain"}, body)
            with mock.patch.object(audit, "SITEMAP_UNPACKED_LIMIT", 1024 * 1024):
                res = audit.audit_site(site.root, delay=0, timeout=5, allow_private=True)
        self.assertIsNone(res.error)
        self.assertEqual(len(res.sitemap_errors), 1)
        self.assertIn("после распаковки больше 1 МБ", res.sitemap_errors[0])
        codes = {i.code for i in res.issues}
        self.assertIn("sitemap-too-large", codes)

    def test_foreign_sitemap_is_skipped_and_listed(self) -> None:
        with _Site({"/sitemap.xml": (200, {}, b"<urlset/>")}) as other:
            foreign = f"http://localhost:{other.port}/sitemap.xml"
            index = (f"<sitemapindex><sitemap><loc>{foreign.replace('sitemap', 'nested')}</loc>"
                     f"</sitemap></sitemapindex>").encode()
            routes = {
                "/": (200, {"Content-Type": "text/html"}, HTML),
                "/robots.txt": (200, {"Content-Type": "text/plain"},
                                f"Sitemap: {foreign}\nSitemap: http://127.0.0.1:__PORT__/idx.xml\n"
                                .encode()),
                "/idx.xml": (200, {"Content-Type": "application/xml"}, index),
            }
            with _Site(routes) as site:
                body = routes["/robots.txt"][2].replace(b"__PORT__", str(site.port).encode())
                site.routes["/robots.txt"] = (200, {"Content-Type": "text/plain"}, body)
                res = audit.audit_site(site.root, delay=0, timeout=5, allow_private=True)
                self.assertIn("/idx.xml", site.hits)
            self.assertEqual(other.hits, [], "чужая карта не запрашивалась")
        self.assertIn(foreign, res.sitemaps_skipped)
        self.assertEqual(len(res.sitemaps_skipped), 2, res.sitemaps_skipped)
        self.assertEqual(sum(i.code == "sitemap-foreign-host" for i in res.issues), 2)

    def test_same_registrable_domain_is_own(self) -> None:
        self.assertTrue(audit._own_domain("https://cdn.example.ru/s.xml", "https://example.ru/"))
        self.assertFalse(audit._own_domain("https://example.com/s.xml", "https://example.ru/"))
        self.assertFalse(audit._own_domain("file:///s.xml", "https://example.ru/"))

    def test_ipv6_host_is_own_and_not_empty(self) -> None:
        from yaseo.geo import domains
        self.assertEqual(domains.host_of("::1"), "::1")
        self.assertEqual(domains.host_of("[::1]"), "::1")
        self.assertEqual(domains.registrable("2a00:1450::1"), "2a00:1450::1")
        self.assertTrue(audit._own_domain("http://[::1]:8080/s.xml", "http://[::1]:8080/"))
        self.assertFalse(audit._own_domain("http://[::2]:8080/s.xml", "http://[::1]:8080/"))


class ReadinessSafetyTests(IsolatedTestCase):
    def test_file_scheme_refused(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            Path(f.name).write_text("секрет")
            got = readiness.fetch(f"file://{f.name}")
        self.assertEqual(got.status, 0)
        self.assertIn("только http и https", got.error)
        self.assertNotIn("секрет", got.text)

    def test_redirect_to_file_refused(self) -> None:
        with _Site({"/": (302, {"Location": "file:///etc/passwd"}, b"")}) as site:
            got = readiness.fetch(site.root, allow_private=True)
        self.assertEqual(got.status, 0)
        self.assertIn("только http и https", got.error)

    def test_site_opener_refuses_private(self) -> None:
        os.environ.pop(net.ALLOW_PRIVATE_ENV, None)
        with _Site({"/": (200, {}, b"ok")}) as target:
            with self.assertRaises(net.UnsafeURL):
                net.site_opener(proxy=False, follow=True, allow_private=False).open(
                    urllib.request.Request(target.root), timeout=5)
            self.assertEqual(target.hits, [])

    def test_check_refuses_private_without_flag(self) -> None:
        os.environ.pop(net.ALLOW_PRIVATE_ENV, None)
        with _Site({"/": (200, {"Content-Type": "text/html"}, HTML)}) as site:
            with self.assertRaises(ValueError) as ctx:
                readiness.check(site.root)
            self.assertEqual(site.hits, [])
            res = readiness.check(site.root, extra_pages=0, allow_private=True)
        self.assertIn("allow_private=True", str(ctx.exception))
        self.assertEqual(res.pages[0]["status"], 200)

    def test_foreign_markdown_alternate_not_fetched(self) -> None:
        with _Site({"/page.md": (200, {"Content-Type": "text/markdown"}, b"# md")}) as other:
            foreign = f"http://localhost:{other.port}/page.md"
            html = (f'<html><head><link rel="alternate" type="text/markdown" href="{foreign}">'
                    f"</head><body>x</body></html>").encode()
            with _Site({"/": (200, {"Content-Type": "text/html"}, html)}) as site:
                res = readiness.check(site.root, extra_pages=0, allow_private=True)
            self.assertEqual(other.hits, [])
        codes = [i.code for i in res.issues]
        self.assertIn("markdown-alternate-foreign", codes)
        self.assertNotIn("markdown-alternate-broken", codes)

    def test_own_markdown_alternate_still_fetched(self) -> None:
        html = (b'<html><head><link rel="alternate" type="text/markdown" href="/page.md">'
                b"</head><body>x</body></html>")
        routes = {"/": (200, {"Content-Type": "text/html"}, html),
                  "/page.md": (200, {"Content-Type": "text/markdown"}, b"# md")}
        with _Site(routes) as site:
            res = readiness.check(site.root, extra_pages=0, allow_private=True)
            self.assertIn("/page.md", site.hits)
        self.assertTrue(any("Markdown-версия" in g for g in res.good))


class ResolveRedirectTests(IsolatedTestCase):
    def test_file_and_private_not_opened(self) -> None:
        with _Site({"/": (302, {"Location": "https://example.ru/"}, b"")}) as site:
            self.assertIsNone(providers.resolve_redirect("file:///etc/passwd"))
            self.assertIsNone(providers.resolve_redirect(site.root))
            self.assertEqual(site.hits, [])

    def test_public_redirect_location_returned(self) -> None:
        opened = []

        def fake_open(self, req, *a, **kw):
            opened.append(urlsplit(req.full_url).hostname)
            import email.message
            import io
            import urllib.error
            h = email.message.Message()
            h["Location"] = "https://example.ru/page"
            raise urllib.error.HTTPError(req.full_url, 302, "Found", h, io.BytesIO(b""))

        with mock.patch.object(urllib.request.OpenerDirector, "open", fake_open), \
                mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("77.88.55.88", 0))]):
            loc = providers.resolve_redirect("https://vertexaisearch.cloud.google.com/grounding/x")
        self.assertEqual(loc, "https://example.ru/page")
        self.assertEqual(opened, ["vertexaisearch.cloud.google.com"])


class EmbeddedAddressTests(unittest.TestCase):
    """IPv4, зашитый в IPv6 (NAT64, 6to4, `::a.b.c.d`), решает сам за себя;
    служебные и групповые сети — внутренние."""

    CASES = {
        "::127.0.0.1": True,
        "64:ff9b::7f00:1": True,
        "64:ff9b::808:808": False,
        "2002:7f00:1::": True,
        "2002:808:808::": False,
        "ff02::1": True,
        "255.255.255.255": True,
        "224.0.0.1": True,
        "192.0.0.8": True,
        "::ffff:127.0.0.1": True,
        "2a00:1450:4010::64": False,
        "8.8.8.8": False,
        "198.18.0.5": False,
    }

    def test_classification(self) -> None:
        import ipaddress

        from yaseo.net import _ip_is_private

        for addr, want in self.CASES.items():
            with self.subTest(addr=addr):
                self.assertIs(_ip_is_private(ipaddress.ip_address(addr)), want)


if __name__ == "__main__":
    unittest.main()
