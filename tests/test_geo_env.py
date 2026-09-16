"""
Дефект 16.09.2026: ключи ИИ-провайдеров GEO (Perplexity, OpenAI, Gemini,
Anthropic), заданные только переменной окружения, читались как «не заданы» —
`yaseo.env.read_all()` подхватывал из окружения процесса только ключи Яндекса
и `YASEO_*`. Здесь — положительный контроль через MCP-инструмент
`geo_providers`, которым это было обнаружено.
"""
from __future__ import annotations

import os
import unittest

from tests.helpers import IsolatedTestCase
from yaseo import mcp_server


def _call(name: str, arguments: dict | None = None) -> dict:
    return mcp_server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })


def _provider_row(text: str, name: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"| {name} "))


class GeoProvidersKeySourceTests(IsolatedTestCase):
    def test_key_only_in_process_env_is_seen_as_configured_with_source(self) -> None:
        os.environ["PERPLEXITY_API_KEY"] = "проверочный-ключ"
        res = _call("geo_providers")
        self.assertNotIn("isError", res["result"])
        row = _provider_row(res["result"]["content"][0]["text"], "perplexity")
        self.assertIn("| да |", row)
        self.assertIn("окружение процесса", row)

    def test_without_the_key_provider_is_not_configured(self) -> None:
        res = _call("geo_providers")
        row = _provider_row(res["result"]["content"][0]["text"], "perplexity")
        self.assertIn("| нет |", row)
        self.assertIn("PERPLEXITY_API_KEY", row)

    def test_key_from_file_is_also_seen(self) -> None:
        from yaseo import env

        env.write_env_value("OPENAI_API_KEY", "из-файла")
        res = _call("geo_providers")
        row = _provider_row(res["result"]["content"][0]["text"], "openai")
        self.assertIn("| да |", row)


if __name__ == "__main__":
    unittest.main()
