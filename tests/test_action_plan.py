"""
Тесты `yaseo.plan` — план действий.

База засевается фикстурами (позиции, выгрузка Вебмастера, реестр статей,
снимки выдачи, частотность), сайт поднимается локально из
`tests/fixtures/site/` (как в `test_audit.py`), а robots.txt дополняется
группой, закрывающей YandexAdditional.

Главное условие: план не делает ни одного обращения за пределы локального
сайта. `OpenerDirector.open` подменён: запрос к любому другому хосту
записывается и роняет вызов, а тест проверяет, что таких запросов не было.
"""
from __future__ import annotations

import functools
import http.server
import os
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from tests.helpers import IsolatedTestCase, isolated_environment
from yaseo import audit, mcp_server, plan, storage

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "site"
DOMAIN = "example.ru"
HOST_ID = "https:example.ru:443"

PAID_HOSTS = {
    "searchapi.api.cloud.yandex.net",
    "api.webmaster.yandex.net",
    "api-metrika.yandex.net",
    "llm.api.cloud.yandex.net",
}


class _NetGuard:
    """Пускает только на локальный сайт. Остальное записывает и роняет."""

    def __init__(self) -> None:
        self.hosts: list[str] = []
        self.blocked: list[str] = []
        self._orig = urllib.request.OpenerDirector.open

    def __enter__(self) -> "_NetGuard":
        guard = self

        def open_(opener, req, *a, **kw):
            url = req if isinstance(req, str) else req.full_url
            host = urlsplit(url).hostname or ""
            guard.hosts.append(host)
            if host != "127.0.0.1":
                guard.blocked.append(url)
                raise AssertionError(f"план полез наружу: {url}")
            return guard._orig(opener, req, *a, **kw)

        self._patch = mock.patch.object(urllib.request.OpenerDirector, "open", open_)
        self._patch.start()
        return self

    def __exit__(self, *exc) -> None:
        self._patch.stop()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # журнал запросов в вывод тестов не нужен
        pass


def _serve(tmp: Path):
    handler = functools.partial(_QuietHandler, directory=str(tmp))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    for src in FIXTURE_DIR.rglob("*"):
        if src.is_dir():
            continue
        dst = tmp / src.relative_to(FIXTURE_DIR)
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8").replace("__PORT__", str(port))
        if src.name == "robots.txt":
            text += "\nUser-agent: YandexAdditional\nDisallow: /\n"
        dst.write_text(text, encoding="utf-8")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, port


def _seed() -> None:
    storage.init_db()
    pid = storage.add_project("Тестовый сайт", DOMAIN)
    storage.set_project_yandex(pid, webmaster_host=HOST_ID)

    # Вебмастер: одно окно.
    storage.save_search_queries(HOST_ID, "2026-08-18", "2026-09-14", [
        {"query": "тестовый запрос сниппета", "shows": 40, "clicks": 0,
         "avg_show_position": 6.0},
        {"query": "запрос с кликами", "shows": 40, "clicks": 3, "avg_show_position": 6.0},
        {"query": "далёкий запрос", "shows": 50, "clicks": 0, "avg_show_position": 30.0},
        {"query": "запрос без страницы", "shows": 25, "clicks": 1, "avg_show_position": 8.0},
    ], project_id=pid)
    storage.save_search_query_pages(HOST_ID, "2026-09-01", "2026-09-14", [
        {"query": "тестовый запрос сниппета", "url": "/blog/dup-a.html", "shows": 40,
         "clicks": 0, "avg_position": 6.0},
        {"query": "запрос с кликами", "url": "/blog/dup-b.html", "shows": 40,
         "clicks": 3, "avg_position": 6.0},
        {"query": "далёкий запрос", "url": "/blog/no-title.html", "shows": 50,
         "clicks": 0, "avg_position": 30.0},
        # Та же потребность, что у статей реестра: 12 показов у третьей страницы —
        # доказательство; 2 показа у четвёртой — шум, в план не идёт.
        {"query": "каннибальный запрос", "url": "/blog/wrong-canon.html", "shows": 12,
         "clicks": 0, "avg_position": 9.0},
        {"query": "каннибальных запросов", "url": "/blog/two-h1.html", "shows": 2,
         "clicks": 0, "avg_position": 9.0},
    ], project_id=pid)

    # Реестр: две свои страницы под один запрос.
    storage.add_article(pid, "https://example.ru/blog/dup-a.html", "каннибальный запрос")
    storage.add_article(pid, "https://example.ru/blog/dup-b.html", "каннибальные запросы")

    # Трекер: запрос на позиции 7 со спросом из Wordstat, и снимки выдачи.
    storage.track_for_project(pid, "позиционный запрос", 225)
    storage.track_for_project(pid, "разрывный запрос", 225)
    storage.track_for_project(pid, "второй запрос", 225)
    with storage.connect() as conn:
        now = storage.now()
        conn.execute(
            "INSERT INTO positions (query, region, domain, position, url, checked_at) "
            "VALUES (?,?,?,?,?,?)",
            ("позиционный запрос", 225, DOMAIN, 7, "https://example.ru/blog/wrong-canon.html", now))
        for phrase, freq in (("позиционный запрос", 50), ("разрывный запрос", 120)):
            conn.execute("INSERT INTO keyword_freq (phrase, region, freq, fetched_at) "
                         "VALUES (?,?,?,?)", (phrase, "225", freq, now))
        for q, docs in (
            ("разрывный запрос", [("rival.ru", "https://rival.ru/page")]),
            ("второй запрос", [("rival.ru", "https://rival.ru/other"),
                               (DOMAIN, "https://example.ru/")]),
            # Две свои страницы в одном топе (брендовый запрос) — не каннибализация.
            ("брендовый запрос", [(DOMAIN, "https://example.ru/"),
                                  (DOMAIN, "https://example.ru/about/")]),
        ):
            cur = conn.execute("INSERT INTO serp_snapshots (query, region, fetched_at, found_all) "
                               "VALUES (?,?,?,?)", (q, 225, now, 10))
            for n, (dom, url) in enumerate(docs, 1):
                conn.execute("INSERT INTO serp_docs (snapshot_id, position, url, domain, title) "
                             "VALUES (?,?,?,?,?)", (cur.lastrowid, n, url, dom, "t"))


class PlanOnSeededBaseTests(unittest.TestCase):
    """Один план на класс: обход сайта не бесплатен по времени."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls._env = isolated_environment(tmp / "env")
        cls._env.__enter__()
        # Локальный сайт — внутренний адрес: обход к нему разрешается явно
        # (net.check_url); окружение восстановит isolated_environment.
        os.environ["YASEO_ALLOW_PRIVATE"] = "1"
        site = tmp / "site"
        site.mkdir()
        cls.httpd, cls.thread, port = _serve(site)
        cls.root = f"http://127.0.0.1:{port}/"
        _seed()
        with _NetGuard() as guard:
            cls.plan = plan.build_plan(DOMAIN, url=cls.root, max_pages=30, delay=0, timeout=5)
            cls.text = plan.render(cls.plan, limit=100)
            cls.stored = plan.build_plan(DOMAIN, url=cls.root, sections=["indexing"],
                                         fresh_audit=False, delay=0, timeout=5)
        cls.guard = guard

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls._env.__exit__(None, None, None)
        cls._tmp.cleanup()

    def _section(self, key: str) -> list:
        return [a for a in self.plan.actions if a.section == key]

    def test_no_request_leaves_the_local_site(self) -> None:
        self.assertEqual(self.guard.blocked, [])
        self.assertTrue(self.guard.hosts, "контроль: план вообще ходил к сайту")
        self.assertFalse(PAID_HOSTS & set(self.guard.hosts))
        self.assertEqual(self.plan.paid_calls, 0)

    def test_sections_follow_rule_order(self) -> None:
        rank = {k: n for n, k in enumerate(plan.SECTIONS)}
        seen = [rank[a.section] for a in self.plan.actions]
        self.assertEqual(seen, sorted(seen))
        present = {a.section for a in self.plan.actions}
        self.assertEqual(present, set(plan.SECTIONS))
        # В тексте разделы идут в том же порядке.
        heads = [line for line in self.text.splitlines() if line.startswith("## ") and "·" in line]
        titles = [plan.SECTIONS[a.section][0] for a in self.plan.actions]
        self.assertEqual([h.split(". ", 1)[1].split(" · ")[0] for h in heads], titles)

    def test_every_item_has_dated_evidence_instruction_and_check(self) -> None:
        for a in self.plan.actions:
            with self.subTest(rule=a.rule, url=a.url):
                self.assertTrue(a.rule and a.why and a.url and a.todo)
                self.assertTrue(a.evidence)
                for e in a.evidence:
                    self.assertTrue(e.text and e.source)
                    datetime.fromisoformat(e.measured_at.replace("Z", "+00:00"))
                self.assertGreater(len(a.claude), 40)
                self.assertTrue(a.verify)
                self.assertTrue(a.wait)
        self.assertEqual(self.text.count("**Инструкция для Claude:**"), len(self.plan.actions))
        self.assertEqual(self.text.count("**Как проверить после:**"), len(self.plan.actions))
        self.assertIn("## Ограничения", self.text)

    def test_indexing_holds_critical_findings(self) -> None:
        rules = {a.rule for a in self._section("indexing")}
        self.assertIn("audit:broken-internal-link", rules)
        self.assertIn("audit:noindex-в-карте", rules)
        self.assertIn("audit:robots-disallow-в-карте", rules)
        # Информационные находки «чинить нечего» в план не идут.
        self.assertNotIn("audit:noindex-not-checked", {a.rule for a in self.plan.actions})

    def test_snippet_item_only_for_zero_clicks(self) -> None:
        snippet = " ".join(e.text for a in self._section("snippet") for e in a.evidence)
        self.assertIn("тестовый запрос сниппета", snippet)
        self.assertNotIn("запрос с кликами", snippet)
        self.assertNotIn("далёкий запрос", snippet)
        quick = " ".join(e.text for a in self._section("quick_wins") for e in a.evidence)
        self.assertIn("запрос с кликами", quick)
        self.assertNotIn("тестовый запрос сниппета", quick)
        self.assertNotIn("далёкий запрос", quick)

    def test_quick_win_quotes_current_title_and_query(self) -> None:
        item = next(a for a in self._section("quick_wins") if "dup-b" in a.url)
        self.assertIn("title «", " ".join(e.text for e in item.evidence))
        # Запрос — данные: в инструкции только ссылка на поле, само значение — в data.
        self.assertEqual(item.data["запрос"], "запрос с кликами")
        self.assertNotIn("запрос с кликами", item.claude)
        self.assertIn("[данные: запрос]", item.claude)
        self.assertIn("[данные: title]", item.claude)
        self.assertIn("Не меняй URL", item.claude)
        self.assertIn("не раньше чем через 13 дн.", item.wait)

    def test_tracker_quick_win_needs_known_demand(self) -> None:
        item = next(a for a in self._section("quick_wins") if "wrong-canon" in a.url)
        self.assertEqual(item.rule, "трекер:позиция-4-15")
        self.assertIn("спрос 50", " ".join(e.text for e in item.evidence))

    def test_query_without_page_is_not_invented(self) -> None:
        self.assertNotIn("запрос без страницы",
                         " ".join(a.claude + str(a.data) for a in self.plan.actions))
        self.assertTrue(any("страница, которая по ним показывается, неизвестна" in m
                            for m in self.plan.missing))

    def test_cannibalization_names_both_pages(self) -> None:
        items = self._section("cannibalization")
        self.assertEqual(len(items), 1)
        self.assertIn("/blog/dup-a.html", items[0].url)
        self.assertIn("/blog/dup-b.html", items[0].url)
        self.assertEqual(items[0].rule, "каннибализация:реестр+вебмастер")
        self.assertIn("/blog/wrong-canon.html", items[0].data["страницы"])
        self.assertNotIn("/blog/wrong-canon.html", items[0].claude)
        self.assertNotIn("two-h1", items[0].data["страницы"])
        self.assertNotIn("брендовый запрос", " ".join(a.claude + str(a.data) for a in self.plan.actions))

    def test_ai_search_flags_yandexadditional_and_llms(self) -> None:
        rules = [a.rule for a in self._section("ai_search")]
        self.assertIn("geo:robots-blocks-yandexadditional", rules)
        self.assertIn("geo:llms-txt-missing", rules)
        self.assertEqual(rules[0], "geo:robots-blocks-yandexadditional")
        item = self._section("ai_search")[0]
        self.assertIn("User-agent: YandexAdditional", item.claude)

    def test_competitor_gap_needs_frequency_and_rival_in_serp(self) -> None:
        items = self._section("competitor_gaps")
        self.assertEqual([a.url for a in items], ["нет страницы под «разрывный запрос»"])
        self.assertIn("rival.ru", " ".join(e.text for e in items[0].evidence))
        self.assertIn("build_brief", items[0].claude)

    def test_stored_audit_is_reused(self) -> None:
        self.assertTrue(any("аудит из базы" in s for s in self.stored.sources))
        self.assertTrue(self.stored.actions)
        self.assertEqual({a.section for a in self.stored.actions}, {"indexing"})

    def test_limit_folds_the_rest_by_section(self) -> None:
        short = plan.render(self.plan, limit=2)
        self.assertEqual(short.count("**Инструкция для Claude:**"), 2)
        self.assertIn("## Не показано", short)
        self.assertIn("Сниппет (`snippet`): ещё 1", short)


class EmptyBaseTests(IsolatedTestCase):
    """Пустая база: пунктов про позиции нет, есть путь к данным и смета."""

    def test_guard_catches_a_paid_request(self) -> None:
        """Положительный контроль: страж видит запрос к Search API тем же путём,
        каким ходит пакет. Без этого его молчание ничего бы не доказывало."""
        from yaseo import yandex_serp
        with _NetGuard() as guard:
            with self.assertRaises(AssertionError):
                yandex_serp.opener().open(urllib.request.Request(yandex_serp.SEARCH_ENDPOINT))
        self.assertEqual(len(guard.blocked), 1)
        self.assertIn("searchapi.api.cloud.yandex.net", guard.blocked[0])

    def test_no_invented_position_items(self) -> None:
        with _NetGuard() as guard, mock.patch.object(
            plan, "_run_audit", return_value=plan._Audit([], {}, None, "аудит пропущен")
        ), mock.patch.object(plan, "_run_readiness", return_value=(None, None, "")):
            p = plan.build_plan(DOMAIN)
            text = plan.render(p)
        self.assertEqual(guard.blocked, [])
        self.assertEqual(p.actions, [])
        self.assertIn("run_tracking", text)
        self.assertIn("Потратит: 3 обращения к «Yandex Search API»", text)
        self.assertIn("yaseo webmaster --pull", text)
        self.assertNotIn("Быстрые выигрыши ·", text)

    def test_tracked_queries_get_run_estimate(self) -> None:
        storage.init_db()
        pid = storage.add_project("Пустой", DOMAIN)
        for q in ("первый", "второй"):
            storage.track_for_project(pid, q, 225)
        with _NetGuard() as guard, mock.patch.object(
            plan, "_run_audit", return_value=plan._Audit([], {}, None, "")
        ), mock.patch.object(plan, "_run_readiness", return_value=(None, None, "")):
            p = plan.build_plan(DOMAIN, sections=["quick_wins"])
        self.assertEqual(guard.blocked, [])
        joined = " ".join(p.missing)
        self.assertIn("run_tracking (2 отслеживаемых запросов)", joined)
        self.assertIn("6 обращений", joined)

    def test_unknown_section_is_an_input_error(self) -> None:
        res = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "get_action_plan", "arguments": {"domain": DOMAIN, "sections": ["всё"]}}})
        self.assertTrue(res["result"]["isError"])
        self.assertIn("Неизвестные разделы", res["result"]["content"][0]["text"])

    def test_tool_is_listed(self) -> None:
        res = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("get_action_plan", {t["name"] for t in res["result"]["tools"]})

    def test_limit_is_validated(self) -> None:
        res = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "get_action_plan", "arguments": {"domain": DOMAIN, "limit": 0}}})
        self.assertTrue(res["result"]["isError"])


class H1QuoteTests(unittest.TestCase):
    def test_h1_text_nodes_are_joined_with_space(self) -> None:
        p = audit._PageParser("https://example.ru/")
        p.feed('<h1><span>Маркетинг</span><span><b>застройщика</b></span>'
               '<span>в одной команде</span></h1>')
        self.assertEqual(p.h1, ["Маркетинг застройщика в одной команде"])


if __name__ == "__main__":
    unittest.main()
