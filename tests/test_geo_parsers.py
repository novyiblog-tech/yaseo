"""
Разбор ответов ИИ-провайдеров на примерах из официальной справки.

Фикстуры — tests/fixtures/geo/<провайдер>.json, откуда взят каждый пример и что
в нём поправлено — в соседнем <провайдер>.source.txt. Домены заменены на
example.ru; absent-domain.ru — отрицательный контроль.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from yaseo.geo import domains as D
from yaseo.geo import providers as P

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "geo"
ABSENT = "absent-domain.ru"

# провайдер: (источников, использовано, [(домен, процитирован, в источниках, место)])
EXPECT = {
    "yandex": (10, 3, [("example.ru", True, True, 2),
                       ("ru.wikipedia.org", False, True, None),   # есть, но used=false
                       (ABSENT, False, False, None)]),
    "perplexity": (1, 1, [("example.ru", True, True, 1), (ABSENT, False, False, None)]),
    "openai": (1, 1, [("example.ru", True, True, 1), (ABSENT, False, False, None)]),
    "gemini": (2, 2, [("example.ru", True, True, 1), ("uefa.com", True, True, 2),
                      (ABSENT, False, False, None)]),
    "anthropic": (1, 1, [("example.ru", True, True, 1), (ABSENT, False, False, None)]),
}

DOMAIN_CASES = [
    ("https://blog.example.ru/a", "example.ru", "site", True),
    ("https://blog.example.ru/a", "example.ru", "host", False),
    ("https://www.example.ru/", "example.ru", "host", True),
    ("https://example.ru.evil.com/", "example.ru", "site", False),
    ("https://notexample.ru/", "example.ru", "site", False),
    ("https://shop.example.co.uk/", "example.co.uk", "site", True),
    ("https://other.co.uk/", "example.co.uk", "site", False),
    ("https://xn--e1afmkfd.xn--p1ai/", "пример.рф", "site", True),
    ("https://a.blog.example.ru/", "blog.example.ru", "site", True),
    ("https://shop.example.ru/", "blog.example.ru", "site", False),
    ("EXAMPLE.RU", "example.ru", "host", True),
]


class ProviderParserTests(unittest.TestCase):
    def test_every_provider_has_a_fixture_and_a_source_note(self) -> None:
        for name in EXPECT:
            self.assertTrue((FIXTURES / f"{name}.json").is_file(), name)
            self.assertTrue((FIXTURES / f"{name}.source.txt").is_file(), name)
        self.assertEqual(set(EXPECT), set(P.PARSERS))

    def test_parsers_on_documented_examples(self) -> None:
        for name, (n_src, n_used, cases) in EXPECT.items():
            raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
            for dom, cited, in_src, pos in cases:
                with self.subTest(provider=name, domain=dom):
                    a = P.PARSERS[name](raw, "q", dom)
                    self.assertEqual(len(a.sources), n_src)
                    self.assertEqual(a.used_count, n_used)
                    self.assertIs(a.cited, cited)
                    self.assertIs(a.in_sources, in_src)
                    self.assertEqual(a.cited_position, pos)
                    self.assertTrue(a.text)


class DomainMatchTests(unittest.TestCase):
    def test_domain_matching(self) -> None:
        for url, dom, mode, want in DOMAIN_CASES:
            with self.subTest(url=url, domain=dom, mode=mode):
                self.assertIs(D.matches(url, dom, mode), want)


if __name__ == "__main__":
    unittest.main()
