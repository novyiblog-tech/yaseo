"""
Тесты `yaseo.tracker`: медиана трёх снимков, «нет в топе» как NULL, шум внутри
разброса, смета/потолок до траты и минимальный интервал между прогонами.
Сеть подменяется на уровне `tracker.serp` — ни один тест не стучится наружу.
"""
from __future__ import annotations

import unittest
from datetime import timezone
from unittest import mock

from tests.helpers import IsolatedTestCase
from yaseo import storage, tracker
from yaseo.yandex_serp import SerpDoc, SerpResult


def _result_with_position(query: str, domain: str, position: int | None) -> SerpResult:
    docs = []
    if position is not None:
        docs.append(
            SerpDoc(
                position=position, organic_position=position, is_wizard=False,
                url=f"https://{domain}/", domain=domain, title="t", title_hlwords=0,
                url_depth=0, domain_doccount=1, modtime=None, passage=None,
            )
        )
    return SerpResult(query=query, region=225, fetched_at="2024-01-01T00:00:00+00:00",
                      found_all=1 if position else 0, docs=docs)


class MedianTests(unittest.TestCase):
    """
    Дефект 16.09.2026: при чётном числе снимков «нет в топе» усреднялось с
    реальной позицией. `_median([5, None])` считал (5 + 10000) // 2 = 5002 —
    число без смысла, которое уходило в историю и в отчёт как настоящая
    позиция. Исправлено: чётная длина берёт нижнюю медиану (реально
    увиденное значение, не выдуманное среднее), а если один из двух
    центральных элементов сам «нет в топе» — результат тоже «нет в топе»,
    консервативно и объяснимо, а не число вроде 5002.
    """

    def test_odd_count_middle_value(self) -> None:
        self.assertEqual(tracker._median([5, 3, 7]), 5)

    def test_single_value_is_itself(self) -> None:
        self.assertEqual(tracker._median([5]), 5)

    def test_even_count_uses_lower_median_not_average(self) -> None:
        # Раньше здесь было арифметическое среднее (3 + 7) // 2 = 5 — числа,
        # которого не видел ни один снимок. Нижняя медиана берёт вместо
        # этого фактически наблюдавшееся значение — 3.
        self.assertEqual(tracker._median([3, 7]), 3)

    def test_absence_counts_as_worse_than_any_real_position(self) -> None:
        # Два «нет в топе» из трёх не должны дать оптимистичную медиану 5.
        self.assertIsNone(tracker._median([5, None, None]))

    def test_absence_can_lose_to_a_real_position_in_majority(self) -> None:
        self.assertEqual(tracker._median([5, 6, None]), 6)

    def test_even_count_one_of_two_absent_is_conservative_not_a_fake_number(self) -> None:
        # Дефектный случай ревью: было 5002 (среднее 5 и BEYOND=10000).
        # Половина снимков не нашла домен вовсе — результат «нет в топе»,
        # а не число, которого никто не видел.
        self.assertIsNone(tracker._median([5, None]))

    def test_even_count_both_absent_is_none(self) -> None:
        self.assertIsNone(tracker._median([None, None]))

    def test_empty_list_is_none(self) -> None:
        self.assertIsNone(tracker._median([]))


class OurPositionShapeTests(unittest.TestCase):
    def test_with_samples_reports_median_and_method(self) -> None:
        out = tracker.our_position(samples=[5, None, 7])
        self.assertEqual(out["позиция"], tracker._median([5, None, 7]))
        self.assertEqual(out["снимков"], 3)
        self.assertEqual(out["найдено в"], 2)
        self.assertIn("медиана", out["как"])

    def test_single_value_reports_different_method(self) -> None:
        out = tracker.our_position(single=9)
        self.assertEqual(out["позиция"], 9)
        self.assertEqual(out["снимков"], 1)
        self.assertEqual(out["как"], "один снимок выдачи")


class MeasureTests(unittest.TestCase):
    def test_measure_computes_median_min_max_and_keeps_samples(self) -> None:
        snapshots = [
            _result_with_position("запрос", "example.ru", 5),
            _result_with_position("запрос", "example.ru", 3),
            _result_with_position("запрос", "example.ru", 5),
        ]
        with mock.patch("yaseo.tracker.serp", side_effect=snapshots):
            m = tracker.measure("запрос", "example.ru", repeats=3)
        self.assertEqual(m["median"], 5)
        self.assertEqual(m["min"], 3)
        self.assertEqual(m["max"], 5)
        self.assertEqual(m["samples"], [5, 3, 5])
        self.assertEqual(m["snapshots"], 3)
        self.assertEqual(m["found_in"], 3)
        self.assertEqual(m["failed"], 0)

    def test_measure_domain_absent_from_all_snapshots_is_none(self) -> None:
        snapshots = [_result_with_position("запрос", "rival.ru", 4) for _ in range(3)]
        with mock.patch("yaseo.tracker.serp", side_effect=snapshots):
            m = tracker.measure("запрос", "example.ru", repeats=3)
        self.assertIsNone(m["median"])
        self.assertEqual(m["samples"], [None, None, None])
        self.assertEqual(m["found_in"], 0)


class PlanRepeatsTests(IsolatedTestCase):
    def test_classifies_hot_cold_and_new(self) -> None:
        storage.init_db()
        hot = _result_with_position("горячий запрос", "example.ru", None)
        storage.record_positions(hot, ["example.ru"], override={"example.ru": 4})
        cold = _result_with_position("холодный запрос", "example.ru", None)
        storage.record_positions(cold, ["example.ru"], override={"example.ru": None})

        plan = tracker.plan_repeats(
            ["горячий запрос", "холодный запрос", "новый запрос"], "example.ru",
        )
        self.assertEqual(plan["план"]["горячий запрос"], tracker.repeats_hot())
        self.assertEqual(plan["план"]["холодный запрос"], tracker.repeats_cold())
        self.assertEqual(plan["план"]["новый запрос"], tracker.repeats_hot())
        self.assertEqual(plan["новых"], 1)
        self.assertEqual(plan["холодных"], 1)
        self.assertEqual(plan["горячих"], 2)  # горячая + новая

    def test_duplicate_phrases_are_counted_once(self) -> None:
        storage.init_db()
        plan = tracker.plan_repeats(["одна фраза", "Одна Фраза", "одна фраза"], "example.ru")
        self.assertEqual(plan["фразы"], ["одна фраза"])
        self.assertEqual(plan["обращений"], tracker.repeats_hot())

    def test_default_depth_top_ten_is_one_page_no_change(self) -> None:
        # n=10 не передан — умолчание, страница одна, число не меняется.
        storage.init_db()
        plan = tracker.plan_repeats(["одна фраза"], "example.ru")
        self.assertEqual(plan["обращений"], tracker.repeats_hot())
        self.assertEqual(plan["обращений"], plan["снимков"])

    def test_deeper_snapshot_multiplies_calls_by_pages(self) -> None:
        # Дефект 16.09.2026: --n 30 стоит 3 обращения на снимок (3 страницы
        # по 10), а смета трекера это игнорировала и считала по числу снимков.
        storage.init_db()
        plan = tracker.plan_repeats(["одна фраза"], "example.ru", n=30)
        self.assertEqual(plan["снимков"], tracker.repeats_hot())
        self.assertEqual(plan["страниц_на_снимок"], 3)
        self.assertEqual(plan["обращений"], tracker.repeats_hot() * 3)
        self.assertIn("глубина топ-30", plan["из_чего"])


class RunTrackingCapTests(IsolatedTestCase):
    def test_stops_before_spending_when_plan_exceeds_cap(self) -> None:
        storage.init_db()
        queries = [f"запрос {i}" for i in range(10)]
        with mock.patch("yaseo.tracker.serp") as mocked_serp:
            res = tracker.run_tracking(["example.ru"], queries=queries, cap=3)
        mocked_serp.assert_not_called()
        self.assertEqual(res.run_id, -1)
        self.assertIsNotNone(res.stopped)
        self.assertEqual(res.calls_planned, 10 * tracker.repeats_setting())
        self.assertEqual(res.positions_recorded, 0)

    def test_cap_accounts_for_depth_pages_not_just_snapshots(self) -> None:
        # 1 запрос × repeats_hot() снимков × 3 страницы (n=30) должно упереться
        # в потолок, которого хватило бы только на снимки без учёта страниц.
        storage.init_db()
        cap = tracker.repeats_hot()  # ровно по числу снимков, без страниц не хватает
        with mock.patch("yaseo.tracker.serp") as mocked_serp:
            res = tracker.run_tracking(["example.ru"], queries=["q"], cap=cap, n=30)
        mocked_serp.assert_not_called()
        self.assertIsNotNone(res.stopped)
        self.assertEqual(res.calls_planned, tracker.repeats_hot() * 3)

    def test_cap_zero_means_no_limit(self) -> None:
        storage.init_db()
        with mock.patch(
            "yaseo.tracker.serp",
            return_value=_result_with_position("q", "example.ru", 1),
        ):
            res = tracker.run_tracking(["example.ru"], queries=["q"], cap=0, progress=False)
        self.assertIsNone(res.stopped)
        self.assertEqual(res.ok, 1)


class RunTrackingIntervalGateTests(IsolatedTestCase):
    """
    Дефект 16.09.2026: MCP-инструмент `run_tracking` не объяснял пропуск по
    сроку между прогонами так же по-человечески, как это делает CLI. Гейт
    теперь встроен в саму функцию через `min_interval=`/`now=` — здесь
    проверяется он напрямую, без MCP-обвязки.
    """

    def test_skips_before_spending_when_not_due_yet(self) -> None:
        storage.init_db()
        from datetime import datetime

        now_iso = datetime.now(timezone.utc).isoformat()
        r = SerpResult(query="q", region=225, fetched_at=now_iso, found_all=1, docs=[])
        storage.record_positions(r, ["example.ru"], override={"example.ru": 3})

        with mock.patch("yaseo.tracker.serp") as mocked_serp:
            res = tracker.run_tracking(["example.ru"], queries=["q"], min_interval=13)
        mocked_serp.assert_not_called()
        self.assertEqual(res.run_id, -1)
        self.assertIsNotNone(res.stopped)
        self.assertIn("прошлый замер", res.stopped)
        self.assertIn("следующий не раньше", res.stopped)
        self.assertIn("now=true", res.stopped)
        self.assertIn("Ничего не потрачено", res.stopped)

    def test_now_true_overrides_the_interval_gate(self) -> None:
        storage.init_db()
        from datetime import datetime

        now_iso = datetime.now(timezone.utc).isoformat()
        r = SerpResult(query="q", region=225, fetched_at=now_iso, found_all=1, docs=[])
        storage.record_positions(r, ["example.ru"], override={"example.ru": 3})

        with mock.patch(
            "yaseo.tracker.serp",
            return_value=_result_with_position("q", "example.ru", 1),
        ):
            res = tracker.run_tracking(
                ["example.ru"], queries=["q"], min_interval=13, now=True, progress=False,
            )
        self.assertIsNone(res.stopped)
        self.assertEqual(res.ok, 1)

    def test_no_min_interval_means_no_gate_as_before(self) -> None:
        # Поведение по умолчанию (CLI, старые вызовы) не меняется: без
        # min_interval гейта нет вовсе, даже если домен мерили только что.
        storage.init_db()
        from datetime import datetime

        now_iso = datetime.now(timezone.utc).isoformat()
        r = SerpResult(query="q", region=225, fetched_at=now_iso, found_all=1, docs=[])
        storage.record_positions(r, ["example.ru"], override={"example.ru": 3})

        with mock.patch(
            "yaseo.tracker.serp",
            return_value=_result_with_position("q", "example.ru", 1),
        ):
            res = tracker.run_tracking(["example.ru"], queries=["q"], progress=False)
        self.assertIsNone(res.stopped)
        self.assertEqual(res.ok, 1)


class DueTests(IsolatedTestCase):
    def test_true_when_never_measured(self) -> None:
        storage.init_db()
        d = tracker.due("example.ru")
        self.assertTrue(d["пора"])
        self.assertEqual(d["почему"], "замеров по домену ещё не было")

    def test_false_right_after_a_measurement_within_interval(self) -> None:
        storage.init_db()
        from datetime import datetime

        now_iso = datetime.now(timezone.utc).isoformat()
        r = SerpResult(query="q", region=225, fetched_at=now_iso, found_all=1, docs=[])
        storage.record_positions(r, ["example.ru"], override={"example.ru": 3})
        d = tracker.due("example.ru")
        self.assertFalse(d["пора"])
        self.assertIsNotNone(d["следующий"])

    def test_zero_interval_is_always_due(self) -> None:
        storage.init_db()
        from datetime import datetime

        now_iso = datetime.now(timezone.utc).isoformat()
        r = SerpResult(query="q", region=225, fetched_at=now_iso, found_all=1, docs=[])
        storage.record_positions(r, ["example.ru"], override={"example.ru": 3})
        d = tracker.due("example.ru", interval=0)
        self.assertTrue(d["пора"])


class NoiseVerdictTests(IsolatedTestCase):
    def test_movement_inside_spread_is_noise(self) -> None:
        storage.init_db()
        r1 = SerpResult(query="шум", region=225, fetched_at="2024-01-01T00:00:00+00:00",
                        found_all=1, docs=[])
        storage.record_positions(r1, ["example.ru"], samples={"example.ru": [5, 6, 7]},
                                 override={"example.ru": 6})
        r2 = SerpResult(query="шум", region=225, fetched_at="2024-01-02T00:00:00+00:00",
                        found_all=1, docs=[])
        storage.record_positions(r2, ["example.ru"], samples={"example.ru": [5, 6, 7]},
                                 override={"example.ru": 5})
        rows = storage.latest_positions("example.ru")
        row = next(r for r in rows if r["query"] == "шум")
        self.assertEqual(row["delta"], 1)
        self.assertTrue(row["within_noise"])

    def test_movement_beyond_spread_is_not_noise(self) -> None:
        storage.init_db()
        r1 = SerpResult(query="реальный рост", region=225, fetched_at="2024-01-01T00:00:00+00:00",
                        found_all=1, docs=[])
        storage.record_positions(r1, ["example.ru"], samples={"example.ru": [9, 10, 10]},
                                 override={"example.ru": 10})
        r2 = SerpResult(query="реальный рост", region=225, fetched_at="2024-01-02T00:00:00+00:00",
                        found_all=1, docs=[])
        storage.record_positions(r2, ["example.ru"], samples={"example.ru": [1, 1, 2]},
                                 override={"example.ru": 1})
        rows = storage.latest_positions("example.ru")
        row = next(r for r in rows if r["query"] == "реальный рост")
        self.assertEqual(row["delta"], 9)
        self.assertFalse(row["within_noise"])

    def test_absence_in_top_is_null_not_a_missing_value(self) -> None:
        storage.init_db()
        r = SerpResult(query="нет в топе", region=225, fetched_at="2024-01-01T00:00:00+00:00",
                       found_all=0, docs=[])
        storage.record_positions(r, ["example.ru"], override={"example.ru": None})
        rows = storage.latest_positions("example.ru")
        row = next(r for r in rows if r["query"] == "нет в топе")
        self.assertIsNone(row["position"])
        self.assertEqual(row["measurements"], 1)


if __name__ == "__main__":
    unittest.main()
