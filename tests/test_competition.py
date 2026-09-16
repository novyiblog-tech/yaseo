"""
Тесты `yaseo.competition`: балл и факторы на фиксированной выдаче должны
быть детерминированы (тот же вход — то же число, с точностью до округления
в самом модуле), и у каждого фактора должно быть доказательство — не голое
число.
"""
from __future__ import annotations

import unittest

from yaseo import competition as comp_mod
from yaseo.yandex_serp import SerpDoc, SerpResult

QUERY = "окна пластиковые"  # 2 значимых слова длиннее 2 символов


def _doc(position, domain, hlwords, url_depth, doccount, modtime, is_wizard=False):
    return SerpDoc(
        position=position,
        organic_position=None if is_wizard else position,
        is_wizard=is_wizard,
        url=f"https://{domain}/p{position}/",
        domain=domain,
        title="заголовок страницы",
        title_hlwords=hlwords,
        url_depth=url_depth,
        domain_doccount=doccount,
        modtime=modtime,
        passage=None,
    )


def _fresh(offset_days: int = 0) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.now(timezone.utc) - timedelta(days=offset_days)
    return dt.strftime("%Y%m%dT%H%M%S")


def _stale_years(years: int) -> str:
    from datetime import datetime, timezone

    dt = datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year - years)
    return dt.strftime("%Y%m%dT%H%M%S")


# Пять документов, подобранных так, чтобы каждый фактор считался вручную:
#   title_fit = (1.0+0.5+0.0+1.0+0.5)/5 = 0.6, «полных» вхождений — 2 из 5
#   depth_coverage: медиана doccount = 20 -> log10(21)/3 ≈ 0.44074
#   portal_share: 1 портал (vk.com) из 5 -> 0.2
#   homepage_share: 2 главные страницы (глубина 0) из 5 -> 0.4
#   freshness: из 4 документов с датой (одному датой не пришло) свежих 3 -> 0.75
# Итоговый балл — 48.2, полоса «средняя» (посчитано отдельно и сверено с
# реализацией на момент написания теста).
def _fixture_docs():
    return [
        _doc(1, "example.ru", hlwords=2, url_depth=1, doccount=50, modtime=_fresh(10)),
        _doc(2, "vk.com", hlwords=1, url_depth=0, doccount=1000, modtime=_fresh(20)),
        _doc(3, "another.ru", hlwords=0, url_depth=2, doccount=5, modtime=_stale_years(3)),
        _doc(4, "site4.ru", hlwords=2, url_depth=0, doccount=20, modtime=None),
        _doc(5, "site5.ru", hlwords=1, url_depth=3, doccount=2, modtime=_fresh(5)),
    ]


def _fixture_result(**kw) -> SerpResult:
    return SerpResult(
        query=QUERY, region=225, fetched_at="2024-01-01T00:00:00+00:00",
        found_all=5000, docs=_fixture_docs(), **kw,
    )


class ScoreSerpDeterminismTests(unittest.TestCase):
    def test_score_is_deterministic_across_calls(self) -> None:
        c1 = comp_mod.score_serp(_fixture_result())
        c2 = comp_mod.score_serp(_fixture_result())
        self.assertEqual(c1.score, c2.score)
        self.assertEqual(c1.band, c2.band)

    def test_score_and_band_match_hand_computed_value(self) -> None:
        c = comp_mod.score_serp(_fixture_result())
        self.assertEqual(c.score, 48.2)
        self.assertEqual(c.band, "средняя")
        self.assertEqual(c.docs_analysed, 5)
        self.assertEqual(c.docs_excluded, 0)

    def test_every_factor_carries_evidence(self) -> None:
        c = comp_mod.score_serp(_fixture_result())
        by_name = {f.name: f for f in c.factors}
        self.assertEqual(set(by_name), set(comp_mod.WEIGHTS))
        for f in c.factors:
            self.assertTrue(f.evidence, f"фактор {f.name} без доказательства")
            self.assertGreater(len(f.evidence), 5)

        self.assertIn("2 из 5", by_name["title_fit"].evidence)
        self.assertEqual(by_name["title_fit"].value, 0.6)
        self.assertIn("20", by_name["depth_coverage"].evidence)
        self.assertAlmostEqual(by_name["depth_coverage"].value, 0.441, places=3)
        self.assertIn("vk.com", by_name["portal_share"].evidence)
        self.assertEqual(by_name["portal_share"].value, 0.2)
        self.assertIn("2 из 5", by_name["homepage_share"].evidence)
        self.assertEqual(by_name["homepage_share"].value, 0.4)
        self.assertIn("3 из 4", by_name["freshness"].evidence)
        self.assertEqual(by_name["freshness"].value, 0.75)

    def test_band_thresholds(self) -> None:
        self.assertEqual(comp_mod._band(29.9), "низкая")
        self.assertEqual(comp_mod._band(30), "средняя")
        self.assertEqual(comp_mod._band(60), "средняя")
        self.assertEqual(comp_mod._band(60.1), "высокая")


class OurPositionTests(unittest.TestCase):
    def test_our_domain_position_found(self) -> None:
        c = comp_mod.score_serp(_fixture_result(), our_domain="example.ru")
        self.assertEqual(c.our_position, 1)

    def test_our_domain_absent_gives_none(self) -> None:
        c = comp_mod.score_serp(_fixture_result(), our_domain="notpresent.ru")
        self.assertIsNone(c.our_position)

    def test_wizard_block_never_counts_as_our_position(self) -> None:
        # Наш домен встречается ТОЛЬКО в блоке-колдунщике — позиции быть не должно.
        docs = _fixture_docs()
        docs[0] = _doc(1, "example.ru", hlwords=0, url_depth=0, doccount=1, modtime=None, is_wizard=True)
        c = comp_mod.score_serp(
            SerpResult(query=QUERY, region=225, fetched_at="x", found_all=10, docs=docs),
            our_domain="example.ru",
        )
        self.assertIsNone(c.our_position)


class TopSlicingTests(unittest.TestCase):
    def test_top_parameter_limits_analysed_docs(self) -> None:
        c = comp_mod.score_serp(_fixture_result(), top=3)
        self.assertEqual(c.docs_analysed, 3)


class EdgeCaseTests(unittest.TestCase):
    def test_error_result_gives_zero_score_and_no_data_band(self) -> None:
        res = SerpResult(query=QUERY, region=225, fetched_at="x", found_all=0, docs=[], error="сбой сети")
        c = comp_mod.score_serp(res)
        self.assertEqual(c.score, 0.0)
        self.assertEqual(c.band, "нет данных")
        self.assertEqual(c.error, "сбой сети")

    def test_only_wizard_docs_gives_no_usable_documents_error(self) -> None:
        docs = [_doc(1, "yandex.ru", 0, 0, 1, None, is_wizard=True)]
        res = SerpResult(query=QUERY, region=225, fetched_at="x", found_all=1, docs=docs)
        c = comp_mod.score_serp(res)
        self.assertEqual(c.score, 0.0)
        self.assertEqual(c.band, "нет данных")
        self.assertIn("нет пригодных документов", c.error)


class SerpCompetitorsTests(unittest.TestCase):
    def test_repeated_domain_across_queries_is_a_competitor(self) -> None:
        docs_a = [_doc(1, "rival.ru", 1, 0, 10, None), _doc(2, "example.ru", 1, 1, 5, None)]
        docs_b = [_doc(1, "example.ru", 1, 1, 5, None), _doc(2, "rival.ru", 1, 0, 10, None)]
        serps = [
            SerpResult(query="a", region=225, fetched_at="x", found_all=2, docs=docs_a),
            SerpResult(query="b", region=225, fetched_at="x", found_all=2, docs=docs_b),
        ]
        stats = comp_mod.serp_competitors(serps)
        by_domain = {s.domain: s for s in stats}
        self.assertEqual(by_domain["rival.ru"].appearances, 2)
        self.assertEqual(by_domain["rival.ru"].best_position, 1)
        self.assertEqual(by_domain["example.ru"].appearances, 2)
        self.assertFalse(by_domain["rival.ru"].is_portal)

    def test_failed_serp_is_skipped(self) -> None:
        serps = [SerpResult(query="a", region=225, fetched_at="x", found_all=0, docs=[], error="боль")]
        self.assertEqual(comp_mod.serp_competitors(serps), [])


if __name__ == "__main__":
    unittest.main()
