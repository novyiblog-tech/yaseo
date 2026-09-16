"""
Тесты `yaseo.yandex_serp`: разбор XML выдачи из обезличенной фикстуры и
сетевой путь (search_async → wait_operation → serp) с подменённым opener —
без единого реального обращения к Яндексу.
"""
from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

from tests.helpers import FakeOpener, FakeResponse
from yaseo import yandex_serp

ENV = {"YC_FOLDER_ID": "folder-1", "YANDEX_AI_STUDIO_API_KEY": "key-1"}

#: Обезличенная выдача в формате, который разбирает `parse_serp`: два
#: органических документа на разных доменах и один блок-колдунщик
#: («картинки по запросу…») между ними — по правилам он не должен
#: получить organic_position и не должен считаться конкурентом.
FIXTURE_XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0">
  <response>
    <found priority="all">4</found>
    <results>
      <grouping>
        <group>
          <categ name="example.ru"/>
          <doccount>12</doccount>
          <doc>
            <url>https://example.ru/okna/</url>
            <domain>example.ru</domain>
            <title>Пластиковые <hlword>окна</hlword> в Москве</title>
            <modtime>20240115T101500</modtime>
            <passages><passage>Продажа и установка</passage></passages>
          </doc>
        </group>
        <group>
          <categ name="картинки"/>
          <doccount>1</doccount>
          <doc>
            <url>https://yandex.ru/images</url>
            <domain>yandex.ru</domain>
            <title>картинки по запросу «пластиковые окна»</title>
          </doc>
        </group>
        <group>
          <categ name="example.com"/>
          <doccount>3</doccount>
          <doc>
            <url>https://example.com/windows</url>
            <domain>example.com</domain>
            <title>Windows shop</title>
          </doc>
        </group>
      </grouping>
    </results>
  </response>
</yandexsearch>
"""


class ParseSerpTests(unittest.TestCase):
    def test_wizard_block_excluded_from_organic_numbering(self) -> None:
        res = yandex_serp.parse_serp(FIXTURE_XML, "пластиковые окна")
        self.assertIsNone(res.error)
        self.assertEqual(res.found_all, 4)
        self.assertEqual([d.domain for d in res.docs], ["example.ru", "yandex.ru", "example.com"])

        first, wizard, third = res.docs
        self.assertFalse(first.is_wizard)
        self.assertEqual(first.organic_position, 1)
        self.assertEqual(first.title_hlwords, 1)
        self.assertEqual(first.domain_doccount, 12)
        self.assertEqual(first.url_depth, 1)

        self.assertTrue(wizard.is_wizard)
        self.assertIsNone(wizard.organic_position)
        self.assertEqual(wizard.position, 2)  # место в выдаче у него есть

        self.assertFalse(third.is_wizard)
        self.assertEqual(third.organic_position, 2)  # а в органике блок колдунщика не считается
        self.assertEqual(third.position, 3)

    def test_error_response_sets_error_field_and_zero_found(self) -> None:
        xml = (
            '<?xml version="1.0"?><yandexsearch><response>'
            '<error code="15">Bad request</error></response></yandexsearch>'
        )
        res = yandex_serp.parse_serp(xml, "запрос")
        self.assertEqual(res.error, "Bad request")
        self.assertEqual(res.found_all, 0)
        self.assertEqual(res.docs, [])


class NetworkMockedTests(unittest.TestCase):
    """Каждый тест подменяет `yandex_serp.opener`, реальная сеть не участвует."""

    def test_search_async_returns_operation_id(self) -> None:
        fake = FakeOpener([FakeResponse(json.dumps({"id": "op-1"}).encode())])
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            op_id = yandex_serp.search_async("окна", env=ENV)
        self.assertEqual(op_id, "op-1")

    def test_search_async_request_body_carries_query_and_paging(self) -> None:
        fake = FakeOpener([FakeResponse(json.dumps({"id": "op-2"}).encode())])
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            yandex_serp.search_async("окна", n=25, page=2, env=ENV)
        body = json.loads(fake.requests[0].data.decode("utf-8"))
        self.assertEqual(body["query"]["queryText"], "окна")
        self.assertEqual(body["query"]["page"], "2")
        # groupsOnPage должен нести именно запрошенную глубину этой страницы.
        self.assertEqual(body["groupSpec"]["groupsOnPage"], "25")
        self.assertEqual(body["folderId"], "folder-1")

    def test_search_async_without_operation_id_raises(self) -> None:
        fake = FakeOpener([FakeResponse(json.dumps({"no_id_here": True}).encode())])
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            with self.assertRaises(RuntimeError):
                yandex_serp.search_async("окна", env=ENV)

    def test_wait_operation_decodes_base64_xml_when_done(self) -> None:
        payload = {
            "done": True,
            "response": {"rawData": base64.b64encode(FIXTURE_XML.encode("utf-8")).decode()},
        }
        fake = FakeOpener([FakeResponse(json.dumps(payload).encode())])
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            xml = yandex_serp.wait_operation("op-1", env=ENV, timeout=1, poll=0.01)
        self.assertIn("<yandexsearch", xml)

    def test_wait_operation_raises_on_operation_error(self) -> None:
        payload = {"done": True, "error": {"code": 7, "message": "boom"}}
        fake = FakeOpener([FakeResponse(json.dumps(payload).encode())])
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            with self.assertRaises(RuntimeError):
                yandex_serp.wait_operation("op-1", env=ENV, timeout=1, poll=0.01)

    def test_serp_end_to_end_with_mocked_transport(self) -> None:
        responses = [
            FakeResponse(json.dumps({"id": "op-1"}).encode()),
            FakeResponse(
                json.dumps(
                    {
                        "done": True,
                        "response": {
                            "rawData": base64.b64encode(FIXTURE_XML.encode("utf-8")).decode()
                        },
                    }
                ).encode()
            ),
        ]
        fake = FakeOpener(responses)
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            res = yandex_serp.serp("пластиковые окна", env=ENV)
        self.assertIsNone(res.error)
        self.assertEqual(len(res.docs), 3)

    def test_serp_wraps_http_error_instead_of_raising(self) -> None:
        import io
        import urllib.error

        err = urllib.error.HTTPError(
            url="https://x", code=403, msg="Forbidden", hdrs=None, fp=io.BytesIO(b"denied")
        )
        fake = FakeOpener([err])
        with mock.patch.object(yandex_serp, "opener", return_value=fake):
            res = yandex_serp.serp("окна", env=ENV)
        self.assertIsNotNone(res.error)
        self.assertIn("403", res.error)
        self.assertEqual(res.docs, [])


if __name__ == "__main__":
    unittest.main()
