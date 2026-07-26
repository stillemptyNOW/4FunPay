"""
Тексты интерфейса.

Проект одноязычный: интерфейс только на русском. Переводы на английский
и украинский были убраны намеренно - каждую новую строку приходилось писать
трижды, и две копии неизбежно отставали от третьей.

Параметр ``language`` в методах сохранён и игнорируется. Он остался в подписях
по двум причинам: его передаёт код панели управления, когда знает язык
Telegram-клиента собеседника, и его могут передавать сторонние плагины.
Убирать параметр из публичного API ради одного языка смысла нет.

Не путать с ``FunPay.locale`` в конфиге: тот параметр задаёт язык страниц
funpay.com и влияет на разбор разметки, а не на язык интерфейса.
"""

from __future__ import annotations

import logging
from typing import Any

import branding
from locales import ru

logger = logging.getLogger("localizer")

LANGUAGE = "ru"
"""Единственный язык интерфейса."""


class Localizer:
    """
    Синглтон, отдающий локализованные тексты.

    Экземпляр создаётся в каждом модуле как ``Localizer()``; аргумент
    конструктора игнорируется и оставлен для совместимости с вызовами вида
    ``Localizer(config["Other"]["language"])``.
    """

    def __new__(cls, curr_lang: str | None = None) -> "Localizer":
        if not hasattr(cls, "instance"):
            cls.instance = super().__new__(cls)
            cls.instance.languages = {LANGUAGE: ru}
            cls.instance.current_language = LANGUAGE
        return cls.instance

    def translate(self, variable_name: str, *args: Any, language: str | None = None) -> str:
        """
        Возвращает форматированный текст по имени переменной.

        Плейсхолдеры бренда (``{{BOT_NAME}}`` и прочие) подставляются
        из :mod:`branding` до форматирования аргументами.

        Если переменной не существует, возвращается её имя - так опечатка
        в ключе видна в интерфейсе, а не роняет обработчик.

        :param variable_name: название переменной с текстом.
        :param args: аргументы для подстановки в ``{}``.
        :param language: игнорируется, см. модульную докстрингу.

        :return: готовый текст.
        """
        text = getattr(ru, variable_name, variable_name)
        text = branding.apply(text)

        args = list(args)
        placeholders = text.count("{}")
        if len(args) < placeholders:
            args.extend(["{}"] * (placeholders - len(args)))
        try:
            return text.format(*args)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
            return text

    def add_translation(self, uuid: str, variable_name: str, value: str,
                        language: str | None = None) -> None:
        """
        Добавляет текст от плагина.

        Ключ префиксуется UUID плагина, чтобы плагины не перетирали
        ни тексты ядра, ни тексты друг друга.

        :param uuid: UUID плагина.
        :param variable_name: название переменной.
        :param value: текст.
        :param language: игнорируется, см. модульную докстрингу.
        """
        setattr(ru, f"{uuid}_{variable_name}", value)

    def plugin_translate(self, uuid: str, variable_name: str, *args: Any,
                         language: str | None = None) -> str:
        """
        Возвращает текст плагина, откатываясь на текст ядра.

        :param uuid: UUID плагина.
        :param variable_name: название переменной.
        :param args: аргументы для подстановки.
        :param language: игнорируется, см. модульную докстрингу.

        :return: текст плагина либо, если его нет, текст ядра.
        """
        prefixed = f"{uuid}_{variable_name}"
        result = self.translate(prefixed, *args)
        if result != prefixed:
            return result
        return self.translate(variable_name, *args)
