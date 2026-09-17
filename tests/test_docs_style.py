"""
Стиль документации: без длинного тире, команды и фразы для Claude копируются кнопкой.

Длинное тире в описательных текстах читается как машинный текст. Исключение —
дословный вывод программы в обратных кавычках (`Метрика — счётчики`): его
человек ищет на экране, и строка должна совпадать символ в символ.
Диапазоны пишутся коротким тире (4–15) и под запрет не попадают.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EM_DASH = "—"
INLINE_CODE = re.compile(r"`[^`\n]*`")


def dashes_outside_code(text: str) -> list[tuple[int, str]]:
    found = []
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if EM_DASH in INLINE_CODE.sub("", line):
            found.append((n, line.strip()[:80]))
    return found


def uncopyable_commands(text: str) -> list[str]:
    """Фразы для Claude в цитатах и склеенные команды плагина.

    У цитаты на GitHub нет кнопки копирования, поэтому каждая фраза и каждая
    команда стоит в своём блоке кода. Команды `/plugin` вводятся в Claude Code
    по одной, два `/plugin` в одном блоке вставятся одним сообщением.
    """
    problems = [line for line in text.splitlines() if line.startswith("> ")]
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S):
        if sum(1 for line in block.splitlines() if line.strip().startswith("/plugin")) > 1:
            problems.append(block.strip()[:80])
    return problems


class DocsStyleTests(unittest.TestCase):
    def test_commands_are_copyable_blocks(self) -> None:
        files = [*ROOT.glob("README*.md"), *ROOT.glob("docs/INSTALL*.md")]
        self.assertEqual(len(files), 4)
        for path in files:
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertEqual(uncopyable_commands(path.read_text(encoding="utf-8")), [])

    def test_command_checker_catches_quotes_and_glued_plugin_commands(self) -> None:
        self.assertEqual(len(uncopyable_commands("> Проверь сайт example.ru")), 1)
        glued = "```\n/plugin marketplace add x\n/plugin install yaseo@yaseo\n```"
        self.assertEqual(len(uncopyable_commands(glued)), 1)
        self.assertEqual(uncopyable_commands("```text\n/plugin install yaseo@yaseo\n```"), [])

    def test_no_em_dash_in_docs(self) -> None:
        files = [*ROOT.glob("*.md"), *ROOT.glob("docs/*.md")]
        self.assertGreater(len(files), 5)
        for path in files:
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertEqual(dashes_outside_code(path.read_text(encoding="utf-8")), [])

    def test_checker_catches_a_planted_dash(self) -> None:
        # Положительный контроль: проверка должна ловить тире в тексте
        # и пропускать его внутри кода.
        self.assertEqual(len(dashes_outside_code(f"Слово {EM_DASH} слово")), 1)
        self.assertEqual(dashes_outside_code(f"Строка `Метрика {EM_DASH} счётчики`"), [])
        self.assertEqual(dashes_outside_code(f"```\nкод {EM_DASH} код\n```"), [])


if __name__ == "__main__":
    unittest.main()
