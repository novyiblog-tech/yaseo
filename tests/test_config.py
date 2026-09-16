"""Тесты `yaseo.config`: default_domain, normalize_domain, настройки трекера."""
from __future__ import annotations

import json
import os
import unittest

from tests.helpers import IsolatedTestCase
from yaseo import config, storage


class NormalizeDomainTests(unittest.TestCase):
    def test_strips_scheme_www_and_path(self) -> None:
        self.assertEqual(
            config.normalize_domain("HTTPS://WWW.Example.RU/path?x=1"), "example.ru"
        )

    def test_plain_domain_is_lowercased(self) -> None:
        self.assertEqual(config.normalize_domain("Example.RU"), "example.ru")

    def test_empty_input(self) -> None:
        self.assertEqual(config.normalize_domain(""), "")
        self.assertEqual(config.normalize_domain(None), "")


class DefaultDomainTests(IsolatedTestCase):
    def test_argument_wins_over_everything(self) -> None:
        os.environ["YASEO_DOMAIN"] = "envdomain.ru"
        storage.init_db()
        storage.add_project("Проект", "project-domain.ru")
        self.assertEqual(config.default_domain("Argument.RU"), "argument.ru")

    def test_env_var_used_when_no_argument(self) -> None:
        os.environ["YASEO_DOMAIN"] = "https://Envdomain.ru/path"
        self.assertEqual(config.default_domain(), "envdomain.ru")

    def test_single_active_project_resolved_automatically(self) -> None:
        storage.init_db()
        storage.add_project("Проект", "example.ru")
        self.assertEqual(config.default_domain(), "example.ru")

    def test_inactive_projects_are_ignored(self) -> None:
        storage.init_db()
        pid = storage.add_project("Старый", "old.ru")
        storage.remove_project(pid)
        storage.add_project("Новый", "new.ru")
        self.assertEqual(config.default_domain(), "new.ru")

    def test_multiple_active_projects_raise_domain_not_set(self) -> None:
        storage.init_db()
        storage.add_project("Первый", "one.ru")
        storage.add_project("Второй", "two.ru")
        with self.assertRaises(config.DomainNotSetError) as ctx:
            config.default_domain()
        self.assertIn("one.ru", str(ctx.exception))
        self.assertIn("two.ru", str(ctx.exception))

    def test_nothing_set_raises_with_helpful_message(self) -> None:
        with self.assertRaises(config.DomainNotSetError) as ctx:
            config.default_domain()
        self.assertIn("YASEO_DOMAIN", str(ctx.exception))

    def test_domain_or_none_swallows_the_error(self) -> None:
        self.assertIsNone(config.domain_or_none())

    def test_domain_or_none_still_returns_value_when_resolvable(self) -> None:
        os.environ["YASEO_DOMAIN"] = "example.ru"
        self.assertEqual(config.domain_or_none(), "example.ru")


class TrackingSettingsTests(IsolatedTestCase):
    def test_defaults_when_no_file_present(self) -> None:
        settings = config.tracking_settings()
        self.assertEqual(settings, config.TRACKING_DEFAULTS)
        self.assertFalse(config.tracking_file().exists())

    def test_file_overrides_known_keys_only(self) -> None:
        path = config.tracking_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"max_calls": 5, "repeats": 1, "unknown_extra_key": "ignored"}),
            encoding="utf-8",
        )
        settings = config.tracking_settings()
        self.assertEqual(settings["max_calls"], 5)
        self.assertEqual(settings["repeats"], 1)
        self.assertNotIn("unknown_extra_key", settings)
        # Ключи, не тронутые файлом, остаются умолчаниями.
        self.assertEqual(settings["min_interval_days"], config.TRACKING_DEFAULTS["min_interval_days"])

    def test_broken_json_falls_back_to_defaults(self) -> None:
        path = config.tracking_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{не json вовсе", encoding="utf-8")
        settings = config.tracking_settings()
        self.assertEqual(settings, config.TRACKING_DEFAULTS)

    def test_env_int_reads_and_reports_bad_value(self) -> None:
        os.environ["YASEO_TRACKING_MAX_CALLS"] = "42"
        self.assertEqual(config.env_int("YASEO_TRACKING_MAX_CALLS"), 42)
        os.environ["YASEO_TRACKING_MAX_CALLS"] = "не число"
        self.assertIsNone(config.env_int("YASEO_TRACKING_MAX_CALLS"))

    def test_env_int_missing_returns_none(self) -> None:
        self.assertIsNone(config.env_int("YASEO_TRACKING_MAX_CALLS"))


if __name__ == "__main__":
    unittest.main()
