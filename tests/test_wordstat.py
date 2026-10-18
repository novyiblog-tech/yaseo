"""
Тесты `yaseo.wordstat` (разбор ответа Wordstat) и `yaseo.wordstat_client`
(тело запроса и разбор HTTP-ошибок) — без единого обращения в сеть.
"""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from tests.helpers import FakeOpener, FakeResponse, IsolatedTestCase
from yaseo import config, wordstat, wordstat_client


class FrequencyParsingTests(unittest.TestCase):
    def test_parses_top_and_association_expansions(self) -> None:
        fake_response = {
            "totalCount": 15000,
            "results": [{"phrase": "окна пвх", "count": 5000}],
            "associations": [{"phrase": "москитные сетки", "count": 800}],
        }
        with mock.patch("yaseo.wordstat_client.top_requests", return_value=fake_response):
            f = wordstat.frequency("пластиковые окна")
        self.assertIsNone(f.error)
        self.assertEqual(f.freq, 15000)
        kinds = {e.phrase: e.kind for e in f.expansions}
        self.assertEqual(kinds["окна пвх"], "top")
        self.assertEqual(kinds["москитные сетки"], "association")
        freqs = {e.phrase: e.freq for e in f.expansions}
        self.assertEqual(freqs["окна пвх"], 5000)

    def test_error_branch_sets_error_and_none_freq(self) -> None:
        with mock.patch(
            "yaseo.wordstat_client.top_requests",
            return_value={"_error": {"reason": "квота исчерпана"}},
        ):
            f = wordstat.frequency("что угодно")
        self.assertIsNone(f.freq)
        self.assertIn("квота исчерпана", f.error)

    def test_zero_frequency_stays_zero_not_no_demand(self) -> None:
        with mock.patch(
            "yaseo.wordstat_client.top_requests",
            return_value={"totalCount": 0, "results": [], "associations": []},
        ):
            f = wordstat.frequency("очень редкий запрос")
        self.assertIsNone(f.error)
        self.assertEqual(f.freq, 0)
        self.assertIsInstance(f.freq, int)
        self.assertEqual(f.expansions, [])

    def test_expansions_filters_by_min_freq(self) -> None:
        fake_response = {
            "totalCount": 100,
            "results": [{"phrase": "a", "count": 50}, {"phrase": "b", "count": 5}],
            "associations": [],
        }
        with mock.patch("yaseo.wordstat_client.top_requests", return_value=fake_response):
            out = wordstat.expansions("q", min_freq=30)
        self.assertEqual([e.phrase for e in out], ["a"])


class LatestSnapshotTests(IsolatedTestCase):
    def test_picks_newest_file_and_skips_errors_and_garbage(self) -> None:
        snap_dir = config.snapshots_dir()
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / "wordstat-2024-01-01.jsonl").write_text(
            json.dumps({"seed": "старый", "response": {"totalCount": 1}}) + "\n",
            encoding="utf-8",
        )
        newest = snap_dir / "wordstat-2024-02-01.jsonl"
        newest.write_text(
            "\n".join(
                [
                    json.dumps({"seed": "новый", "response": {"totalCount": 500}}),
                    json.dumps({"seed": "ошибка", "response": {"_error": {"reason": "x"}}}),
                    "не json вовсе",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        path, freqs = wordstat.latest_snapshot()
        self.assertEqual(path, newest)
        self.assertEqual(freqs, {"новый": 500})

    def test_no_snapshots_dir_returns_empty(self) -> None:
        path, freqs = wordstat.latest_snapshot()
        self.assertIsNone(path)
        self.assertEqual(freqs, {})


class WordstatClientTests(unittest.TestCase):
    ENV = {"YC_FOLDER_ID": "folder-1", "YANDEX_AI_STUDIO_API_KEY": "key-1"}

    def test_top_requests_builds_expected_body_and_parses_json(self) -> None:
        payload = {"totalCount": 10, "results": []}
        fake = FakeOpener([FakeResponse(json.dumps(payload).encode())])
        with mock.patch.object(wordstat_client, "opener", return_value=fake):
            res = wordstat_client.top_requests("окна", regions=["213"], num=50, env=self.ENV)
        self.assertEqual(res, payload)
        body = json.loads(fake.requests[0].data.decode("utf-8"))
        self.assertEqual(body["phrase"], "окна")
        self.assertEqual(body["folderId"], "folder-1")
        self.assertEqual(body["regions"], ["213"])
        self.assertEqual(body["numPhrases"], 50)
        self.assertEqual(
            fake.requests[0].get_header("Authorization"), "Api-Key key-1"
        )

    def test_http_error_becomes_error_dict_not_exception(self) -> None:
        err = urllib.error.HTTPError(
            url="https://x", code=429, msg="Too Many Requests",
            hdrs=None, fp=io.BytesIO(b"limit exceeded"),
        )
        fake = FakeOpener([err])
        with mock.patch.object(wordstat_client, "opener", return_value=fake):
            res = wordstat_client.top_requests("окна", env=self.ENV)
        self.assertIn("_error", res)
        self.assertEqual(res["_error"]["status"], 429)

    def test_url_error_becomes_error_dict(self) -> None:
        fake = FakeOpener([urllib.error.URLError("не разрешилось имя хоста")])
        with mock.patch.object(wordstat_client, "opener", return_value=fake):
            res = wordstat_client.top_requests("окна", env=self.ENV)
        self.assertIn("_error", res)
        self.assertIn("reason", res["_error"])


class DynamicsRegionsClientTests(unittest.TestCase):
    ENV = {"YC_FOLDER_ID": "folder-1", "YANDEX_AI_STUDIO_API_KEY": "key-1"}

    def _call(self, payload, fn, *a, **kw):
        fake = FakeOpener([FakeResponse(json.dumps(payload).encode())])
        with mock.patch.object(wordstat_client, "opener", return_value=fake):
            res = fn(*a, env=self.ENV, **kw)
        return res, fake

    def test_dynamics_body_and_endpoint(self) -> None:
        payload = {"results": [{"date": "2026-01-01T00:00:00Z", "count": "100", "share": "0.0001"}]}
        res, fake = self._call(
            payload, wordstat_client.dynamics, "окна", "2026-01-01T00:00:00Z",
            period="PERIOD_WEEKLY", regions=["213"],
        )
        self.assertEqual(res, payload)
        req = fake.requests[0]
        self.assertTrue(req.full_url.endswith("/v2/wordstat/dynamics"))
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["period"], "PERIOD_WEEKLY")
        self.assertEqual(body["fromDate"], "2026-01-01T00:00:00Z")
        self.assertEqual(body["regions"], ["213"])
        self.assertEqual(body["folderId"], "folder-1")
        self.assertNotIn("toDate", body)

    def test_dynamics_sends_to_date_when_given(self) -> None:
        _, fake = self._call(
            {"results": []}, wordstat_client.dynamics, "окна",
            "2026-01-01T00:00:00Z", to_date="2026-06-30T23:59:59Z",
        )
        body = json.loads(fake.requests[0].data.decode("utf-8"))
        self.assertEqual(body["toDate"], "2026-06-30T23:59:59Z")

    def test_bad_input_is_refused_before_network(self) -> None:
        fake = FakeOpener([])
        with mock.patch.object(wordstat_client, "opener", return_value=fake):
            for res in (
                wordstat_client.dynamics("окна", "2026-01-01T00:00:00Z", period="MONTH", env=self.ENV),
                wordstat_client.dynamics("x" * 401, "2026-01-01T00:00:00Z", env=self.ENV),
                wordstat_client.dynamics("окна", "", env=self.ENV),
                wordstat_client.dynamics("окна", "2026-01-01T00:00:00Z", regions=["1"] * 101, env=self.ENV),
                wordstat_client.regions_distribution("окна", region="ALL", env=self.ENV),
                wordstat_client.regions_distribution("", env=self.ENV),
            ):
                self.assertIn("_error", res)
        self.assertEqual(fake.requests, [])

    def test_regions_distribution_body_and_endpoint(self) -> None:
        res, fake = self._call(
            {"results": []}, wordstat_client.regions_distribution, "окна",
            region="REGION_CITIES",
        )
        req = fake.requests[0]
        self.assertTrue(req.full_url.endswith("/v2/wordstat/regions"))
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["region"], "REGION_CITIES")
        self.assertEqual(body["devices"], ["DEVICE_ALL"])

    def test_regions_tree_sends_only_folder(self) -> None:
        _, fake = self._call({"regions": []}, wordstat_client.regions_tree)
        req = fake.requests[0]
        self.assertTrue(req.full_url.endswith("/v2/wordstat/getRegionsTree"))
        self.assertEqual(json.loads(req.data.decode("utf-8")), {"folderId": "folder-1"})

    def test_http_error_becomes_error_dict(self) -> None:
        err = urllib.error.HTTPError(
            url="https://x", code=429, msg="Too Many Requests",
            hdrs=None, fp=io.BytesIO(b"limit"),
        )
        with mock.patch.object(wordstat_client, "opener", return_value=FakeOpener([err])):
            res = wordstat_client.regions_tree(env=self.ENV)
        self.assertEqual(res["_error"]["status"], 429)


class DynamicsRegionsParsingTests(unittest.TestCase):
    def test_dynamics_parses_string_numbers(self) -> None:
        resp = {"results": [
            {"date": "2026-01-01T00:00:00Z", "count": "1200", "share": "0.00031"},
            {"date": "2026-02-01T00:00:00Z", "count": "", "share": None},
        ]}
        with mock.patch("yaseo.wordstat_client.dynamics", return_value=resp):
            d = wordstat.dynamics("окна", "2026-01-01T00:00:00Z")
        self.assertIsNone(d.error)
        self.assertEqual([p.count for p in d.points], [1200, 0])
        self.assertAlmostEqual(d.points[0].share, 0.00031)
        self.assertIsNone(d.points[1].share)

    def test_dynamics_error_is_kept(self) -> None:
        with mock.patch("yaseo.wordstat_client.dynamics", return_value={"_error": {"reason": "квота"}}):
            d = wordstat.dynamics("окна", "2026-01-01T00:00:00Z")
        self.assertIn("квота", d.error)
        self.assertEqual(d.points, [])

    def test_regions_parses_affinity(self) -> None:
        resp = {"results": [{"region": "213", "count": "500", "share": "0.2", "affinityIndex": "1.8"}]}
        with mock.patch("yaseo.wordstat_client.regions_distribution", return_value=resp):
            r = wordstat.regions("окна")
        self.assertEqual((r.regions[0].region, r.regions[0].count), ("213", 500))
        self.assertAlmostEqual(r.regions[0].affinity, 1.8)

    def test_region_names_flattens_tree_and_survives_error(self) -> None:
        tree = {"regions": [{"id": "225", "label": "Россия", "children": [
            {"id": "213", "label": "Москва", "children": []}]}]}
        with mock.patch("yaseo.wordstat_client.regions_tree", return_value=tree):
            self.assertEqual(wordstat.region_names(), {"225": "Россия", "213": "Москва"})
        with mock.patch("yaseo.wordstat_client.regions_tree", return_value={"_error": {}}):
            self.assertEqual(wordstat.region_names(), {})


if __name__ == "__main__":
    unittest.main()
