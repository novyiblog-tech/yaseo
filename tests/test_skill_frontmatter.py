"""
Шапки скиллов плагина читаются как YAML.

`claude plugin validate --strict` пропускает шапку, в которой строгий YAML
падает: двоеточие с пробелом внутри незакавыченного значения
(`description: … English: "check my site"`) — «mapping values are not
allowed here». Библиотеки YAML в пакете нет, поэтому правило проверяется
напрямую.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent / "plugins" / "yaseo" / "skills"


def _frontmatter(text: str) -> list[str]:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise AssertionError("нет шапки --- … ---")
    return m.group(1).splitlines()


class SkillFrontmatterTests(unittest.TestCase):
    def test_skills_exist(self) -> None:
        self.assertEqual(len(list(SKILLS.glob("*/SKILL.md"))), 7)

    def test_plain_values_are_valid_yaml(self) -> None:
        for path in sorted(SKILLS.glob("*/SKILL.md")):
            for line in _frontmatter(path.read_text(encoding="utf-8")):
                key, sep, value = line.partition(": ")
                with self.subTest(skill=path.parent.name, key=key):
                    self.assertTrue(sep, f"строка без «ключ: значение»: {line!r}")
                    value = value.strip()
                    if value[:1] in ('"', "'"):
                        continue
                    self.assertNotIn(": ", value, "двоеточие с пробелом в незакавыченном значении")
                    self.assertNotIn(" #", value, "« #» начинает комментарий YAML")
                    self.assertFalse(value.endswith(":"), "значение кончается двоеточием")

    def test_name_matches_directory_and_has_english_triggers(self) -> None:
        for path in sorted(SKILLS.glob("*/SKILL.md")):
            fm = dict(line.split(": ", 1) for line in _frontmatter(path.read_text(encoding="utf-8")))
            with self.subTest(skill=path.parent.name):
                self.assertEqual(fm["name"], path.parent.name)
                self.assertIn("English — ", fm["description"])
                self.assertLessEqual(len(fm["description"]), 1024)

    def test_control_catches_colon(self) -> None:
        """Положительный контроль: та самая поломка правилом ловится."""
        bad = '---\nname: x\ndescription: Проверить сайт. English: "check my site".\n---\n'
        line = _frontmatter(bad)[1]
        self.assertIn(": ", line.partition(": ")[2])


if __name__ == "__main__":
    unittest.main()
