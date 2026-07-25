"""
Работа с секретами: golden_key и токен Telegram-бота.

Зачем отдельный модуль:

1. **Приоритет окружения.** Значение из переменной окружения перекрывает
   значение из ``configs/_main.cfg``. Это нужно для Docker и для случаев, когда
   секрет не хочется держать в файле.

2. **Секреты не попадают в сохраняемый конфиг.** Панель управления в Telegram
   периодически перезаписывает ``configs/_main.cfg`` целиком
   (``Cardinal.save_config``). Если подставлять значение из окружения прямо в
   объект ``ConfigParser``, оно осело бы в файле при первом же переключении
   настройки - то есть смысл переменных окружения был бы потерян. Поэтому
   окружение читается в момент использования, а не при загрузке конфига.

3. **Маскирование.** :func:`mask` даёт безопасное для логов представление,
   чтобы случайно не записать ключ целиком.

Секреты не логируются ни на одном уровне логирования и не выводятся в консоль.
"""

from __future__ import annotations

import os
from configparser import ConfigParser

GOLDEN_KEY_ENV = "FOURFP_GOLDEN_KEY"
"""Переменная окружения с cookie golden_key."""

TELEGRAM_TOKEN_ENV = "FOURFP_TELEGRAM_TOKEN"
"""Переменная окружения с токеном Telegram-бота."""

GOLDEN_KEY_LENGTH = 32
"""Ожидаемая длина golden_key."""


def load_dotenv_file(path: str = ".env") -> None:
    """
    Подгружает переменные из файла ``.env``, если он есть.

    Использует ``python-dotenv``, если пакет установлен; при его отсутствии
    разбирает файл сам - зависимость не обязательна для работы бота.
    Существующие переменные окружения не перезаписываются: то, что задано
    в systemd-юните или в ``docker compose``, приоритетнее файла.

    :param path: путь до файла с переменными.
    """
    if not os.path.exists(path):
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_dotenv_fallback(path)
        return

    load_dotenv(path, override=False)


def _load_dotenv_fallback(path: str) -> None:
    """
    Минимальный разбор ``.env`` без внешних зависимостей.

    Поддерживает строки ``KEY=value``, комментарии с ``#`` и кавычки вокруг
    значения. Этого достаточно для секретов, которые тут хранятся.

    :param path: путь до файла с переменными.
    """
    with open(path, "r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def resolve(config: ConfigParser, section: str, option: str, env_var: str) -> str:
    """
    Возвращает значение секрета: сначала из окружения, потом из конфига.

    :param config: объект основного конфига.
    :param section: секция конфига.
    :param option: параметр конфига.
    :param env_var: имя переменной окружения.

    :return: значение секрета или пустая строка, если он не задан нигде.
    """
    if value := os.getenv(env_var, "").strip():
        return value
    try:
        return config[section][option].strip()
    except KeyError:
        return ""


def golden_key(config: ConfigParser) -> str:
    """
    Возвращает актуальный golden_key.

    :param config: объект основного конфига.

    :return: значение golden_key.
    """
    return resolve(config, "FunPay", "golden_key", GOLDEN_KEY_ENV)


def telegram_token(config: ConfigParser) -> str:
    """
    Возвращает актуальный токен Telegram-бота.

    :param config: объект основного конфига.

    :return: значение токена.
    """
    return resolve(config, "Telegram", "token", TELEGRAM_TOKEN_ENV)


def is_from_env(env_var: str) -> bool:
    """
    Задан ли секрет переменной окружения.

    Нужно, чтобы панель управления не предлагала менять то, что всё равно будет
    перекрыто окружением при следующем запуске.

    :param env_var: имя переменной окружения.

    :return: True, если переменная задана и непустая.
    """
    return bool(os.getenv(env_var, "").strip())


def mask(secret: str, visible: int = 4) -> str:
    """
    Готовит безопасное для логов представление секрета.

    :param secret: значение секрета.
    :param visible: сколько символов оставить с каждого края.

    :return: строка вида ``abcd...wxyz`` или ``<не задан>``.
    """
    if not secret:
        return "<не задан>"
    if len(secret) <= visible * 2:
        return "*" * len(secret)
    return f"{secret[:visible]}...{secret[-visible:]}"


def check_startup_secrets(config: ConfigParser) -> list[str]:
    """
    Проверяет наличие и формат секретов перед запуском.

    Возвращает список понятных описаний проблем вместо возбуждения исключения:
    вызывающий код выводит их все сразу, чтобы не заставлять исправлять
    по одной за запуск.

    :param config: объект основного конфига.

    :return: список описаний проблем. Пустой список означает, что всё в порядке.
    """
    problems: list[str] = []

    key = golden_key(config)
    if not key:
        problems.append(
            f"Не задан golden_key.\n"
            f"     Впиши его в configs/_main.cfg, секция [FunPay], параметр golden_key,\n"
            f"     либо задай переменную окружения {GOLDEN_KEY_ENV}.\n"
            f"     Где взять: funpay.com -> DevTools (F12) -> Application -> Cookies -> golden_key")
    elif len(key) != GOLDEN_KEY_LENGTH:
        problems.append(
            f"golden_key имеет длину {len(key)} символов вместо {GOLDEN_KEY_LENGTH}.\n"
            f"     Похоже, значение скопировано не полностью или с лишними символами.")

    telegram_enabled = config.has_section("Telegram") and config["Telegram"].getboolean("enabled", fallback=False)
    if telegram_enabled:
        token = telegram_token(config)
        if not token:
            problems.append(
                f"Telegram включён (enabled: 1), но токен не задан.\n"
                f"     Впиши его в configs/_main.cfg, секция [Telegram], параметр token,\n"
                f"     либо задай переменную окружения {TELEGRAM_TOKEN_ENV}.\n"
                f"     Токен выдаёт @BotFather.")
        elif ":" not in token or not token.split(":")[0].isdigit():
            problems.append(
                "Токен Telegram-бота имеет неверный формат.\n"
                "     Ожидается вид 1234567890:AAbbCc... - цифры, двоеточие, строка.")

        if not config["Telegram"].get("secretKeyHash", "").strip():
            problems.append(
                "Не задан secretKeyHash - пароль доступа к панели управления.\n"
                "     Удали configs/_main.cfg и пройди первичную настройку заново,\n"
                "     либо задай хеш вручную (bcrypt).")

    return problems
