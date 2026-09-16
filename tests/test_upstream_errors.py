"""
Отказ источника и «последнее окно» выгрузок.

1. Если инструмент целиком не получил данных от Яндекса или ИИ-провайдера,
   MCP-ответ несёт `isError: true` и человеческую причину. Проверяется
   подменой `OpenerDirector.open`: любой запрос получает HTTP 401, как при
   неверном ключе. Частичный отказ ошибкой не считается, упавшие перечислены.
2. Две выгрузки Вебмастера или Метрики за один день с разной длиной окна
   дают окна с общим концом. «Последнее окно» — одно из них, а не оба сразу.
"""
from __future__ import annotations

import email.message
import io
import os
import urllib.error
import urllib.request
from unittest import mock

from tests.helpers import IsolatedTestCase
from yaseo import mcp_server, storage
from yaseo.yandex_serp import SerpDoc, SerpResult


def _call(name: str, arguments: dict | None = None) -> dict:
    return mcp_server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })["result"]


class _Deny401:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, opener, req, *a, **kw):
        self.calls += 1
        url = req if isinstance(req, str) else req.full_url
        return_body = io.BytesIO(b'{"code": 16, "message": "Unknown api key"}')
        raise urllib.error.HTTPError(url, 401, "Unauthorized", email.message.Message(), return_body)


class UpstreamRefusalTests(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["YC_FOLDER_ID"] = "folder-test"
        os.environ["YANDEX_AI_STUDIO_API_KEY"] = "bad"
        self.deny = _Deny401()
        deny = self.deny

        def open_(opener, req, *a, **kw):  # функция, чтобы привязаться как метод
            return deny(opener, req, *a, **kw)

        patch = mock.patch.object(urllib.request.OpenerDirector, "open", open_)
        patch.start()
        self.addCleanup(patch.stop)
        # Паузы между запросами пакета тестам не нужны.
        sleep = mock.patch("time.sleep", lambda *_: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def assertRefused(self, name: str, arguments: dict) -> str:
        res = _call(name, arguments)
        text = res["content"][0]["text"]
        with self.subTest(tool=name):
            self.assertTrue(res.get("isError"), f"{name} вернул отказ без isError: {text[:200]}")
            self.assertIn("401", text)
            self.assertNotIn("Traceback", text)
        return text

    def test_positive_control_requests_really_hit_the_stub(self) -> None:
        self.assertRefused("get_serp_results", {"query": "ремонт"})
        self.assertGreater(self.deny.calls, 0)

    def test_search_and_wordstat_tools_mark_total_refusal(self) -> None:
        cases = {
            "get_serp_results": {"query": "ремонт"},
            "research_keywords": {"seeds": ["ремонт"]},
            "get_keyword_metrics": {"keywords": ["ремонт", "отделка"]},
            "find_serp_competitors": {"queries": ["ремонт", "отделка"]},
            "get_competition": {"queries": ["ремонт"]},
            "build_brief": {"query": "ремонт"},
            "expand_query_pool": {"seed": "ремонт"},
        }
        for name, args in cases.items():
            self.assertRefused(name, args)

    def test_run_tracking_marks_total_refusal(self) -> None:
        storage.init_db()
        storage.track("ремонт", 225)
        text = self.assertRefused("run_tracking", {"domain": "example.ru", "now": True})
        self.assertIn("не снят ни один", text)

    def test_check_articles_marks_total_refusal(self) -> None:
        storage.init_db()
        pid = storage.add_project("Тест", "example.ru")
        storage.add_article(pid, "https://example.ru/blog/x", "ремонт квартиры")
        self.assertRefused("check_articles", {"project_id": pid})

    def test_geo_visibility_marks_total_refusal(self) -> None:
        text = self.assertRefused("geo_check_visibility", {
            "queries": ["где заказать ремонт"], "domain": "example.ru",
            "providers": ["yandex"], "confirm": True})
        self.assertIn("yandex", text)

    def test_free_tools_are_not_affected(self) -> None:
        for name, args in {"whoami": {}, "get_storage_stats": {},
                           "get_positions": {"domain": "example.ru"}}.items():
            with self.subTest(tool=name):
                self.assertFalse(_call(name, args).get("isError"))

    def test_partial_failure_is_not_an_error_but_is_listed(self) -> None:
        doc = SerpDoc(position=1, organic_position=1, is_wizard=False, url="https://a.ru/",
                      domain="a.ru", title="t", title_hlwords=0, url_depth=0,
                      domain_doccount=1, modtime=None, passage=None)
        ok = SerpResult("ремонт", 225, "2026-01-01T00:00:00+00:00", 1, [doc])
        bad = SerpResult("отделка", 225, "2026-01-01T00:00:00+00:00", 0, [],
                         error="HTTPError 401: Unknown api key")
        with mock.patch.object(mcp_server, "score_batch", return_value=([], [ok, bad])):
            res = _call("find_serp_competitors", {"queries": ["ремонт", "отделка"]})
        text = res["content"][0]["text"]
        self.assertFalse(res.get("isError"))
        self.assertIn("Не удалось снять: отделка — HTTPError 401", text)


class LatestWindowTests(IsolatedTestCase):
    HOST = "https:example.ru:443"

    def setUp(self) -> None:
        super().setUp()
        storage.init_db()

    def test_two_pulls_with_same_end_give_one_window(self) -> None:
        storage.save_search_queries(self.HOST, "2026-08-18", "2026-09-14", [
            {"query": "брендинг", "shows": 72, "clicks": 1, "avg_show_position": 7.2}])
        storage.save_search_queries(self.HOST, "2026-09-08", "2026-09-14", [
            {"query": "брендинг", "shows": 22, "clicks": 0, "avg_show_position": 7.8},
            {"query": "нейминг", "shows": 5, "clicks": 0, "avg_show_position": 9.0}])
        rows = storage.search_queries_latest(self.HOST)
        self.assertEqual([r["query"] for r in rows].count("брендинг"), 1)
        self.assertEqual({r["date_from"] for r in rows}, {"2026-09-08"})
        win = storage.search_window(self.HOST)
        self.assertEqual((win["date_from"], win["queries"], win["shows"]), ("2026-09-08", 2, 27))
        self.assertEqual(storage.search_queries_map(self.HOST)["брендинг"]["shows"], 22)
        st = storage.stats()
        self.assertEqual(st["webmaster_shows_all_hosts"], 27)

    def test_repeated_pull_of_longer_window_wins_back(self) -> None:
        storage.save_search_queries(self.HOST, "2026-09-08", "2026-09-14", [
            {"query": "брендинг", "shows": 22, "clicks": 0, "avg_show_position": 7.8}])
        storage.save_search_queries(self.HOST, "2026-08-18", "2026-09-14", [
            {"query": "брендинг", "shows": 72, "clicks": 1, "avg_show_position": 7.2}])
        rows = storage.search_queries_latest(self.HOST)
        self.assertEqual([(r["query"], r["shows"]) for r in rows], [("брендинг", 72)])

    def test_newer_end_always_wins(self) -> None:
        storage.save_search_queries(self.HOST, "2026-08-19", "2026-09-15", [
            {"query": "брендинг", "shows": 70, "clicks": 1, "avg_show_position": 7.0}])
        storage.save_search_queries(self.HOST, "2026-09-08", "2026-09-14", [
            {"query": "брендинг", "shows": 22, "clicks": 0, "avg_show_position": 7.8}])
        self.assertEqual(storage.search_window(self.HOST)["date_to"], "2026-09-15")

    def test_query_pages_use_one_window(self) -> None:
        storage.save_search_query_pages(self.HOST, "2026-08-18", "2026-09-14", [
            {"query": "брендинг", "url": "/old/", "shows": 72}])
        storage.save_search_query_pages(self.HOST, "2026-09-01", "2026-09-14", [
            {"query": "брендинг", "url": "/new/", "shows": 30}])
        self.assertEqual(storage.search_pages_map(self.HOST)["брендинг"]["url"], "/new/")

    def test_metrika_two_pulls_same_day_give_one_window(self) -> None:
        storage.save_metrika_pages(1, "2026-08-19", "2026-09-16", "organic", [
            {"url": "https://example.ru/a", "visits": 40}])
        storage.save_metrika_pages(1, "2026-09-09", "2026-09-16", "organic", [
            {"url": "https://example.ru/a", "visits": 9}])
        rows = storage.metrika_pages_latest(1)
        self.assertEqual([(r["url"], r["visits"]) for r in rows], [("https://example.ru/a", 9)])
        self.assertEqual(len(storage.metrika_pages_map(1)), 1)
