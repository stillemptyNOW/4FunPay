"""
Единая точка брендинга проекта.

Все пользовательские тексты (локализации, консольный баннер, тексты Telegram-ПУ)
ссылаются на бренд не литералами, а плейсхолдерами вида ``{{BOT_NAME}}``.
Подстановка выполняется в :meth:`locales.localizer.Localizer.translate`
и в :func:`apply`, поэтому сменить бренд можно правкой только этого файла.

Заполни значения ниже своими данными. Значения в двойных фигурных скобках -
незаполненные плейсхолдеры; они видны в интерфейсе как есть, что и служит
напоминанием их заменить.
"""

from __future__ import annotations

from typing import Final

# --- Идентичность проекта --------------------------------------------------

BOT_NAME: Final[str] = "4FunPay"
"""Полное название проекта. Показывается в баннере, /about, уведомлениях."""

BOT_SHORT_NAME: Final[str] = "4FP"
"""Короткая форма названия. Используется в частых упоминаниях внутри текстов."""

VERSION: Final[str] = "1.0.0"
"""Версия сборки. Своя нумерация, не связанная с версиями апстрима."""

# --- Контакты и ссылки -----------------------------------------------------
# Замени на свои. Формат: OWNER_TG и SUPPORT_CHAT - с ведущей "@",
# REPO_URL - полный https-адрес без завершающего слэша.

OWNER_TG: Final[str] = "@{{OWNER_TG}}"
"""Telegram владельца проекта."""

SUPPORT_CHAT: Final[str] = "@{{SUPPORT_CHAT}}"
"""Telegram-чат поддержки."""

REPO_URL: Final[str] = "{{REPO_URL}}"
"""Адрес git-репозитория проекта."""

# --- Технические имена -----------------------------------------------------

SERVICE_NAME: Final[str] = "4funpay"
"""Имя systemd-юнита и каталога установки на сервере."""

LOGGER_PREFIX: Final[str] = "4FP"
"""Префикс имён логгеров ядра."""

RUNTIME_ENV_FLAG: Final[str] = "FOURFP_RUNNING_AS_SERVICE"
"""Имя переменной окружения, по которой процесс понимает, что запущен как сервис."""

CONSOLE_TITLE: Final[str] = BOT_NAME
"""Заголовок окна консоли."""

# --- Подстановка плейсхолдеров ---------------------------------------------

_PLACEHOLDERS: Final[dict[str, str]] = {
    "{{BOT_NAME}}": BOT_NAME,
    "{{BOT_SHORT_NAME}}": BOT_SHORT_NAME,
    "{{OWNER_TG}}": OWNER_TG,
    "{{SUPPORT_CHAT}}": SUPPORT_CHAT,
    "{{REPO_URL}}": REPO_URL,
    "{{SERVICE_NAME}}": SERVICE_NAME,
}


def apply(text: str) -> str:
    """
    Подставляет значения бренда в текст.

    Вызывается до ``str.format()``, поэтому двойные фигурные скобки
    плейсхолдеров не конфликтуют с ``{}`` для аргументов форматирования.

    :param text: текст с плейсхолдерами вида ``{{BOT_NAME}}``.

    :return: текст с подставленными значениями.
    """
    if "{{" not in text:
        return text
    for placeholder, value in _PLACEHOLDERS.items():
        if placeholder in text:
            text = text.replace(placeholder, value)
    return text
