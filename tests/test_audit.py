"""
Тесты `yaseo.audit`: собственный краулер поднимается против настоящего
`http.server` на свободном порту localhost — это не «сеть» в смысле запрета
(наружу ничего не уходит), а единственный способ проверить HTTP-код,
robots.txt и sitemap.xml без сети.

Фикстура `tests/fixtures/site/` — сайт с девятью подложенными дефектами и
одной «хорошей» страницей; порт в её файлах шаблонный (`__PORT__`), потому
что порт выбирается заново при каждом запуске теста.
"""
from __future__ import annotations

import functools
import http.server
import tempfile
import threading
import unittest
from pathlib import Path

from yaseo import audit

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "site"

EXPECTED_CODES = {
    "missing-title",
    "missing-description",
    "duplicate-title",
    "duplicate-description",
    "broken-internal-link",
    "page-unavailable",
    "noindex-в-карте",
    "canonical-mismatch",
    "robots-disallow-в-карте",
}


class AuditSiteAgainstLocalFixtureTests(unittest.TestCase):
    """Один прогон обхода на весь класс — обход не бесплатен по времени."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        serve_dir = Path(cls._tmpdir.name)

        # Порт открываем ДО того, как пишем файлы фикстуры: так порт известен
        # заранее и гонки за него нет — сервер уже держит его занятым.
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(serve_dir))
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]

        for src in FIXTURE_DIR.rglob("*"):
            if src.is_dir():
                continue
            rel = src.relative_to(FIXTURE_DIR)
            dst = serve_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            text = src.read_text(encoding="utf-8").replace("__PORT__", str(cls.port))
            dst.write_text(text, encoding="utf-8")

        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.root = f"http://127.0.0.1:{cls.port}/"
        # Фикстура живёт на 127.0.0.1: внутренний адрес открывается только явно.
        cls.result = audit.audit_site(cls.root, max_pages=50, delay=0, timeout=5, use_sitemap=True,
                                      allow_private=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls._tmpdir.cleanup()

    def _codes(self) -> set[str]:
        return {i.code for i in self.result.issues}

    def test_crawl_did_not_fail_outright(self) -> None:
        self.assertIsNone(self.result.error)
        self.assertTrue(self.result.robots_txt)
        self.assertGreater(self.result.pages_crawled, 0)

    def test_all_nine_defect_codes_are_found(self) -> None:
        found = self._codes()
        missing = EXPECTED_CODES - found
        self.assertFalse(
            missing,
            f"не найдены коды: {sorted(missing)}; найдено всего: {sorted(found)}",
        )

    def test_each_expected_issue_carries_evidence(self) -> None:
        by_code = {}
        for i in self.result.issues:
            by_code.setdefault(i.code, []).append(i)
        for code in EXPECTED_CODES:
            issues = by_code.get(code, [])
            self.assertTrue(issues, f"код {code} не найден вовсе")
            for i in issues:
                self.assertTrue(i.evidence, f"{code}: доказательства нет")
                self.assertTrue(i.fix, f"{code}: нет 'что делать'")

    def test_good_page_has_no_false_canonical_findings(self) -> None:
        root_key = self.root.rstrip("/")
        bad = [
            i for i in self.result.issues
            if i.url.rstrip("/") == root_key and i.code in ("canonical-mismatch", "canonical-external")
        ]
        self.assertEqual(bad, [], f"ложные находки canonical на хорошей странице: {bad}")

    def test_good_page_has_title_and_description(self) -> None:
        root_key = self.root.rstrip("/")
        bad = [
            i for i in self.result.issues
            if i.url.rstrip("/") == root_key and i.code in ("missing-title", "missing-description")
        ]
        self.assertEqual(bad, [])

    def test_broken_internal_link_cites_the_referring_page(self) -> None:
        found = [i for i in self.result.issues if i.code == "broken-internal-link"]
        self.assertTrue(found)
        self.assertTrue(any("broken.html" in i.url for i in found))
        self.assertIn("ссылк", found[0].evidence.lower())

    def test_page_unavailable_is_the_unlinked_sitemap_entry(self) -> None:
        found = [i for i in self.result.issues if i.code == "page-unavailable"]
        self.assertTrue(any("gone-404.html" in i.url for i in found))

    def test_robots_disallow_and_sitemap_conflict_is_critical(self) -> None:
        found = [i for i in self.result.issues if i.code == "robots-disallow-в-карте"]
        self.assertTrue(found)
        self.assertEqual(found[0].severity, "critical")
        self.assertTrue(any("two-h1.html" in i.url for i in found))

    def test_noindex_and_sitemap_conflict_is_critical(self) -> None:
        found = [i for i in self.result.issues if i.code == "noindex-в-карте"]
        self.assertTrue(found)
        self.assertEqual(found[0].severity, "critical")


class NormalizeAndDedupTests(unittest.TestCase):
    """Мелкие чистые функции модуля — без сети вовсе."""

    def test_normalize_url_lowercases_host_and_keeps_trailing_slash(self) -> None:
        self.assertEqual(
            audit.normalize_url("HTTPS://Example.RU/Path/"), "https://example.ru/Path/"
        )

    def test_dedup_key_strips_utm_params_and_trailing_slash(self) -> None:
        a = audit.dedup_key("https://example.ru/blog/x/")
        b = audit.dedup_key("https://example.ru/blog/x?utm_source=blog&utm_campaign=y")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
