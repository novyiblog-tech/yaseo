"""
Тесты `yaseo.estimate`: тарифы из встроенного `rates.json`, подмена
`YASEO_RATES`/одиночной ставки переменной окружения, и обязательные четыре
ответа сметы текстом/одной строкой/HTML.
"""
from __future__ import annotations

import json
import os
import unittest

from tests.helpers import IsolatedTestCase
from yaseo import estimate


class RatesResolutionTests(IsolatedTestCase):
    def test_default_rate_comes_from_package_rates_json(self) -> None:
        rate, source = estimate.rate("search-api")
        self.assertAlmostEqual(rate, 0.0305)
        self.assertIn("rates.json", source)

    def test_wordstat_default_rate(self) -> None:
        rate, _ = estimate.rate("wordstat")
        self.assertAlmostEqual(rate, 0.020)

    def test_yaseo_rates_file_overrides_package_file(self) -> None:
        custom = self.paths["home"] / "custom_rates.json"
        custom.write_text(json.dumps({"ставки": {"search-api": 1.5}}), encoding="utf-8")
        os.environ["YASEO_RATES"] = str(custom)
        rate, source = estimate.rate("search-api")
        self.assertEqual(rate, 1.5)
        self.assertIn("custom_rates.json", source)

    def test_single_rate_env_var_wins_over_file(self) -> None:
        custom = self.paths["home"] / "custom_rates.json"
        custom.write_text(json.dumps({"ставки": {"search-api": 1.5}}), encoding="utf-8")
        os.environ["YASEO_RATES"] = str(custom)
        os.environ["YASEO_RATE_SEARCH_API"] = "2.5"
        rate, source = estimate.rate("search-api")
        self.assertEqual(rate, 2.5)
        self.assertEqual(source, "переменная YASEO_RATE_SEARCH_API")

    def test_missing_rates_file_means_rate_not_set(self) -> None:
        os.environ["YASEO_RATES"] = str(self.paths["home"] / "does-not-exist.json")
        rate, source = estimate.rate("search-api")
        self.assertIsNone(rate)
        self.assertEqual(source, "ставка не задана")

    def test_non_numeric_rate_is_treated_as_unset(self) -> None:
        os.environ["YASEO_RATE_WORDSTAT"] = "не число"
        rate, source = estimate.rate("wordstat")
        self.assertIsNone(rate)
        self.assertEqual(source, "ставка не задана")

    def test_unknown_source_key_alias_is_translated(self) -> None:
        # "движок" — старое имя, должно резолвиться так же, как "движок текста".
        self.assertEqual(estimate.ALIASES["движок"], "движок текста")


class EstimateTextTests(IsolatedTestCase):
    def test_paid_call_with_known_rate(self) -> None:
        e = estimate.estimate(
            "search-api", 100, project="Проект", domain="example.ru",
            из_чего="100 фраз", потолок=500,
        )
        self.assertTrue(e["платный"])
        self.assertEqual(e["обращений"], 100)
        text = estimate.text(e)
        self.assertIn("Потратит: 100 обращений", text)
        self.assertIn("100 фраз", text)
        self.assertIn("Стоимость:", text)
        self.assertIn("Потолок за прогон: 500", text)
        self.assertIn("example.ru", text)

    def test_free_named_source(self) -> None:
        e = estimate.estimate("нет", 0)
        self.assertFalse(e["платный"])
        self.assertEqual(estimate.text(e).splitlines()[0], "Ничего не потрачено")

    def test_zero_calls_on_paid_source_is_still_free(self) -> None:
        e = estimate.estimate("search-api", 0)
        self.assertFalse(e["платный"])
        self.assertIn("ноль", estimate.text(e))

    def test_over_cap_is_named_explicitly(self) -> None:
        e = estimate.estimate("search-api", 1000, потолок=10)
        self.assertTrue(e["за_потолком"])
        self.assertIn("остановлен", estimate.text(e))

    def test_no_cap_is_named_explicitly_for_paid_calls(self) -> None:
        e = estimate.estimate("search-api", 10)
        self.assertIn("Потолок за прогон: не задан", estimate.text(e))

    def test_html_escapes_untrusted_content(self) -> None:
        e = estimate.estimate("search-api", 1, project="<script>alert(1)</script>")
        html = estimate.html(e)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_one_line_mentions_calls_source_and_asks_to_continue(self) -> None:
        e = estimate.estimate("search-api", 42, из_чего="42 фразы")
        line = estimate.one_line(e)
        self.assertIn("42", line)
        self.assertIn("Yandex Search API", line)
        self.assertIn("Продолжить?", line)

    def test_one_line_for_free_source_still_asks_to_continue(self) -> None:
        e = estimate.estimate("нет", 0)
        line = estimate.one_line(e)
        self.assertIn("Продолжить?", line)

    def test_calls_word_pluralisation(self) -> None:
        self.assertEqual(estimate.calls_word(1), "1 обращение")
        self.assertEqual(estimate.calls_word(2), "2 обращения")
        self.assertEqual(estimate.calls_word(5), "5 обращений")
        self.assertEqual(estimate.calls_word(11), "11 обращений")
        self.assertEqual(estimate.calls_word(21), "21 обращение")

    def test_plural_helper(self) -> None:
        self.assertEqual(estimate.plural(1, "фраза", "фразы", "фраз"), "фраза")
        self.assertEqual(estimate.plural(2, "фраза", "фразы", "фраз"), "фразы")
        self.assertEqual(estimate.plural(5, "фраза", "фразы", "фраз"), "фраз")
        self.assertEqual(estimate.plural(11, "фраза", "фразы", "фраз"), "фраз")
        self.assertEqual(estimate.plural(21, "фраза", "фразы", "фраз"), "фраза")

    def test_unpaid_source_reports_zero_cost(self) -> None:
        e = estimate.estimate("вебмастер", 5)
        self.assertFalse(e["платный"])
        self.assertIn("платы за запрос не берёт", estimate.text(e))


class PagesForDepthTests(unittest.TestCase):
    """
    Дефект 16.09.2026: постраничный набор выдачи (n=30 → 3 страницы по 10)
    не был учтён в смете ни трекера, ни проверки статей — обе считали
    «снимков» и «запросов», а платить нужно по страницам.

    `pages_for_depth` делегирует `yandex_serp.pages_for` (не считает сам),
    поэтому здесь же проверяется и потолок глубины: n=100 должен стоить
    столько же, сколько реально потратит `serp()` при глубине, обрезанной
    до `MAX_DEPTH` (50) — не больше, иначе смета завысит цену вдвое.
    """

    def test_top_ten_is_one_page(self) -> None:
        self.assertEqual(estimate.pages_for_depth(10), 1)

    def test_top_thirty_is_three_pages(self) -> None:
        self.assertEqual(estimate.pages_for_depth(30), 3)

    def test_rounds_up_a_partial_page(self) -> None:
        self.assertEqual(estimate.pages_for_depth(11), 2)
        self.assertEqual(estimate.pages_for_depth(21), 3)

    def test_depth_below_ten_is_still_one_page(self) -> None:
        self.assertEqual(estimate.pages_for_depth(1), 1)
        self.assertEqual(estimate.pages_for_depth(0), 1)

    def test_depth_above_max_is_capped_like_serp_actually_spends(self) -> None:
        from yaseo.yandex_serp import MAX_DEPTH
        self.assertEqual(estimate.pages_for_depth(MAX_DEPTH), 5)
        self.assertEqual(estimate.pages_for_depth(100), estimate.pages_for_depth(MAX_DEPTH))


if __name__ == "__main__":
    unittest.main()
