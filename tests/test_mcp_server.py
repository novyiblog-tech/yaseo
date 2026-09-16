"""
Тесты `yaseo.mcp_server`: протокол JSON-RPC поверх stdio без сети.

Каждый тест работает во временных HOME/XDG_*/YASEO_DB (`IsolatedTestCase`),
поэтому отсутствие ключей в этих тестах — не случайность окружения машины,
а сознательно созданное условие для проверки контракта `isError`.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from tests.helpers import IsolatedTestCase
from yaseo import mcp_server, storage
from yaseo.yandex_serp import SerpDoc, SerpResult


def _serp_with_position(query: str, domain: str, position: int) -> SerpResult:
    doc = SerpDoc(position=position, organic_position=position, is_wizard=False,
                 url=f"https://{domain}/", domain=domain, title="t", title_hlwords=0,
                 url_depth=0, domain_doccount=1, modtime=None, passage=None)
    return SerpResult(query=query, region=225, fetched_at="2024-01-01T00:00:00+00:00",
                      found_all=1, docs=[doc])


CORE_TOOL_NAMES = {
    "whoami", "research_keywords", "get_keyword_metrics", "get_serp_results",
    "find_serp_competitors", "get_competition", "build_brief", "track_query",
    "run_tracking", "get_positions", "get_position_history", "audit_site",
    "check_articles", "get_article_effectiveness", "get_storage_stats",
    "expand_query_pool", "get_competitor_keywords",
}


def _call(name: str, arguments: dict | None = None, req_id: int = 1) -> dict:
    return mcp_server.handle({
        "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })


class ProtocolTests(IsolatedTestCase):
    def test_initialize_reports_protocol_and_server_info(self) -> None:
        res = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(res["result"]["protocolVersion"], mcp_server.PROTOCOL_VERSION)
        self.assertEqual(res["result"]["serverInfo"]["name"], "yaseo")
        self.assertIn("tools", res["result"]["capabilities"])

    def test_tools_list_has_at_least_the_17_core_tools(self) -> None:
        res = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = res["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertGreaterEqual(len(names), 17, f"инструментов только {len(names)}: {sorted(names)}")
        missing_core = CORE_TOOL_NAMES - names
        self.assertFalse(missing_core, f"нет инструментов ядра: {missing_core}")
        # У каждого объявлена схема входа — контракт для MCP-клиента.
        for t in tools:
            self.assertIn("inputSchema", t)
            self.assertIn("description", t)
            self.assertNotIn("handler", t)  # обработчик не должен утекать наружу

    def test_notifications_initialized_returns_nothing(self) -> None:
        self.assertIsNone(
            mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_ping_returns_empty_result(self) -> None:
        res = mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "ping"})
        self.assertEqual(res, {"jsonrpc": "2.0", "id": 5, "result": {}})

    def test_unknown_top_level_method_is_method_not_found(self) -> None:
        res = mcp_server.handle({"jsonrpc": "2.0", "id": 6, "method": "совсем/не метод"})
        self.assertEqual(res["error"]["code"], -32601)

    def test_unknown_tool_name_is_method_not_found(self) -> None:
        res = _call("такого_инструмента_нет", req_id=7)
        self.assertEqual(res["error"]["code"], -32601)
        self.assertIn("такого_инструмента_нет", res["error"]["message"])


class InputValidationTests(IsolatedTestCase):
    def test_missing_required_field_is_error_without_traceback(self) -> None:
        res = _call("research_keywords", {})
        self.assertTrue(res["result"]["isError"])
        text = res["result"]["content"][0]["text"]
        self.assertNotIn("Traceback", text)
        self.assertIn("seed", text.lower())

    def test_missing_query_on_serp_tool_is_error(self) -> None:
        res = _call("get_serp_results", {})
        self.assertTrue(res["result"]["isError"])
        self.assertIn("query", res["result"]["content"][0]["text"].lower())

    def test_missing_queries_on_competitors_tool_is_error(self) -> None:
        res = _call("find_serp_competitors", {})
        self.assertTrue(res["result"]["isError"])


class MissingKeysKeepServerAliveTests(IsolatedTestCase):
    def test_call_requiring_keys_is_error_and_server_stays_usable(self) -> None:
        res = _call("get_serp_results", {"query": "пластиковые окна"}, req_id=10)
        self.assertTrue(res["result"]["isError"])
        text = res["result"]["content"][0]["text"]
        self.assertIn("YC_FOLDER_ID", text)
        self.assertIn("YANDEX_AI_STUDIO_API_KEY", text)
        self.assertNotIn("Traceback", text)

        # Сервер жив: следующий вызов (даже другого инструмента) отвечает нормально.
        ping = mcp_server.handle({"jsonrpc": "2.0", "id": 11, "method": "ping"})
        self.assertEqual(ping["result"], {})

        who = _call("whoami", req_id=12)
        self.assertNotIn("isError", who["result"])
        self.assertIn("YC_FOLDER_ID", who["result"]["content"][0]["text"])

    def test_run_tracking_without_domain_is_domain_not_set_error(self) -> None:
        res = _call("run_tracking", {}, req_id=13)
        self.assertTrue(res["result"]["isError"])


class RunTrackingStopMessagesTests(IsolatedTestCase):
    """
    Дефект 16.09.2026: остановка ДО первого запроса (потолок обращений или
    срок между прогонами) отвечала невнятным «Прогон -1: снято 0 из N»,
    без единого слова о причине. Инструмент обязан отдавать ту же
    человеческую причину, что печатает CLI.
    """

    def test_stopped_by_cap_explains_why_instead_of_run_minus_one(self) -> None:
        storage.init_db()
        storage.track("запрос один", region=225)
        storage.track("запрос два", region=225)
        os.environ["YASEO_TRACKING_MAX_CALLS"] = "1"

        with mock.patch("yaseo.tracker.serp") as mocked_serp:
            res = _call("run_tracking", {"domain": "example.ru", "region": 225}, req_id=30)
        mocked_serp.assert_not_called()
        self.assertNotIn("isError", res["result"])
        text = res["result"]["content"][0]["text"]
        self.assertNotIn("Прогон -1: снято", text)
        self.assertIn("остановлен до первого запроса", text)
        self.assertIn("потолке", text)
        self.assertIn("Ничего не потрачено", text)

    def test_stopped_by_min_interval_explains_why(self) -> None:
        storage.init_db()
        from datetime import datetime, timezone

        now_iso = datetime.now(timezone.utc).isoformat()
        r = SerpResult(query="q", region=225, fetched_at=now_iso, found_all=1, docs=[])
        storage.record_positions(r, ["example.ru"], override={"example.ru": 3})
        storage.track("q", region=225)

        with mock.patch("yaseo.tracker.serp") as mocked_serp:
            res = _call("run_tracking", {"domain": "example.ru", "region": 225}, req_id=31)
        mocked_serp.assert_not_called()
        self.assertNotIn("isError", res["result"])
        text = res["result"]["content"][0]["text"]
        self.assertNotIn("Прогон -1: снято", text)
        self.assertIn("прошлый замер", text)
        self.assertIn("следующий не раньше", text)
        self.assertIn("now=true", text)

    def test_now_true_skips_the_interval_gate(self) -> None:
        storage.init_db()
        from datetime import datetime, timezone

        now_iso = datetime.now(timezone.utc).isoformat()
        r = SerpResult(query="q", region=225, fetched_at=now_iso, found_all=1, docs=[])
        storage.record_positions(r, ["example.ru"], override={"example.ru": 3})
        storage.track("q", region=225)

        with mock.patch("yaseo.tracker.serp", return_value=_serp_with_position("q", "example.ru", 1)):
            res = _call("run_tracking", {
                "domain": "example.ru", "region": 225, "now": True,
            }, req_id=32)
        self.assertNotIn("isError", res["result"])
        text = res["result"]["content"][0]["text"]
        self.assertIn("Прогон", text)
        self.assertNotIn("прошлый замер", text)


class SafeToolsWithoutKeysTests(IsolatedTestCase):
    """Инструменты, которые ничего не тратят и должны работать даже без ключей."""

    def test_whoami_reports_missing_keys_without_failing(self) -> None:
        res = _call("whoami", req_id=20)
        self.assertNotIn("isError", res["result"])
        text = res["result"]["content"][0]["text"]
        self.assertIn("не готово", text.lower())

    def test_get_storage_stats_works_on_empty_db(self) -> None:
        res = _call("get_storage_stats", req_id=21)
        self.assertNotIn("isError", res["result"])

    def test_check_articles_with_no_articles_needs_no_network(self) -> None:
        res = _call("check_articles", {}, req_id=22)
        self.assertNotIn("isError", res["result"])
        self.assertIn("Статей на отслеживании нет", res["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
