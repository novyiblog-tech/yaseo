#!/usr/bin/env python3
"""
yaseo — SEO под Яндекс: выдача, частотность, позиции, ядро, аудит.

    yaseo <команда> [аргументы]

Команды:
    init         первичная настройка: ключи Yandex AI Studio и проверка
    serp         выдача Яндекса по запросу
    keywords     частотность и расширения через Wordstat
    competition  конкурентность запросов с разбором по факторам
    pool         расширение пула отслеживаемых запросов, словарь ниши проекта
    core         семантическое ядро проекта: бренд, целевые, околоцелевые
    brief        бриф на новую статью по данным выдачи
    track        трекер позиций: добавить, снять, прогнать, отчёт
    articles     эффективность статей по целевым запросам
    audit        технический аудит сайта
    plan         план правок сайта по приоритету, из уже измеренного
    webmaster    Яндекс.Вебмастер: показы, клики, индексация, пул запросов
    metrika      Яндекс.Метрика: поведение на посадочных страницах
    yandex       доступ к Метрике и Вебмастеру: проверка и продление токена
    storage      база: создать схему, проекты, что накоплено
    mcp          запустить MCP-сервер (обычно запускает MCP-клиент)

У каждой команды свой --help:
    yaseo serp --help
"""
from __future__ import annotations

import getpass
import os
import runpy
import sys

COMMANDS: dict[str, str] = {
    "serp": "yaseo.yandex_serp",
    "keywords": "yaseo.wordstat",
    "competition": "yaseo.competition",
    "pool": "yaseo.pool",
    "core": "yaseo.core",
    "brief": "yaseo.brief",
    "track": "yaseo.tracker",
    "articles": "yaseo.articles",
    "audit": "yaseo.audit",
    "plan": "yaseo.plan",
    "webmaster": "yaseo.webmaster",
    "metrika": "yaseo.metrika",
    "yandex": "yaseo.yandex_auth",
    "storage": "yaseo.storage",
    "mcp": "yaseo.mcp_server",
}


def usage() -> None:
    print((__doc__ or "").strip())


# ──────────────────────────── init ────────────────────────────

def _ask(prompt: str, current: str | None, secret: bool = False) -> str:
    shown = ""
    if current:
        shown = f" [{current[:4]}…, Enter — оставить]" if secret else f" [{current}]"
    read = getpass.getpass if secret else input
    value = read(f"{prompt}{shown}: ").strip()
    return value or (current or "")


def init(argv: list[str] | None = None) -> int:
    """
    Первичная настройка: ключи в `~/.config/yaseo/.env` (права 0600), затем
    бесплатная проверка и, по согласию, одна дешёвая живая проверка.
    """
    import argparse

    from . import env as _env

    p = argparse.ArgumentParser(prog="yaseo init",
                                description="Первичная настройка yaseo")
    p.add_argument("--no-live", action="store_true",
                   help="не предлагать живую проверку (без трат)")
    args = p.parse_args(argv)

    target = _env.user_env_file()
    existing = _env.parse_env_file(target) if target.exists() else {}

    print("Настройка yaseo.")
    print(_env.WHERE_TO_GET)
    print(f"Ключи будут записаны в {target} (права 0600).\n")

    try:
        folder = _ask("YC_FOLDER_ID (идентификатор каталога)", existing.get("YC_FOLDER_ID"))
        key = _ask("YANDEX_AI_STUDIO_API_KEY (ввод скрыт)",
                   existing.get("YANDEX_AI_STUDIO_API_KEY"), secret=True)
        domain = _ask("YASEO_DOMAIN — ваш сайт, необязательно (example.ru)",
                      existing.get("YASEO_DOMAIN"))
    except (EOFError, KeyboardInterrupt):
        print("\nНастройка прервана, ничего не записано.", file=sys.stderr)
        return 1

    if not folder or not key:
        print("Без YC_FOLDER_ID и YANDEX_AI_STUDIO_API_KEY выдача и Wordstat не работают. "
              "Ничего не записано.", file=sys.stderr)
        return 1

    values = dict(existing)
    values["YC_FOLDER_ID"] = folder
    values["YANDEX_AI_STUDIO_API_KEY"] = key
    if domain:
        values["YASEO_DOMAIN"] = domain

    lines = ["# yaseo — ключи. Файл читается только владельцем (0600)."]
    lines += [f"{k}={v}" for k, v in values.items()]
    # Та же атомарная запись, что и у обновления одного ключа (`write_env_value`):
    # падение посреди записи не должно оставить файл пустым или обрезанным.
    _env.atomic_write_text(target, "\n".join(lines) + "\n")
    print(f"\nЗаписано: {target}")

    # Бесплатная проверка: что видно пакету, без обращений к API.
    from .mcp_server import tool_whoami
    print("\n" + tool_whoami({}))

    # Переменные окружения старше файла: если они заданы, запись в файл
    # ничего не изменит, и об этом надо сказать.
    shadow = [k for k in _env.SEARCH_KEYS if os.environ.get(k)]
    if shadow:
        print(f"\nВнимание: в окружении заданы {', '.join(shadow)} — они старше файла.")

    if args.no_live:
        return 0
    try:
        ok = input("\nСделать одну живую проверку — запрос частотности в Wordstat "
                   "(около 0,02 ₽)? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ok = ""
    if ok not in ("y", "yes", "д", "да"):
        print("Живая проверка пропущена.")
        return 0

    from . import wordstat
    f = wordstat.frequency("погода", num=1)
    if f.error:
        print(f"Wordstat ответил ошибкой: {f.error}", file=sys.stderr)
        return 1
    print(f"Wordstat отвечает: «погода» — {f.freq} показов в месяц. Всё готово.")
    return 0


# ──────────────────────────── вход ────────────────────────────

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        usage()
        return 0
    if argv[0] in ("-V", "--version", "version"):
        from . import __version__
        print(f"yaseo {__version__}")
        return 0

    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "init":
            return init(rest)

        module = COMMANDS.get(cmd)
        if not module:
            print(f"Неизвестная команда: {cmd}\n", file=sys.stderr)
            usage()
            return 1

        # Подменяем argv, чтобы у модуля работал его собственный argparse.
        sys.argv = [f"yaseo {cmd}"] + rest
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        # `raise SystemExit("текст")` — штатный способ модуля сказать человеку,
        # что не так. Строковый код к числу не приводится: печатаем текст и
        # отдаём 1, как это делает сам питон.
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 1
        return int(e.code or 0)
    except ModuleNotFoundError as e:
        print(f"Модуль команды {cmd!r} недоступен: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
