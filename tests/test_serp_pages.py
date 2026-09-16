"""
Глубина выдачи набирается страницами по 10, а не раздутым groupsOnPage.

Сеть подменена `FakeOpener`: запросы уходят в очередь ответов, тела запросов
проверяются по записанным `requests`.
"""
from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

from tests.helpers import FakeOpener, FakeResponse
from yaseo import yandex_serp

ENV = {"YC_FOLDER_ID": "folder-1", "YANDEX_AI_STUDIO_API_KEY": "key-1"}


def _group(url: str, domain: str, title: str) -> str:
    return (f"<group><categ name=\"{domain}\"/><doccount>1</doccount>"
            f"<doc><url>{url}</url><domain>{domain}</domain><title>{title}</title></doc></group>")


def _page_xml(page: int, wizard_at: int | None = None) -> str:
    groups = []
    for i in range(10):
        if wizard_at is not None and i == wizard_at:
            groups.append(_group("https://yandex.ru/images", "yandex.ru",
                                 "картинки по запросу «окна»"))
        else:
            groups.append(_group(f"https://site{page}-{i}.ru/", f"site{page}-{i}.ru",
                                 f"Сайт {page}-{i}"))
    return ("<?xml version=\"1.0\" encoding=\"utf-8\"?><yandexsearch><response>"
            "<found priority=\"all\">1000</found><results><grouping>"
            + "".join(groups) + "</grouping></results></response></yandexsearch>")


def _op(op_id: str) -> FakeResponse:
    return FakeResponse(json.dumps({"id": op_id}).encode())


def _done(xml: str) -> FakeResponse:
    raw = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    return FakeResponse(json.dumps({"done": True, "response": {"rawData": raw}}).encode())


class PagesForTests(unittest.TestCase):
    def test_cost_in_requests(self) -> None:
        self.assertEqual(yandex_serp.pages_for(10), 1)
        self.assertEqual(yandex_serp.pages_for(5), 1)
        self.assertEqual(yandex_serp.pages_for(11), 2)
        self.assertEqual(yandex_serp.pages_for(20), 2)
        self.assertEqual(yandex_serp.pages_for(30), 3)
        self.assertEqual(yandex_serp.pages_for(50), 5)

    def test_depth_is_capped(self) -> None:
        self.assertEqual(yandex_serp.MAX_DEPTH, 50)
        self.assertEqual(yandex_serp.pages_for(100), 5)
        self.assertEqual(yandex_serp.pages_for(0), 1)


class SerpPagingTests(unittest.TestCase):
    def test_three_pages_give_thirty_docs_with_through_positions(self) -> None:
        fake = FakeOpener([
            _op("op-0"), _op("op-1"), _op("op-2"),
            _done(_page_xml(0)), _done(_page_xml(1, wizard_at=2)), _done(_page_xml(2)),
        ])
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            res = yandex_serp.serp("окна", n=30, env=ENV)

        self.assertIsNone(res.error)
        self.assertEqual(len(fake.requests), 6, "3 страницы = 3 постановки + 3 чтения")

        bodies = [json.loads(r.data.decode()) for r in fake.requests[:3]]
        self.assertEqual([b["query"]["page"] for b in bodies], ["0", "1", "2"])
        self.assertEqual({b["groupSpec"]["groupsOnPage"] for b in bodies}, {"10"})

        self.assertEqual(len(res.docs), 30)
        self.assertEqual([d.position for d in res.docs], list(range(1, 31)))

        # Первая страница — органика 1..10.
        self.assertEqual([d.organic_position for d in res.docs[:10]], list(range(1, 11)))
        # Вторая: 11, 12, колдунщик, 13 …
        wizard = res.docs[12]
        self.assertTrue(wizard.is_wizard)
        self.assertEqual(wizard.position, 13)
        self.assertIsNone(wizard.organic_position)
        self.assertEqual(res.docs[11].organic_position, 12)
        self.assertEqual(res.docs[13].organic_position, 13)
        # Колдунщик не сдвинул нумерацию третьей страницы: 20..29 без дыр.
        self.assertEqual([d.organic_position for d in res.docs[20:]], list(range(20, 30)))
        self.assertEqual(res.docs[20].domain, "site2-0.ru")

    def test_depth_fifteen_costs_two_requests_and_trims(self) -> None:
        fake = FakeOpener([_op("a"), _op("b"), _done(_page_xml(0)), _done(_page_xml(1))])
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            res = yandex_serp.serp("окна", n=15, env=ENV)
        self.assertEqual(len(fake.requests), 4)
        self.assertEqual(len(res.docs), 15)
        self.assertEqual(res.docs[-1].position, 15)

    def test_depth_over_cap_is_limited(self) -> None:
        fake = FakeOpener([_op(f"o{i}") for i in range(5)]
                          + [_done(_page_xml(i)) for i in range(5)])
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            res = yandex_serp.serp("окна", n=500, env=ENV)
        self.assertEqual(len(fake.requests), 10, "потолок 50 = 5 страниц")
        self.assertEqual(len(res.docs), 50)

    def test_repeated_doc_on_next_page_counted_once(self) -> None:
        dup_page = _page_xml(1).replace("https://site1-0.ru/", "https://site0-9.ru/")
        fake = FakeOpener([_op("a"), _op("b"), _done(_page_xml(0)), _done(dup_page)])
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            res = yandex_serp.serp("окна", n=20, env=ENV)
        urls = [d.url for d in res.docs]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(len(res.docs), 19)
        # Повтор занял 11-е место: нумерация Яндекса сохранена, хвост не съехал.
        self.assertEqual([d.position for d in res.docs], list(range(1, 11)) + list(range(12, 21)))
        self.assertEqual(res.docs[-1].organic_position, 20)

    def test_failed_second_page_marks_result_as_error(self) -> None:
        fake = FakeOpener([_op("a"), _op("b"), _op("c"),
                           _done(_page_xml(0)), RuntimeError("сбой"), _done(_page_xml(2))])
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            res = yandex_serp.serp("окна", n=30, env=ENV)
        self.assertIsNotNone(res.error, "неполная глубина не должна выглядеть как полная")
        self.assertIn("страница 2 из 3", res.error)
        self.assertEqual(len(res.docs), 10)

    def test_batch_submits_every_page(self) -> None:
        fake = FakeOpener([_op("a0"), _op("a1"), _op("b0"), _op("b1"),
                           _done(_page_xml(0)), _done(_page_xml(1)),
                           _done(_page_xml(0)), _done(_page_xml(1))])
        with mock.patch.object(yandex_serp, "opener", return_value=fake), \
                mock.patch.object(yandex_serp, "load_env", return_value=ENV), \
                mock.patch("time.sleep", lambda *_: None):
            out = yandex_serp.serp_batch(["окна", "двери"], n=20, progress=False)
        self.assertEqual(len(fake.requests), 8, "2 запроса × 2 страницы = 4 обращения")
        self.assertEqual([len(r.docs) for r in out], [20, 20])
        self.assertTrue(all(r.error is None for r in out))


if __name__ == "__main__":
    unittest.main()
