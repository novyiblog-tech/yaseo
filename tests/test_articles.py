"""
Тесты `yaseo.articles`: потолок обращений у `check_all` и смета до траты.

Дефект 16.09.2026: у `check_articles`/`check_all` не было потолка вовсе —
число статей и дополнительных запросов росло, а денег это никак не ограничивало.
Почин зеркалит `tracker.run_tracking`: смета считается ДО единого платного
запроса, а прогон, который в потолок не укладывается, останавливается и ничего
не тратит. Сеть подменяется на уровне `articles.serp_batch` — тесты наружу не ходят.
"""
from __future__ import annotations

import unittest
from unittest import mock

from tests.helpers import IsolatedTestCase
from yaseo import articles, storage
from yaseo.yandex_serp import SerpResult


def _serp(query: str) -> SerpResult:
    return SerpResult(query=query, region=225, fetched_at="2024-01-01T00:00:00+00:00",
                      found_all=0, docs=[])


def _project() -> int:
    return storage.add_project("Проект", "example.ru")


def _add(project_id: int, url: str, query: str) -> int:
    return storage.add_article(project_id=project_id, url=url, target_query=query)


class CheckAllCapTests(IsolatedTestCase):
    def test_no_articles_returns_empty_run_without_touching_network(self) -> None:
        storage.init_db()
        with mock.patch("yaseo.articles.serp_batch") as mocked:
            run = articles.check_all()
        mocked.assert_not_called()
        self.assertEqual(run.checked, [])
        self.assertIsNone(run.stopped)
        self.assertEqual(run.calls_planned, 0)

    def test_stops_before_spending_when_plan_exceeds_cap(self) -> None:
        storage.init_db()
        pid = _project()
        for i in range(10):
            _add(pid, f"https://example.ru/blog/{i}", f"запрос {i}")

        with mock.patch("yaseo.articles.serp_batch") as mocked:
            run = articles.check_all(cap=3, progress=False)

        mocked.assert_not_called()
        self.assertIsNotNone(run.stopped)
        self.assertIn("потолке 3", run.stopped)
        self.assertEqual(run.checked, [])
        self.assertEqual(run.calls_planned, 10)

    def test_cap_zero_means_no_limit(self) -> None:
        storage.init_db()
        pid = _project()
        _add(pid, "https://example.ru/blog/x", "запрос")
        with mock.patch("yaseo.articles.serp_batch", return_value=[_serp("запрос")]):
            run = articles.check_all(cap=0, progress=False)
        self.assertIsNone(run.stopped)
        self.assertEqual(len(run.checked), 1)

    def test_shared_query_across_articles_counts_once_for_the_cap(self) -> None:
        # Один и тот же запрос у двух статей — снимок один, а не по разу за
        # каждую: потолок 1 должен пропустить прогон, а не остановить его.
        storage.init_db()
        pid = _project()
        _add(pid, "https://example.ru/blog/a", "общий запрос")
        _add(pid, "https://example.ru/blog/b", "общий запрос")
        with mock.patch(
            "yaseo.articles.serp_batch", return_value=[_serp("общий запрос")]
        ) as mocked:
            run = articles.check_all(cap=1, progress=False)
        mocked.assert_called_once()
        self.assertIsNone(run.stopped)
        self.assertEqual(len(run.checked), 2)  # два вердикта — по одному на статью
        self.assertEqual(run.calls_planned, 1)

    def test_depth_above_ten_multiplies_calls_by_pages(self) -> None:
        # n=30 стоит 3 обращения на уникальный запрос, а не одно — постраничный
        # набор выдачи, тот же расчёт, что у трекера позиций.
        storage.init_db()
        pid = _project()
        _add(pid, "https://example.ru/blog/x", "запрос")
        cap = 2  # хватило бы на 1 запрос без учёта страниц, но не хватает с ними
        with mock.patch("yaseo.articles.serp_batch") as mocked:
            run = articles.check_all(cap=cap, n=30, progress=False)
        mocked.assert_not_called()
        self.assertEqual(run.calls_planned, 3)
        self.assertIn("глубина топ-30", run.stopped)

    def test_default_cap_matches_max_calls_setting(self) -> None:
        storage.init_db()
        self.assertEqual(articles.max_calls(), articles.DEFAULT_MAX_CALLS)


class MaxCallsSettingTests(IsolatedTestCase):
    def test_env_var_overrides_default(self) -> None:
        import os
        os.environ["YASEO_ARTICLES_MAX_CALLS"] = "7"
        self.assertEqual(articles.max_calls(), 7)


class CheckEstimateTests(IsolatedTestCase):
    def test_estimate_before_spending_matches_planned_calls(self) -> None:
        storage.init_db()
        pid = _project()
        for i in range(4):
            _add(pid, f"https://example.ru/blog/{i}", f"запрос {i}")
        est = articles.check_estimate(project_id=pid, n=10)
        self.assertEqual(est["обращений"], 4)
        self.assertTrue(est["платный"])

    def test_estimate_with_no_articles_is_free(self) -> None:
        storage.init_db()
        est = articles.check_estimate()
        self.assertFalse(est["платный"])


if __name__ == "__main__":
    unittest.main()
