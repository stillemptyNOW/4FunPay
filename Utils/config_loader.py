"""
Загрузка и валидация конфигов.

Схема основного конфига описана декларативно в :data:`MAIN_CONFIG_SPEC`:
для каждого параметра заданы допустимые значения и значение по умолчанию.

Отсутствующий параметр не является ошибкой, если у него есть значение
по умолчанию: он дописывается в файл при загрузке. Это заменило цепочку
из 15 частных миграций, унаследованную от исходного проекта, и делает
конфиг совместимым вперёд - добавление новой настройки в код не ломает
уже существующие конфиги.
"""
from __future__ import annotations

import codecs
import configparser
import os
from configparser import ConfigParser, SectionProxy
from dataclasses import dataclass

from Utils.exceptions import (ParamNotFoundError, EmptyValueError, ValueNotValidError, SectionNotFoundError,
                              ConfigParseError, ProductsFileNotFoundError, NoProductVarError,
                              SubCommandAlreadyExists, DuplicateSectionErrorWrapper)

BOOL_VALUES = ["0", "1"]
"""Допустимые значения переключателя."""

SITE_LOCALES = ["ru", "en", "uk"]
"""
Языки, на которых можно запрашивать страницы funpay.com.

Не путать с языком интерфейса бота: этот параметр влияет на разметку,
которую придётся разбирать, поэтому список задан самим сайтом.
"""


@dataclass(frozen=True)
class ParamSpec:
    """
    Описание одного параметра конфига.

    :param valid: список допустимых значений; None - любая строка.
    :param allow_empty: допустимо ли пустое значение.
    :param default: значение, которым параметр дописывается, если его нет
        в файле. None - параметр обязателен, его отсутствие это ошибка.
    """

    valid: list[str] | None = None
    allow_empty: bool = False
    default: str | None = None


def _switch(default: str) -> ParamSpec:
    """Сокращение для параметра-переключателя 0/1."""
    return ParamSpec(valid=BOOL_VALUES, default=default)


def _text(default: str = "", allow_empty: bool = True) -> ParamSpec:
    """Сокращение для текстового параметра."""
    return ParamSpec(allow_empty=allow_empty, default=default)


def check_param(param_name: str, section: SectionProxy, valid_values: list[str | None] | None = None,
                raise_if_not_exists: bool = True) -> str | None:
    """
    Проверяет, существует ли в переданной секции указанный параметр и если да, валидно ли его значение.

    :param param_name: название параметра.
    :param section: объект секции.
    :param valid_values: валидные значения. Если None, любая строка - валидное значение.
    :param raise_if_not_exists: возбуждать ли исключение, если параметр не найден.

    :return: Значение ключа, если ключ найден и его значение валидно. Если ключ не найден и
    raise_ex_if_not_exists == False - возвращает None. В любом другом случае возбуждает исключения.
    """
    if param_name not in list(section.keys()):
        if raise_if_not_exists:
            raise ParamNotFoundError(param_name)
        return None

    value = section[param_name].strip()

    # Если значение пустое ("", оно не может быть None)
    if not value:
        if valid_values and None in valid_values:
            return value
        raise EmptyValueError(param_name)

    if valid_values and valid_values != [None] and value not in valid_values:
        raise ValueNotValidError(param_name, value, valid_values)
    return value


def create_config_obj(config_path: str) -> ConfigParser:
    """
    Создает объект конфига с нужными настройками.

    :param config_path: путь до файла конфига.

    :return: объект конфига.
    """
    config = ConfigParser(delimiters=(":",), interpolation=None)
    config.optionxform = str
    config.read_file(codecs.open(config_path, "r", "utf8"))
    return config


MAIN_CONFIG_SPEC: dict[str, dict[str, ParamSpec]] = {
    "FunPay": {
        # Пустое значение допустимо: секрет может быть задан переменной
        # окружения. Наличие проверяется отдельно в Utils.secrets.
        "golden_key": _text(),
        "user_agent": _text(),
        "autoRaise": _switch("0"),
        "autoResponse": _switch("0"),
        "autoDelivery": _switch("0"),
        "multiDelivery": _switch("0"),
        "autoRestore": _switch("0"),
        "autoDisable": _switch("0"),
        "oldMsgGetMode": _switch("0"),
        "keepSentMessagesUnread": _switch("0"),
        "locale": ParamSpec(valid=SITE_LOCALES, default="ru"),
    },

    "Telegram": {
        "enabled": _switch("0"),
        "token": _text(),
        "secretKeyHash": _text(),
        "blockLogin": _switch("0"),
    },

    "BlockList": {
        "blockDelivery": _switch("0"),
        "blockResponse": _switch("0"),
        "blockNewMessageNotification": _switch("0"),
        "blockNewOrderNotification": _switch("0"),
        "blockCommandNotification": _switch("0"),
    },

    "NewMessageView": {
        "includeMyMessages": _switch("1"),
        "includeFPMessages": _switch("1"),
        "includeBotMessages": _switch("0"),
        "notifyOnlyMyMessages": _switch("0"),
        "notifyOnlyFPMessages": _switch("0"),
        "notifyOnlyBotMessages": _switch("0"),
        "showImageName": _switch("1"),
    },

    "Greetings": {
        "ignoreSystemMessages": _switch("0"),
        "onlyNewChats": _switch("0"),
        "sendGreetings": _switch("0"),
        "greetingsText": _text("Привет, $chat_name! Чем могу помочь?", allow_empty=False),
        "greetingsCooldown": _text("2", allow_empty=False),
    },

    "OrderConfirm": {
        "watermark": _switch("1"),
        "sendReply": _switch("0"),
        "replyText": _text("$username, спасибо за подтверждение заказа $order_id!", allow_empty=False),
    },

    "ReviewReply": {
        **{f"star{stars}Reply": _switch("0") for stars in range(1, 6)},
        **{f"star{stars}ReplyText": _text() for stars in range(1, 6)},
    },


    "Other": {
        "watermark": _text("🤖"),
        "requestsDelay": ParamSpec(valid=[str(i) for i in range(1, 101)], default="4"),
    },
}
"""Схема основного конфига: допустимые значения и значения по умолчанию."""

# Водяной знак подставляется в сообщения покупателям. Если конфиг перенесён
# из исходного проекта, в нём остался чужой бренд - вычищаем при загрузке.
FOREIGN_WATERMARK_MARKERS = ("cardinal", "𝑪𝒂𝒓𝒅𝒊𝒏𝒂𝒍", "𝓒𝓪𝓻𝓭𝓲𝓷𝓪𝓵", "ᴄᴀʀᴅɪɴᴀʟ")


def _fill_defaults(config: ConfigParser) -> bool:
    """
    Дописывает отсутствующие параметры значениями по умолчанию.

    :param config: объект конфига.

    :return: True, если конфиг был изменён и его нужно сохранить.
    """
    changed = False
    for section_name, params in MAIN_CONFIG_SPEC.items():
        if not config.has_section(section_name):
            continue
        for param_name, spec in params.items():
            if param_name in config[section_name] or spec.default is None:
                continue
            config.set(section_name, param_name, spec.default)
            changed = True
    return changed


def _clean_foreign_watermark(config: ConfigParser) -> bool:
    """
    Убирает бренд исходного проекта из водяного знака.

    :param config: объект конфига.

    :return: True, если водяной знак был заменён.
    """
    if not config.has_option("Other", "watermark"):
        return False
    current = config["Other"]["watermark"].lower()
    if not any(marker in current for marker in FOREIGN_WATERMARK_MARKERS):
        return False
    config.set("Other", "watermark", "🤖")
    return True


def load_main_config(config_path: str) -> ConfigParser:
    """
    Загружает и проверяет основной конфиг.

    Отсутствующие параметры, у которых есть значение по умолчанию,
    дописываются в файл. Файл перезаписывается один раз, а не после
    каждой правки, как это было в исходном проекте.

    :param config_path: путь до основного конфига.

    :return: разобранный конфиг.

    :raises ConfigParseError: секция отсутствует либо значение недопустимо.
    """
    config = create_config_obj(config_path)

    for section_name in MAIN_CONFIG_SPEC:
        if section_name not in config.sections():
            raise ConfigParseError(config_path, section_name, SectionNotFoundError())

    changed = _fill_defaults(config)
    changed |= _clean_foreign_watermark(config)
    if changed:
        with open(config_path, "w", encoding="utf-8") as file:
            config.write(file)

    for section_name, params in MAIN_CONFIG_SPEC.items():
        for param_name, spec in params.items():
            valid_values = spec.valid
            if spec.allow_empty:
                valid_values = [None] if valid_values is None else [*valid_values, None]
            try:
                check_param(param_name, config[section_name], valid_values=valid_values)
            except (ParamNotFoundError, EmptyValueError, ValueNotValidError) as exc:
                raise ConfigParseError(config_path, section_name, exc)

    return config



def load_auto_response_config(config_path: str):
    """
    Парсит и проверяет на правильность конфиг команд.

    :param config_path: путь до конфига команд.

    :return: спарсеный конфиг команд.
    """
    try:
        config = create_config_obj(config_path)
    except configparser.DuplicateSectionError as e:
        raise ConfigParseError(config_path, e.section, DuplicateSectionErrorWrapper())

    command_sets = []
    for command in config.sections():
        try:
            check_param("response", config[command])
            check_param("telegramNotification", config[command], valid_values=["0", "1"], raise_if_not_exists=False)
            check_param("enabled", config[command], valid_values=["0", "1"], raise_if_not_exists=False)
            check_param("notificationText", config[command], raise_if_not_exists=False)
        except (ParamNotFoundError, EmptyValueError, ValueNotValidError) as e:
            raise ConfigParseError(config_path, command, e)

        if not config.has_option(command, "enabled"):
            config.set(command, "enabled", "1")

        if "|" in command:
            command_sets.append(command)

    for command_set in command_sets:
        commands = command_set.split("|")
        parameters = config[command_set]

        for new_command in commands:
            new_command = new_command.strip()
            if not new_command:
                continue
            if new_command in config.sections():
                raise ConfigParseError(config_path, command_set, SubCommandAlreadyExists(new_command))
            config.add_section(new_command)
            for param_name in parameters:
                config.set(new_command, param_name, parameters[param_name])
    return config


def load_raw_auto_response_config(config_path: str):
    """
    Загружает исходный конфиг автоответчика.

    :param config_path: путь до конфига команд.

    :return: спарсеный конфиг команд.
    """
    config = create_config_obj(config_path)
    for raw_commands in config.sections():
        if not config.has_option(raw_commands, "enabled"):
            config.set(raw_commands, "enabled", "1")
    return config


def load_auto_delivery_config(config_path: str):
    """
    Парсит и проверяет на правильность конфиг автовыдачи.

    :param config_path: путь до конфига автовыдачи.

    :return: спарсеный конфиг товаров для автовыдачи.
    """
    try:
        config = create_config_obj(config_path)
    except configparser.DuplicateSectionError as e:
        raise ConfigParseError(config_path, e.section, DuplicateSectionErrorWrapper())

    for lot_title in config.sections():
        try:
            lot_response = check_param("response", config[lot_title])
            products_file_name = check_param("productsFileName", config[lot_title], raise_if_not_exists=False)
            check_param("disable", config[lot_title], valid_values=["0", "1"], raise_if_not_exists=False)
            check_param("disableAutoRestore", config[lot_title], valid_values=["0", "1"], raise_if_not_exists=False)
            check_param("disableAutoDisable", config[lot_title], valid_values=["0", "1"], raise_if_not_exists=False)
            check_param("disableAutoDelivery", config[lot_title], valid_values=["0", "1"], raise_if_not_exists=False)
            if products_file_name is None:
                # Если данного параметра нет, то в текущем лоте более нечего проверять -> переход на след. итерацию.
                continue
        except (ParamNotFoundError, EmptyValueError, ValueNotValidError) as e:
            raise ConfigParseError(config_path, lot_title, e)

        # Проверяем, существует ли файл.
        if not os.path.exists(f"storage/products/{products_file_name}"):
            raise ConfigParseError(config_path, lot_title,
                                   ProductsFileNotFoundError(f"storage/products/{products_file_name}"))

        # Проверяем, есть ли хотя бы 1 переменная $product в тексте response.
        if "$product" not in lot_response:
            raise ConfigParseError(config_path, lot_title, NoProductVarError())
    return config
