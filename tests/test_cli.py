"""
Тесты `yaseo.cli`: неинтерактивный `init` (права 0600), `--help`/`--version`
верхнего уровня и точка входа `python -m yaseo --help` отдельным процессом.
Сеть не используется нигде — `init` вызывается с `--no-live`.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import IsolatedTestCase
from yaseo import __version__, cli, env


class InitNonInteractiveTests(IsolatedTestCase):
    def test_writes_env_file_with_0600_and_expected_keys(self) -> None:
        answers = iter(["FOLDER-123", "MyDomain.ru"])
        buf = io.StringIO()
        with mock.patch("builtins.input", side_effect=lambda *_: next(answers)), \
             mock.patch("getpass.getpass", return_value="SECRET-KEY"), \
             contextlib.redirect_stdout(buf):
            rc = cli.init(["--no-live"])
        self.assertEqual(rc, 0)

        target = env.user_env_file()
        self.assertTrue(target.exists())
        mode = target.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"права {oct(mode)}, ожидалось 0o600")

        content = target.read_text(encoding="utf-8")
        self.assertIn("YC_FOLDER_ID=FOLDER-123", content)
        self.assertIn("YANDEX_AI_STUDIO_API_KEY=SECRET-KEY", content)
        self.assertIn("YASEO_DOMAIN=MyDomain.ru", content)
        self.assertIn("yaseo", buf.getvalue().lower())

    def test_refuses_without_folder_or_key(self) -> None:
        answers = iter(["", ""])  # пустой YC_FOLDER_ID и пустой домен
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("builtins.input", side_effect=lambda *_: next(answers)), \
             mock.patch("getpass.getpass", return_value=""), \
             contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = cli.init(["--no-live"])
        self.assertEqual(rc, 1)
        self.assertFalse(env.user_env_file().exists())

    def test_ctrl_c_during_prompt_aborts_without_writing(self) -> None:
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            rc = cli.init(["--no-live"])
        self.assertEqual(rc, 1)
        self.assertFalse(env.user_env_file().exists())


class MainTopLevelTests(unittest.TestCase):
    def test_no_args_prints_usage_and_returns_zero(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main([])
        self.assertEqual(rc, 0)
        self.assertIn("yaseo", buf.getvalue())

    def test_help_flag_returns_zero(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("Команды:", buf.getvalue())

    def test_version_flag_prints_version(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--version"])
        self.assertEqual(rc, 0)
        self.assertIn(__version__, buf.getvalue())

    def test_unknown_command_returns_1_and_reports_it(self) -> None:
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = cli.main(["совсем-не-команда"])
        self.assertEqual(rc, 1)
        self.assertIn("Неизвестная команда", buf_err.getvalue())


class ModuleEntryPointTests(unittest.TestCase):
    """`python -m yaseo --help` в отдельном процессе — без сети и без ключей."""

    def test_python_dash_m_yaseo_help_succeeds(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = str(repo_root / "src")
        result = subprocess.run(
            [sys.executable, "-m", "yaseo", "--help"],
            cwd=str(repo_root),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("yaseo", result.stdout.lower())

    def test_python_dash_m_yaseo_version_succeeds(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = str(repo_root / "src")
        result = subprocess.run(
            [sys.executable, "-m", "yaseo", "--version"],
            cwd=str(repo_root),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(__version__, result.stdout)


if __name__ == "__main__":
    unittest.main()
