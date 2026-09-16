"""
Страж утечек: в открытый репозиторий не должны попасть служебные файлы
(базы, ключи, выгрузки) и абсолютные пути чьей-то домашней папки.

Список слов, которые конкретная команда не хочет видеть в репозитории (имена
клиентов, внутренние домены), в самом репозитории не хранится: опубликованный
список сам стал бы утечкой. Его подают снаружи — путь к текстовому файлу в
переменной YASEO_LEAK_WORDS, одно слово на строку, строки с # пропускаются.
Без переменной проверка слов пропускается, остальные проверки идут всегда.

Слово ищется подстрокой без учёта регистра: короткое слово внутри длинного
тоже считается находкой — лишнюю строку проще переписать, чем пропустить утечку.
"""
from __future__ import annotations

import fnmatch
import os
import re
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_FILE_PATTERNS = ("*.db", "*.jsonl", ".env", "*.plist", "*.bak-*")

#: Абсолютный путь домашней папки конкретного человека. Шаблоны вида
#: `/Users/<имя>` в документации пишутся как `~`.
HOME_PATH_RE = re.compile(r"/(?:Users|home)/(?!<)[A-Za-z0-9._-]+/")

SKIP_DIR_NAMES = {".git", "dist", "build", "__pycache__", ".venv", ".pytest_cache", ".mypy_cache"}


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.endswith(".egg-info")


def iter_scanned_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(_skip_dir(part) for part in rel_parts[:-1]):
            continue
        yield path


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # бинарные и нечитаемые файлы — не текстовая утечка


def load_words(path: str | os.PathLike | None) -> tuple[str, ...]:
    if not path:
        return ()
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return tuple(w.strip() for w in lines if w.strip() and not w.lstrip().startswith("#"))


def scan_words(root: Path, words: tuple[str, ...]) -> list[str]:
    """Список 'путь:строка: «слово»' по каждому найденному вхождению."""
    hits: list[str] = []
    for path in iter_scanned_files(root):
        text = _read_text(path)
        if text is None:
            continue
        lower = text.lower()
        for word in words:
            idx = lower.find(word.lower())
            if idx != -1:
                line_no = text.count("\n", 0, idx) + 1
                hits.append(f"{path.relative_to(root)}:{line_no}: «{word}»")
    return hits


def scan_home_paths(root: Path) -> list[str]:
    hits: list[str] = []
    for path in iter_scanned_files(root):
        text = _read_text(path)
        if text is None:
            continue
        for m in HOME_PATH_RE.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            hits.append(f"{path.relative_to(root)}:{line_no}: {m.group(0)}")
    return hits


def scan_files(root: Path) -> list[str]:
    hits: list[str] = []
    for path in iter_scanned_files(root):
        for pattern in FORBIDDEN_FILE_PATTERNS:
            if fnmatch.fnmatch(path.name, pattern):
                hits.append(f"{path.relative_to(root)} (совпадает с {pattern})")
    return hits


class LeakScannerSelfTest(unittest.TestCase):
    """Положительный контроль: молчание сканера что-то доказывает, только если
    на заведомом совпадении он не молчит."""

    def test_scanner_detects_word_including_as_substring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "note.py").write_text("# секретныйклиент упомянут\n", encoding="utf-8")
            self.assertTrue(scan_words(tmp_path, ("СекретныйКлиент",)))
            self.assertTrue(scan_words(tmp_path, ("клиент",)))
            self.assertEqual(scan_words(tmp_path, ("другое",)), [])

    def test_word_list_file_skips_comments_and_blanks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "words.txt"
            f.write_text("# комментарий\n\nальфа\n  бета  \n", encoding="utf-8")
            self.assertEqual(load_words(f), ("альфа", "бета"))
        self.assertEqual(load_words(None), ())

    def test_scanner_detects_home_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Пути собраны по частям: литерал в этом файле сам был бы находкой.
            (tmp_path / "a.md").write_text("лежит в /" + "Users/someone/project\n", encoding="utf-8")
            (tmp_path / "b.md").write_text("лежит в /" + "home/someone/project\n", encoding="utf-8")
            (tmp_path / "c.md").write_text("шаблон /Users/<имя>/ и путь ~/.config\n", encoding="utf-8")
            hits = scan_home_paths(tmp_path)
        self.assertEqual(len(hits), 2, hits)

    def test_scanner_detects_forbidden_file_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "leftover.db").write_bytes(b"\x00\x01")
            self.assertTrue(scan_files(tmp_path))

    def test_scanner_is_clean_on_an_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.assertEqual(scan_words(tmp_path, ("x",)), [])
            self.assertEqual(scan_home_paths(tmp_path), [])
            self.assertEqual(scan_files(tmp_path), [])


class NoLeaksInRepoTests(unittest.TestCase):
    def test_no_private_words_anywhere_in_the_repo(self) -> None:
        source = os.environ.get("YASEO_LEAK_WORDS")
        if not source:
            self.skipTest("YASEO_LEAK_WORDS не задан — список слов подаётся снаружи")
        words = load_words(source)
        self.assertTrue(words, f"список слов пуст: {source}")
        hits = scan_words(REPO_ROOT, words)
        self.assertEqual(hits, [], "найдены запрещённые слова:\n" + "\n".join(hits))

    def test_no_home_paths_anywhere_in_the_repo(self) -> None:
        hits = scan_home_paths(REPO_ROOT)
        self.assertEqual(hits, [], "найдены абсолютные домашние пути:\n" + "\n".join(hits))

    def test_no_forbidden_file_patterns_anywhere_in_the_repo(self) -> None:
        hits = scan_files(REPO_ROOT)
        self.assertEqual(hits, [], "найдены запрещённые файлы:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
