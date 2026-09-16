"""
Все закрепления на тег совпадают с версией пакета.

Выпуск меняет номер в нескольких файлах сразу: pyproject, `__version__`,
plugin.json, ref маркетплейса, `@vX.Y.Z` в `.mcp.json`, скиллах и
документации. Пропущенный файл отправляет пользователя на старый тег.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from yaseo import __version__

ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r"novyiblog-tech/yaseo@v(\d+\.\d+\.\d+)")


class VersionPinTests(unittest.TestCase):
    def test_pyproject_and_plugin_match_package(self) -> None:
        m = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
        self.assertEqual(m.group(1), __version__)
        plugin = json.loads((ROOT / "plugins/yaseo/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["version"], __version__)
        market = (ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'"ref":\s*"v([^"]+)"', market), [__version__])

    def test_every_tag_pin_matches_package(self) -> None:
        files = [ROOT / "plugins/yaseo/.mcp.json", *ROOT.glob("*.md"), *ROOT.glob("docs/*.md"),
                 *ROOT.glob("plugins/yaseo/skills/*/SKILL.md")]
        seen = 0
        for path in files:
            if path.name == "CHANGELOG.md":
                continue
            for found in PIN.findall(path.read_text(encoding="utf-8")):
                seen += 1
                with self.subTest(file=str(path.relative_to(ROOT))):
                    self.assertEqual(found, __version__)
        # Положительный контроль: закреплений в документации заведомо много.
        self.assertGreater(seen, 20)


if __name__ == "__main__":
    unittest.main()
