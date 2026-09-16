"""Тесты `yaseo.env`: цепочка источников ключей, ошибка нехватки, запись файла."""
from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import IsolatedTestCase
from yaseo import env


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class SourceChainTests(IsolatedTestCase):
    """
    Порядок: окружение → YASEO_ENV_FILE → $XDG_CONFIG_HOME/yaseo/.env.

    `./.env` текущего каталога в цепочке нет (снято 16.09.2026): MCP-сервер
    запускается из каталога чужого проекта пользователя, и его `.env`
    (ключи другого проекта — OpenAI, Яндекс) молча подхватывался бы под видом
    своих. `test_cwd_dotenv_is_never_read` — отрицательный контроль на это.

    Каждый тест сам решает, какие файлы существуют — так «побеждает нужный»
    проверяется по-настоящему, а не совпадением: если писать все файлы сразу
    в `setUp`, самый слабый уровень (домашний файл) никогда не выиграет,
    и тест на него будет проверять не то, что заявлено в имени.
    """

    def test_user_config_file_used_when_nothing_stronger_exists(self) -> None:
        _write(env.user_env_file(), "YC_FOLDER_ID=из_домашнего_файла\n")
        values, source = env.read_all()
        self.assertEqual(values["YC_FOLDER_ID"], "из_домашнего_файла")
        self.assertEqual(source["YC_FOLDER_ID"], str(env.user_env_file()))

    def test_cwd_dotenv_is_never_read(self) -> None:
        # Отрицательный контроль пункта 2: чужой ./.env текущего каталога
        # (например, ключ другого проекта) не должен просочиться, даже когда
        # больше ключ ниоткуда не взять.
        _write(self.paths["cwd"] / ".env", "YC_FOLDER_ID=из_текущего_каталога\n")
        values, source = env.read_all()
        self.assertNotIn("YC_FOLDER_ID", values)
        self.assertNotIn("YC_FOLDER_ID", source)

    def test_yaseo_env_file_wins_over_cwd_and_user(self) -> None:
        _write(env.user_env_file(), "YC_FOLDER_ID=из_домашнего_файла\n")
        _write(self.paths["cwd"] / ".env", "YC_FOLDER_ID=из_текущего_каталога\n")
        explicit_file = self.paths["home"] / "explicit.env"
        _write(explicit_file, "YC_FOLDER_ID=из_явного_файла\n")
        os.environ["YASEO_ENV_FILE"] = str(explicit_file)

        values, source = env.read_all()
        self.assertEqual(values["YC_FOLDER_ID"], "из_явного_файла")
        self.assertEqual(source["YC_FOLDER_ID"], str(explicit_file))

    def test_process_env_wins_over_all_files(self) -> None:
        _write(env.user_env_file(), "YC_FOLDER_ID=из_домашнего_файла\n")
        _write(self.paths["cwd"] / ".env", "YC_FOLDER_ID=из_текущего_каталога\n")
        explicit_file = self.paths["home"] / "explicit.env"
        _write(explicit_file, "YC_FOLDER_ID=из_явного_файла\n")
        os.environ["YASEO_ENV_FILE"] = str(explicit_file)
        os.environ["YC_FOLDER_ID"] = "из_окружения_процесса"

        values, source = env.read_all()
        self.assertEqual(values["YC_FOLDER_ID"], "из_окружения_процесса")
        self.assertEqual(source["YC_FOLDER_ID"], "окружение процесса")

    def test_candidate_files_lists_both_in_priority_order(self) -> None:
        explicit_file = self.paths["home"] / "explicit.env"
        os.environ["YASEO_ENV_FILE"] = str(explicit_file)
        files = [p.resolve() for p in env.candidate_files()]
        expected = [explicit_file.resolve(), env.user_env_file().resolve()]
        self.assertEqual(files, expected)


class AiProviderKeyTests(IsolatedTestCase):
    """
    Дефект 16.09.2026: ключи ИИ-провайдеров (Perplexity, OpenAI, Gemini,
    Anthropic) из окружения процесса не подхватывались — `read_all()` знал
    только ключи Яндекса и `YASEO_*`. Положительный контроль ниже проверяет,
    что окружение теперь первая ступень для НИХ тоже, наравне с Яндексом.
    """

    def test_perplexity_key_from_process_env_is_seen(self) -> None:
        os.environ["PERPLEXITY_API_KEY"] = "из_окружения_процесса"
        values, source = env.read_all()
        self.assertEqual(values["PERPLEXITY_API_KEY"], "из_окружения_процесса")
        self.assertEqual(source["PERPLEXITY_API_KEY"], "окружение процесса")

    def test_perplexity_key_from_file_is_also_seen(self) -> None:
        _write(env.user_env_file(), "PERPLEXITY_API_KEY=из_файла\n")
        values, _ = env.read_all()
        self.assertEqual(values["PERPLEXITY_API_KEY"], "из_файла")

    def test_unrelated_env_var_is_not_picked_up(self) -> None:
        # Контроль на объект: read_all() не должен превратиться в «взять всё
        # окружение» — только известные пакету ключи и YASEO_*.
        os.environ["СЛУЧАЙНАЯ_ПЕРЕМЕННАЯ_НЕ_ИЗ_YASEO"] = "мимо"
        values, _ = env.read_all()
        self.assertNotIn("СЛУЧАЙНАЯ_ПЕРЕМЕННАЯ_НЕ_ИЗ_YASEO", values)


class MissingKeysTests(IsolatedTestCase):
    def test_missing_key_raises_with_key_name_and_hint(self) -> None:
        with self.assertRaises(env.MissingKeysError) as ctx:
            env.load_env(required=("YC_FOLDER_ID", "YANDEX_AI_STUDIO_API_KEY"))
        exc = ctx.exception
        self.assertEqual(sorted(exc.missing), ["YANDEX_AI_STUDIO_API_KEY", "YC_FOLDER_ID"])
        message = str(exc.code)
        self.assertIn("YC_FOLDER_ID", message)
        self.assertIn("YANDEX_AI_STUDIO_API_KEY", message)
        self.assertIn("yaseo init", message)

    def test_present_keys_do_not_raise(self) -> None:
        os.environ["YC_FOLDER_ID"] = "folder1"
        os.environ["YANDEX_AI_STUDIO_API_KEY"] = "key1"
        values = env.load_env(required=("YC_FOLDER_ID", "YANDEX_AI_STUDIO_API_KEY"))
        self.assertEqual(values["YC_FOLDER_ID"], "folder1")

    def test_empty_required_never_raises(self) -> None:
        # Пустой набор — «без проверки»: не должно падать даже без единого ключа.
        try:
            env.load_env(required=())
        except env.MissingKeysError:
            self.fail("пустой required не должен требовать ни одного ключа")


class WriteEnvValueTests(IsolatedTestCase):
    def test_creates_user_file_with_0600_when_nothing_exists(self) -> None:
        target = env.write_env_value("YANDEX_AI_STUDIO_API_KEY", "секрет123")
        self.assertEqual(target, env.user_env_file())
        mode = target.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"права файла {oct(mode)}, ожидалось 0o600")
        self.assertIn("YANDEX_AI_STUDIO_API_KEY=секрет123", target.read_text(encoding="utf-8"))

    def test_updates_existing_key_in_place_without_touching_others(self) -> None:
        _write(env.user_env_file(), "YC_FOLDER_ID=folder1\nYANDEX_AI_STUDIO_API_KEY=old\n")
        env.write_env_value("YANDEX_AI_STUDIO_API_KEY", "new")
        content = env.user_env_file().read_text(encoding="utf-8")
        self.assertIn("YC_FOLDER_ID=folder1", content)
        self.assertIn("YANDEX_AI_STUDIO_API_KEY=new", content)
        self.assertNotIn("=old", content)

    def test_writes_into_primary_existing_file_not_a_second_one(self) -> None:
        # Ключи уже есть в явном файле (YASEO_ENV_FILE) — писать нужно туда же,
        # а не создавать второй файл в ~/.config/yaseo/.env.
        explicit_file = self.paths["home"] / "explicit.env"
        _write(explicit_file, "YC_FOLDER_ID=folder1\n")
        os.environ["YASEO_ENV_FILE"] = str(explicit_file)
        target = env.write_env_value("YANDEX_AI_STUDIO_API_KEY", "new-token")
        self.assertEqual(target.resolve(), explicit_file.resolve())
        self.assertFalse(env.user_env_file().exists())

    def test_cwd_dotenv_is_never_written_to(self) -> None:
        # Отрицательный контроль: ./.env текущего каталога вне цепочки, значит
        # запись не должна выбрать его, даже если он там лежит с ключами.
        cwd_env = self.paths["cwd"] / ".env"
        _write(cwd_env, "YC_FOLDER_ID=folder1\n")
        target = env.write_env_value("YANDEX_AI_STUDIO_API_KEY", "new-token")
        self.assertEqual(target, env.user_env_file())
        self.assertNotIn("YANDEX_AI_STUDIO_API_KEY", cwd_env.read_text(encoding="utf-8"))


class AtomicWriteTextTests(IsolatedTestCase):
    """
    Дефект 16.09.2026: `write_env_value`/`cli init` обнуляли файл (`write_text`
    / `O_TRUNC`), потом писали заново. Падение между этими двумя шагами
    (обрыв во время продления OAuth-токена, конкурентный процесс) оставляло
    файл пустым — параллельный MCP-сервер читал ноль ключей вместо старых.
    """

    def test_new_file_is_created_with_0600(self) -> None:
        target = self.paths["home"] / "new.env"
        env.atomic_write_text(target, "YC_FOLDER_ID=x\n")
        mode = stat.S_IMODE(target.stat().st_mode)
        self.assertEqual(mode, 0o600, f"права файла {oct(mode)}, ожидалось 0o600")
        self.assertEqual(target.read_text(encoding="utf-8"), "YC_FOLDER_ID=x\n")

    def test_preserves_other_lines_when_updating(self) -> None:
        target = self.paths["home"] / "keys.env"
        _write(target, "YC_FOLDER_ID=folder1\nYANDEX_AI_STUDIO_API_KEY=old\n")
        env.atomic_write_text(target, "YC_FOLDER_ID=folder1\nYANDEX_AI_STUDIO_API_KEY=new\n")
        content = target.read_text(encoding="utf-8")
        self.assertIn("YC_FOLDER_ID=folder1", content)
        self.assertIn("YANDEX_AI_STUDIO_API_KEY=new", content)

    def test_existing_wide_file_loses_group_and_other_access(self) -> None:
        # В файл ложится секрет: права группы и остальных снимаются,
        # права владельца остаются как были.
        target = self.paths["home"] / "wide.env"
        _write(target, "YC_FOLDER_ID=old\n")
        os.chmod(target, 0o644)
        env.atomic_write_text(target, "YC_FOLDER_ID=new\n")
        mode = stat.S_IMODE(target.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(target.read_text(encoding="utf-8"), "YC_FOLDER_ID=new\n")

    def test_symlinked_env_file_is_written_through_the_link(self) -> None:
        real = self.paths["home"] / "vault" / "keys.env"
        _write(real, "YANDEX_OAUTH_TOKEN=old\n")
        link = self.paths["home"] / "link.env"
        link.symlink_to(real)
        env.atomic_write_text(link, "YANDEX_OAUTH_TOKEN=new\n")
        self.assertTrue(link.is_symlink(), "ссылка подменена обычным файлом")
        self.assertEqual(real.read_text(encoding="utf-8"), "YANDEX_OAUTH_TOKEN=new\n")

    def test_failure_during_replace_leaves_old_file_intact(self) -> None:
        target = self.paths["home"] / "keys.env"
        _write(target, "YC_FOLDER_ID=старое_значение\n")

        with mock.patch("os.replace", side_effect=OSError("диск пропал на середине")):
            with self.assertRaises(OSError):
                env.atomic_write_text(target, "YC_FOLDER_ID=новое_значение\n")

        # Файл цели не тронут — падение случилось до `os.replace`, старое
        # содержимое осталось на месте, а не обнулилось и не оборвалось.
        self.assertEqual(target.read_text(encoding="utf-8"), "YC_FOLDER_ID=старое_значение\n")
        # Временный файл за собой не оставлен — не должен виснуть рядом мусором.
        leftovers = [p for p in target.parent.iterdir() if p.name != target.name]
        self.assertEqual(leftovers, [], f"остался временный файл: {leftovers}")

    def test_write_env_value_survives_a_replace_failure_too(self) -> None:
        # Тот же контроль, но через публичный путь `write_env_value` — именно
        # им пользуются `yandex_auth.refresh` и MCP-сервер.
        _write(env.user_env_file(), "YC_FOLDER_ID=folder1\nYANDEX_OAUTH_TOKEN=старый\n")
        with mock.patch("os.replace", side_effect=OSError("диск пропал на середине")):
            with self.assertRaises(OSError):
                env.write_env_value("YANDEX_OAUTH_TOKEN", "новый")
        content = env.user_env_file().read_text(encoding="utf-8")
        self.assertIn("YANDEX_OAUTH_TOKEN=старый", content)
        self.assertNotIn("новый", content)


class ParseEnvFileTests(unittest.TestCase):
    def test_handles_quotes_export_prefix_and_comments(self) -> None:
        with_tmp = Path(__file__).resolve().parent / "fixtures"
        p = with_tmp / "_scratch_env_for_parse_test.env"
        try:
            p.write_text(
                "# комментарий, пропускается\n"
                "export FOO=\"bar baz\"\n"
                "BAR='qux'\n"
                "\n"
                "BAZ=plain\n"
                "не_пара_без_равно\n",
                encoding="utf-8",
            )
            data = env.parse_env_file(p)
        finally:
            p.unlink(missing_ok=True)
        self.assertEqual(data, {"FOO": "bar baz", "BAR": "qux", "BAZ": "plain"})

    def test_missing_file_returns_empty_dict(self) -> None:
        self.assertEqual(env.parse_env_file(Path("/no/such/file/here.env")), {})


if __name__ == "__main__":
    unittest.main()
