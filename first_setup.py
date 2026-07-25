"""
Мастер первичной настройки.

Запускается из main.py, если отсутствует ``configs/_main.cfg``. Спрашивает
минимум, необходимый для старта: golden_key, User-Agent, прокси, токен
Telegram-бота и пароль доступа к панели управления. Остальное настраивается
уже через Telegram.

Секреты никогда не выводятся в консоль после ввода и не пишутся в логи -
только в ``configs/_main.cfg``, который исключён из git.
"""

from __future__ import annotations

import os
import time
from configparser import ConfigParser

import telebot
from colorama import Fore, Style

import branding
from Utils.cardinal_tools import build_proxy, check_proxy, hash_password, validate_proxy
from Utils.config_loader import load_main_config

GOLDEN_KEY_LENGTH = 32
"""Длина cookie golden_key на funpay.com."""

MIN_PASSWORD_LENGTH = 8
"""Минимальная длина пароля доступа к Telegram-ПУ."""

DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
"""
User-Agent по умолчанию.

Лучше указать свой: значение по умолчанию одинаково у всех, кто не менял его,
и потому само по себе является узнаваемым признаком. Скопируй строку из того
браузера, из которого брал golden_key - тогда сессия выглядит цельно.
"""

DEFAULT_CONFIG: dict[str, dict[str, str]] = {
    "FunPay": {
        "golden_key": "",
        "user_agent": DEFAULT_USER_AGENT,
        "autoRaise": "0",
        "autoResponse": "0",
        "autoDelivery": "0",
        "multiDelivery": "0",
        "autoRestore": "0",
        "autoDisable": "0",
        "oldMsgGetMode": "0",
        "keepSentMessagesUnread": "0",
        "locale": "ru",
    },
    "Telegram": {
        "enabled": "0",
        "token": "",
        "secretKeyHash": "",
        "blockLogin": "0",
        "proxy": "",
    },
    "BlockList": {
        "blockDelivery": "0",
        "blockResponse": "0",
        "blockNewMessageNotification": "0",
        "blockNewOrderNotification": "0",
        "blockCommandNotification": "0",
    },
    "NewMessageView": {
        "includeMyMessages": "1",
        "includeFPMessages": "1",
        "includeBotMessages": "0",
        "notifyOnlyMyMessages": "0",
        "notifyOnlyFPMessages": "0",
        "notifyOnlyBotMessages": "0",
        "showImageName": "1",
    },
    "Greetings": {
        "ignoreSystemMessages": "0",
        "onlyNewChats": "0",
        "sendGreetings": "0",
        "greetingsText": "Привет, $chat_name! Чем могу помочь?",
        "greetingsCooldown": "2",
    },
    "OrderConfirm": {
        "watermark": "1",
        "sendReply": "0",
        "replyText": "$username, спасибо за подтверждение заказа $order_id!\n"
                     "Будем благодарны за отзыв.",
    },
    "ReviewReply": {
        "star1Reply": "0",
        "star2Reply": "0",
        "star3Reply": "0",
        "star4Reply": "0",
        "star5Reply": "0",
        "star1ReplyText": "",
        "star2ReplyText": "",
        "star3ReplyText": "",
        "star4ReplyText": "",
        "star5ReplyText": "",
    },
    "Proxy": {
        "enable": "0",
        "proxy": "",
        "check": "0",
    },
    "Other": {
        "watermark": "🤖",
        "requestsDelay": "4",
        "language": "ru",
    },
}


def _say(text: str) -> None:
    """Печатает информационную строку мастера."""
    print(f"{Fore.CYAN}{Style.BRIGHT}{text}{Style.RESET_ALL}")


def _ask(prompt: str) -> str:
    """Задаёт вопрос и возвращает введённое значение без окружающих пробелов."""
    print(f"\n{Fore.MAGENTA}{Style.BRIGHT}┌── {Fore.CYAN}{prompt}{Style.RESET_ALL}")
    return input(f"{Fore.MAGENTA}{Style.BRIGHT}└──> {Style.RESET_ALL}").strip()


def _warn(text: str) -> None:
    """Печатает сообщение об ошибке ввода."""
    print(f"{Fore.RED}{Style.BRIGHT}   {text}{Style.RESET_ALL}")


def create_configs() -> None:
    """Создаёт пустые конфиги автоответчика и автовыдачи, если их нет."""
    for path in ("configs/auto_response.cfg", "configs/auto_delivery.cfg"):
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8"):
                pass


def create_config_obj(settings: dict[str, dict[str, str]]) -> ConfigParser:
    """
    Создаёт объект конфига с настройками формата, принятыми в проекте.

    :param settings: словарь секций и параметров.

    :return: объект конфига.
    """
    config = ConfigParser(delimiters=(":",), interpolation=None)
    config.optionxform = str
    config.read_dict(settings)
    return config


def contains_cyrillic(text: str) -> bool:
    """
    Проверяет наличие кириллицы в строке.

    Используется для отлова случая, когда вместо User-Agent вставили
    русскоязычную подсказку.

    :param text: проверяемая строка.

    :return: True, если найден хотя бы один кириллический символ.
    """
    return any("А" <= char <= "я" or char in "Ёё" for char in text)


def input_proxy(set_telebot_proxy: bool = False) -> str | None:
    """
    Запрашивает прокси и проверяет их работоспособность.

    :param set_telebot_proxy: применить ли прокси к telebot сразу после проверки.

    :return: строка прокси или None, если пользователь пропустил шаг.
    """
    while True:
        proxy_input = input(f"{Fore.MAGENTA}{Style.BRIGHT}└──> {Style.RESET_ALL}").strip()

        if not proxy_input:
            if set_telebot_proxy:
                telebot.apihelper.proxy = None
            return None

        try:
            proxy = build_proxy(*validate_proxy(proxy_input))
            if not check_proxy({"http": proxy, "https": proxy}):
                _warn("Прокси не отвечают. Попробуй другие или нажми Enter, чтобы пропустить.")
                continue
            if set_telebot_proxy:
                telebot.apihelper.proxy = {"http": proxy, "https": proxy}
            return proxy
        except Exception as exc:
            _warn(f"Неверный формат прокси: {exc}")


PROXY_PROMPT = ("Прокси в формате scheme://login:password@ip:port, login:password@ip:port "
                "или ip:port. Не нужны - просто нажми Enter.")


def setup_telegram_proxy() -> None:
    """
    Отдельный сценарий: сменить прокси для доступа к Telegram в готовом конфиге.

    Вызывается из ``setup_telegram_proxy.py`` - нужен, когда бот уже настроен,
    но Telegram недоступен напрямую с сервера.
    """
    config = load_main_config("configs/_main.cfg")
    print(f"\n{Fore.MAGENTA}{Style.BRIGHT}┌── {Fore.CYAN}Прокси ДЛЯ ДОСТУПА К TELEGRAM. "
          f"{PROXY_PROMPT}{Style.RESET_ALL}")
    while True:
        try:
            proxy = input_proxy(set_telebot_proxy=True)
            username = telebot.TeleBot(config["Telegram"]["token"]).get_me().username
            _say(f"\nПодключение к Telegram работает: @{username}")
            break
        except Exception as exc:
            _warn(f"Не удалось подключиться к Telegram: {exc}")

    config.set("Telegram", "proxy", proxy or "")
    _say("Сохраняю конфиг...")
    with open("configs/_main.cfg", "w", encoding="utf-8") as f:
        config.write(f)
    time.sleep(3)


def _ask_golden_key(config: ConfigParser) -> None:
    """Запрашивает golden_key и записывает его в конфиг."""
    while True:
        golden_key = _ask(
            "Токен FunPay-аккаунта (cookie golden_key).\n"
            "   Где взять: залогинься на funpay.com, открой DevTools (F12) ->\n"
            "   Application -> Cookies -> https://funpay.com -> скопируй значение golden_key.\n"
            "   Это ключ от аккаунта: не показывай его никому.")
        if len(golden_key) != GOLDEN_KEY_LENGTH:
            _warn(f"golden_key состоит ровно из {GOLDEN_KEY_LENGTH} символов, "
                  f"а введено {len(golden_key)}. Попробуй ещё раз.")
            continue
        config.set("FunPay", "golden_key", golden_key)
        return


def _ask_user_agent(config: ConfigParser) -> None:
    """Запрашивает User-Agent (необязательно) и записывает его в конфиг."""
    while True:
        user_agent = _ask(
            "User-Agent браузера, из которого ты взял golden_key.\n"
            "   Узнать: вбей в поиске \"my user agent\" и скопируй строку.\n"
            "   Нажми Enter, чтобы оставить значение по умолчанию (менее желательно).")
        if contains_cyrillic(user_agent):
            _warn("Похоже, это не User-Agent. Строка начинается с \"Mozilla/5.0\".")
            continue
        if user_agent:
            config.set("FunPay", "user_agent", user_agent)
        return


def _ask_telegram_token(config: ConfigParser) -> None:
    """Запрашивает и проверяет токен Telegram-бота."""
    while True:
        token = _ask("API-токен Telegram-бота (получить у @BotFather).\n"
                     "   Токен даёт полный контроль над ботом: держи его в секрете.")
        try:
            if not token or not token.split(":")[0].isdigit():
                raise ValueError("формат должен быть <цифры>:<строка>")
            username = telebot.TeleBot(token).get_me().username
        except Exception as exc:
            _warn(f"Токен не подошёл: {exc}")
            continue
        _say(f"   Бот определён: @{username}")
        config.set("Telegram", "token", token)
        config.set("Telegram", "enabled", "1")
        return


def _ask_password(config: ConfigParser) -> None:
    """Запрашивает пароль доступа к Telegram-ПУ и сохраняет его хеш."""
    while True:
        password = _ask(
            f"Придумай пароль для входа в панель управления в Telegram.\n"
            f"   Минимум {MIN_PASSWORD_LENGTH} символов, заглавные и строчные буквы, хотя бы одна цифра.\n"
            f"   В конфиг пишется только bcrypt-хеш, сам пароль не сохраняется.")
        if (len(password) < MIN_PASSWORD_LENGTH
                or password.lower() == password
                or password.upper() == password
                or not any(char.isdigit() for char in password)):
            _warn("Пароль слишком простой. Нужны заглавные, строчные буквы и цифра.")
            continue
        config.set("Telegram", "secretKeyHash", hash_password(password))
        return


def first_setup() -> None:
    """Проводит первичную настройку и записывает ``configs/_main.cfg``."""
    config = create_config_obj(DEFAULT_CONFIG)
    create_configs()

    _say(f"\nПервичная настройка {branding.BOT_NAME}.")
    _say("Основной конфиг не найден, поэтому создадим его сейчас.\n"
         "Всё, что ты введёшь, попадёт только в configs/_main.cfg на этой машине.")

    _ask_golden_key(config)
    _ask_user_agent(config)

    print(f"\n{Fore.MAGENTA}{Style.BRIGHT}┌── {Fore.CYAN}Прокси ДЛЯ ДОСТУПА К TELEGRAM. "
          f"{PROXY_PROMPT}{Style.RESET_ALL}")
    if telegram_proxy := input_proxy(set_telebot_proxy=True):
        config.set("Telegram", "proxy", telegram_proxy)

    _ask_telegram_token(config)
    _ask_password(config)

    print(f"\n{Fore.MAGENTA}{Style.BRIGHT}┌── {Fore.CYAN}Прокси ДЛЯ ДОСТУПА К FUNPAY. "
          f"{PROXY_PROMPT}{Style.RESET_ALL}")
    if funpay_proxy := input_proxy():
        config.set("Proxy", "proxy", funpay_proxy)
        config.set("Proxy", "enable", "1")
        config.set("Proxy", "check", "1")

    with open("configs/_main.cfg", "w", encoding="utf-8") as f:
        config.write(f)

    _say("\nГотово, конфиг сохранён в configs/_main.cfg.")
    _say("Запусти бота снова и напиши своему Telegram-боту пароль, который только что придумал.")
    _say("Дальше всё настраивается через Telegram: команда /menu.")
    time.sleep(5)
