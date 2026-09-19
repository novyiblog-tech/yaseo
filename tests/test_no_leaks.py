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
import subprocess  # только `git ls-files`, список аргументов  # nosec B404
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


def git_files(root: Path) -> list[Path] | None:
    """Файлы, которые уедут в репозиторий: отслеживаемые плюс неотслеживаемые
    мимо .gitignore. `None` — если git не ответил (распакованный архив).

    Обход дерева видит и рабочий мусор соседних задач, который в репозиторий
    не попадёт никогда. 19.09.2026 на этом встал весь прогон: каталог ролика
    от скилла /brag лежал рядом с кодом и принёс домашний путь. Спросить git
    строже, а не слабее: неотслеживаемый файл он тоже покажет, если тот не
    закрыт .gitignore, то есть ровно то, что уедет следующим `git add`.
    """
    try:
        out = subprocess.run(  # nosec B603 B607
            ["git", "-C", str(root), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    names = [n for n in out.stdout.decode("utf-8", "replace").split("\0") if n]
    return [root / n for n in names if (root / n).is_file()]


def iter_scanned_files(root: Path):
    listed = git_files(root)
    if listed is not None:
        yield from listed
        return
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

    def test_git_mode_sees_what_would_be_committed(self) -> None:
        """Контроль на обе стороны: незакрытый файл виден, закрытый .gitignore
        не виден, а сам список берётся у git, а не обходом дерева."""
        def git(*args, cwd):
            return subprocess.run(["git", *args], cwd=cwd,  # nosec B603 B607
                                  capture_output=True, check=False, timeout=30)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            if git("init", "-q", cwd=repo).returncode != 0:
                self.skipTest("git недоступен")
            # Путь собран по частям: литерал в этом файле сам был бы находкой.
            (repo / "junk.md").write_text("лежит в /" + "Users/someone/x\n", encoding="utf-8")
            self.assertIsNotNone(git_files(repo))
            self.assertEqual(len(scan_home_paths(repo)), 1)

            (repo / ".gitignore").write_text("junk.md\n", encoding="utf-8")
            self.assertEqual(scan_home_paths(repo), [])

    def test_git_mode_falls_back_outside_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(git_files(Path(tmp)))

    def test_empty_git_answer_is_not_mistaken_for_a_clean_repo(self) -> None:
        """Пустой ответ git (дерево целиком закрыто родительским .gitignore)
        отличается от `None`, и на нём сканер не находит ничего. Значит
        проверки обязаны сами убеждаться, что файлы вообще нашлись."""
        def git(*args, cwd):
            return subprocess.run(["git", *args], cwd=cwd,  # nosec B603 B607
                                  capture_output=True, check=False, timeout=30)
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            if git("init", "-q", cwd=parent).returncode != 0:
                self.skipTest("git недоступен")
            (parent / ".gitignore").write_text("/*\n", encoding="utf-8")
            work = parent / "yaseo"
            work.mkdir()
            (work / "leak.md").write_text("лежит в /" + "Users/someone/x\n", encoding="utf-8")

            self.assertEqual(git_files(work), [])       # не None: режим git включён
            self.assertEqual(scan_home_paths(work), [])  # и утечки «нет»
            # Ровно поэтому в NoLeaksInRepoTests.setUp стоит счёт файлов.
            self.assertEqual(list(iter_scanned_files(work)), [])

    def test_scanner_is_clean_on_an_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.assertEqual(scan_words(tmp_path, ("x",)), [])
            self.assertEqual(scan_home_paths(tmp_path), [])
            self.assertEqual(scan_files(tmp_path), [])


class NoLeaksInRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        """Положительный контроль: молчание сканера доказывает что-то, только
        если он вообще смотрел файлы.

        `git ls-files` отвечает пустым списком и кодом 0, когда дерево лежит
        внутри чужого репозитория, который закрывает его целиком (whitelist
        `/*` в .gitignore родителя), или когда это распакованный архив внутри
        любого репозитория. Тогда все три проверки ниже зеленели бы, не
        прочитав ни одного файла. Нашло независимое ревью 20.09.2026.
        """
        self.scanned = list(iter_scanned_files(REPO_ROOT))
        self.assertGreater(len(self.scanned), 50,
                           "сканер утечек не увидел файлов: проверка ничего не доказывает")

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
