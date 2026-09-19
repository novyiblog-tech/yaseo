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


#: `uses: владелец/действие@<40 шестнадцатеричных> # версия`
#: Пробелы перед комментарием только горизонтальные: `\s` включает перевод
#: строки, и версия из комментария на следующей строке засчитывалась как
#: своя. Нашло независимое ревью 20.09.2026.
ACTION_USE = re.compile(r"^[ \t]*-?[ \t]*uses:[ \t]*(\S+)[ \t]*(#.*)?$", re.M)
SHA_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")


class ActionPinTests(unittest.TestCase):
    """Действия GitHub закреплены по SHA коммита, а не по тегу.

    Тег переносится владельцем действия в любой момент, и прогон однажды
    возьмёт не тот код, который читали при выкладке. Это самый дешёвый способ
    попасть в цепочку поставки, и заметить подмену можно только заранее.
    """

    def workflows(self) -> list[Path]:
        return sorted(ROOT.glob(".github/workflows/*.yml"))

    def test_every_action_is_pinned_by_sha(self) -> None:
        seen = 0
        for path in self.workflows():
            for ref, comment in ACTION_USE.findall(path.read_text(encoding="utf-8")):
                seen += 1
                with self.subTest(file=path.name, uses=ref):
                    self.assertRegex(ref, SHA_PIN,
                                     "действие закреплено не по SHA коммита")
                    self.assertTrue(comment and re.search(r"v\d", comment),
                                    "рядом с SHA нет версии в комментарии")
        # Положительный контроль: прогоны заведомо зовут чужие действия.
        self.assertGreater(seen, 5)

    def test_checker_catches_a_moving_tag(self) -> None:
        moving = "      - uses: actions/checkout@v7\n"
        ref, comment = ACTION_USE.findall(moving)[0]
        self.assertNotRegex(ref, SHA_PIN)
        self.assertFalse(comment)

    def test_checker_does_not_borrow_a_comment_from_the_next_line(self) -> None:
        """Версия засчитывается только из той же строки, что и SHA."""
        sha = "0" * 40
        two_lines = f"      - uses: actions/checkout@{sha}\n      # v7.0.1\n"
        ref, comment = ACTION_USE.findall(two_lines)[0]
        self.assertRegex(ref, SHA_PIN)
        self.assertFalse(comment)

    def test_installed_tools_are_pinned_too(self) -> None:
        """`pip install` без версии разрешается заново на каждом прогоне, при
        живом checkout репозитория. Поверхность больше, чем у переносимого
        тега, ради которого всё закреплялось."""
        for path in self.workflows():
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith("pip install"):
                    continue
                with self.subTest(file=path.name, line=stripped):
                    for pkg in stripped[len("pip install"):].split():
                        self.assertIn("==", pkg, "пакет поставлен без версии")

    def test_dependabot_watches_the_actions(self) -> None:
        text = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("github-actions", text)


if __name__ == "__main__":
    unittest.main()
