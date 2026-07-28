"""
Неподтверждённые заказы: поиск и подготовка заявки в поддержку FunPay.

Покупатели регулярно забывают нажать «Подтвердить выполнение заказа», и деньги
продавца висят замороженными. Штатный путь решения - заявка в поддержку
FunPay с перечнем таких заказов.

Плагин раз в сутки просматривает раздел продаж, отбирает заказы со статусом
«оплачен» старше заданного возраста и присылает в Telegram готовый текст
заявки, который остаётся скопировать в форму на сайте.

Почему заявка НЕ отправляется автоматически - две причины.

1. Форма поддержки требует, чтобы продавец подтвердил: услуги оказаны, а чат
   заказа «безусловно и прямо» читается как «покупатель забыл нажать кнопку»,
   без какой-либо двусмысленности. Ошибка в этом списке приводит к отклонению
   заявки целиком. Отличить «забыл подтвердить» от «есть претензия» может
   только человек, прочитавший переписку.

2. В FunPayAPI нет метода отправки заявки в поддержку: эндпоинт, имена полей
   формы и значения выпадающих списков в проекте не описаны. Реализация
   вслепую отправляла бы неизвестно что.

Команды Telegram:
    /unconfirmed        - проверить прямо сейчас
    /unconfirmed_age N  - возраст заказа в часах, с которого он попадает в список
    /unconfirmed_every N - период автопроверки в часах, 0 отключает
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging import getLogger
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardButton as Button
from telebot.types import InlineKeyboardMarkup as Keyboard
from telebot.types import Message

from FunPayAPI.common.enums import OrderStatuses

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.types import OrderShortcut

NAME = "Неподтверждённые заказы"
VERSION = "1.0.0"
DESCRIPTION = ("Раз в сутки ищет заказы, которые покупатели забыли подтвердить, "
               "и присылает готовый текст заявки в поддержку FunPay.\n\n"
               "Заявка не отправляется автоматически: форма требует, чтобы ты "
               "сверил чаты глазами, а ошибка в списке отклоняет заявку целиком.\n\n"
               "Команды: /unconfirmed")
CREDITS = "{{OWNER_TG}}"
UUID = "9f4c2e18-7b63-4a05-91d8-3c5e0a7d2b46"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

logger = getLogger(f"4FP.{__name__}")

SETTINGS_FILE = "storage/cache/unconfirmed_orders.json"
SUPPORT_FORM_URL = "https://support.funpay.com/"

DEFAULT_MIN_AGE_HOURS = 24
DEFAULT_INTERVAL_HOURS = 24
MAX_PAGES = 10
"""Ограничение на число страниц продаж, чтобы не уйти в бесконечный обход."""

TICKET_HEADER = "Здравствуйте, пожалуйста подтвердите данные заказы:"


@dataclass
class Settings:
    """Настройки плагина."""

    min_age_hours: int = DEFAULT_MIN_AGE_HOURS
    """С какого возраста заказ считается зависшим."""

    interval_hours: int = DEFAULT_INTERVAL_HOURS
    """Период автопроверки. 0 - автопроверка выключена."""

    last_check: float = 0.0
    """Время последней проверки."""


def load_settings() -> Settings:
    """
    Загружает настройки.

    :return: настройки; при отсутствии или порче файла - значения по умолчанию.
    """
    if not os.path.exists(SETTINGS_FILE):
        return Settings()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return Settings(
            min_age_hours=int(data.get("min_age_hours", DEFAULT_MIN_AGE_HOURS)),
            interval_hours=int(data.get("interval_hours", DEFAULT_INTERVAL_HOURS)),
            last_check=float(data.get("last_check", 0.0)),
        )
    except (OSError, ValueError, TypeError):
        logger.debug("Настройки повреждены, беру значения по умолчанию", exc_info=True)
        return Settings()


def save_settings(settings: Settings) -> None:
    """
    Сохраняет настройки.

    :param settings: настройки.
    """
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as file:
            json.dump({
                "min_age_hours": settings.min_age_hours,
                "interval_hours": settings.interval_hours,
                "last_check": settings.last_check,
            }, file)
    except OSError:
        logger.debug("Не удалось сохранить настройки", exc_info=True)


def fetch_paid_orders(cardinal: Cardinal, max_pages: int = MAX_PAGES) -> list[OrderShortcut]:
    """
    Забирает со страницы продаж все заказы, ожидающие подтверждения.

    Обходит страницы по ``start_from``, пока они есть, но не более
    ``max_pages`` - у активного магазина история продаж бесконечная,
    а зависшие заказы всегда в начале списка.

    :param cardinal: экземпляр ядра.
    :param max_pages: предельное число страниц.

    :return: список заказов со статусом «оплачен».
    """
    orders: list[OrderShortcut] = []
    start_from: str | None = None
    seen_ids: set[str] = set()

    for page in range(max_pages):
        try:
            next_id, page_orders, _locale, _subcats = cardinal.account.get_sales(
                start_from=start_from, include_paid=True,
                include_closed=False, include_refunded=False)
        except Exception:
            logger.warning(f"[{NAME}] не удалось получить список продаж (страница {page + 1})")
            logger.debug("TRACEBACK", exc_info=True)
            break

        for order in page_orders:
            if order.id in seen_ids:
                continue
            seen_ids.add(order.id)
            if order.status is OrderStatuses.PAID:
                orders.append(order)

        if not next_id:
            break
        start_from = next_id
        # Пауза между страницами: подряд идущие запросы к FunPay ловят 429.
        time.sleep(1)

    return orders


def filter_stale(orders: list[OrderShortcut], min_age_hours: int,
                 now: datetime | None = None) -> list[OrderShortcut]:
    """
    Отбирает заказы, висящие дольше заданного времени.

    :param orders: заказы со статусом «оплачен».
    :param min_age_hours: минимальный возраст в часах.
    :param now: текущее время, для тестов.

    :return: заказы старше порога, свежие в конце.
    """
    moment = now or datetime.now()
    threshold = moment - timedelta(hours=min_age_hours)
    stale = [order for order in orders if order.date and order.date <= threshold]
    return sorted(stale, key=lambda order: order.date)


def build_ticket_text(orders: list[OrderShortcut]) -> str:
    """
    Собирает текст заявки в поддержку.

    Формат ровно такой, как принято в форме поддержки: заголовок и нумерованный
    список заказов, по одному в строке.

    :param orders: заказы для включения в заявку.

    :return: текст заявки.
    """
    lines = [TICKET_HEADER, ""]
    for number, order in enumerate(orders, 1):
        lines.append(f"{number} - #{order.id}")
    return "\n".join(lines)


def _age_text(order: OrderShortcut, now: datetime | None = None) -> str:
    """
    Возвращает возраст заказа человекочитаемо.

    :param order: заказ.
    :param now: текущее время, для тестов.

    :return: строка вида ``3д 4ч``.
    """
    delta = (now or datetime.now()) - order.date
    days, seconds = delta.days, delta.seconds
    hours = seconds // 3600
    if days:
        return f"{days}д {hours}ч"
    return f"{hours}ч"


def build_report(orders: list[OrderShortcut], now: datetime | None = None) -> tuple[str, str]:
    """
    Готовит сообщение для Telegram и текст заявки.

    :param orders: зависшие заказы.
    :param now: текущее время, для тестов.

    :return: кортеж ``(текст сообщения, текст заявки)``.
    """
    ticket = build_ticket_text(orders)

    lines = [f"⏳ <b>Заказы без подтверждения: {len(orders)}</b>", ""]
    for number, order in enumerate(orders, 1):
        lines.append(
            f"{number}. <code>#{order.id}</code> - {order.buyer_username}\n"
            f"     {order.price:g} {order.currency or ''} · висит {_age_text(order, now)}\n"
            f"     {order.description[:60] if order.description else ''}")

    lines += [
        "",
        "📋 <b>Текст заявки</b> (нажми, чтобы скопировать):",
        f"<code>{ticket}</code>",
        "",
        "⚠️ <b>Перед отправкой сверь чаты.</b>",
        "Форма требует, чтобы по каждому заказу услуга была оказана, "
        "а переписка однозначно читалась как «покупатель забыл нажать кнопку». "
        "Заказы с претензиями и спорные выноси во <b>второй</b> список формы - "
        "ошибка в первом отклоняет заявку целиком.",
    ]
    return "\n".join(lines), ticket


def _notify(cardinal: Cardinal, text: str) -> None:
    """
    Отправляет отчёт в Telegram.

    :param cardinal: экземпляр ядра.
    :param text: текст отчёта.
    """
    if cardinal.telegram is None:
        return

    keyboard = Keyboard().add(Button("📨 Открыть форму поддержки", url=SUPPORT_FORM_URL))

    from tg_bot import utils as tg_utils
    try:
        cardinal.telegram.send_notification(
            text, keyboard=keyboard,
            notification_type=tg_utils.NotificationTypes.critical)
    except Exception:
        logger.debug("Не удалось отправить отчёт", exc_info=True)


def run_check(cardinal: Cardinal, notify_when_empty: bool = False) -> int:
    """
    Выполняет одну проверку продаж.

    :param cardinal: экземпляр ядра.
    :param notify_when_empty: сообщать ли, когда зависших заказов нет.

    :return: количество найденных зависших заказов.
    """
    settings = load_settings()
    orders = fetch_paid_orders(cardinal)
    stale = filter_stale(orders, settings.min_age_hours)

    settings.last_check = time.time()
    save_settings(settings)

    logger.info(f"[{NAME}] ожидают подтверждения: {len(orders)}, "
                f"из них старше {settings.min_age_hours}ч: {len(stale)}")

    if not stale:
        if notify_when_empty and cardinal.telegram:
            cardinal.telegram.bot.send_message(
                list(cardinal.telegram.notification_settings)[0] if cardinal.telegram.notification_settings else 0,
                f"✅ Заказов без подтверждения старше {settings.min_age_hours}ч нет.")
        return 0

    report, _ticket = build_report(stale)
    _notify(cardinal, report)
    return len(stale)


def _check_loop(cardinal: Cardinal) -> None:
    """
    Цикл автопроверки.

    Проверка идёт не по таймеру от старта, а по времени последней проверки:
    перезапуск бота не должен ни сбрасывать отсчёт, ни вызывать проверку заново.

    :param cardinal: экземпляр ядра.
    """
    # Даём ядру закончить инициализацию: список продаж требует готового аккаунта.
    time.sleep(120)

    while True:
        try:
            settings = load_settings()
            if settings.interval_hours <= 0:
                time.sleep(600)
                continue

            elapsed = time.time() - settings.last_check
            wait = settings.interval_hours * 3600 - elapsed
            if wait > 0:
                time.sleep(min(wait, 3600))
                continue

            run_check(cardinal)
        except Exception:
            logger.debug("Ошибка в цикле проверки", exc_info=True)
            time.sleep(600)


# --- Telegram --------------------------------------------------------------

def _register_telegram(cardinal: Cardinal) -> None:
    """Регистрирует команды."""
    telegram = cardinal.telegram
    if telegram is None:
        return

    def check_now(message: Message) -> None:
        settings = load_settings()
        telegram.bot.send_message(
            message.chat.id,
            f"🔍 Смотрю раздел продаж (заказы старше {settings.min_age_hours}ч)...")

        def worker() -> None:
            try:
                orders = fetch_paid_orders(cardinal)
                stale = filter_stale(orders, settings.min_age_hours)
                if not stale:
                    telegram.bot.send_message(
                        message.chat.id,
                        f"✅ Всего ожидают подтверждения: <b>{len(orders)}</b>\n"
                        f"Из них старше {settings.min_age_hours}ч: <b>нет</b>")
                    return
                report, _ticket = build_report(stale)
                keyboard = Keyboard().add(
                    Button("📨 Открыть форму поддержки", url=SUPPORT_FORM_URL))
                telegram.bot.send_message(message.chat.id, report, reply_markup=keyboard)
            except Exception:
                logger.debug("Ошибка проверки по команде", exc_info=True)
                telegram.bot.send_message(
                    message.chat.id, "❌ Не удалось получить список продаж. Смотри /logs")

        threading.Thread(target=worker, daemon=True).start()

    def set_age(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            telegram.bot.send_message(
                message.chat.id,
                "Формат: <code>/unconfirmed_age 24</code>\nВозраст заказа в часах.")
            return
        settings = load_settings()
        settings.min_age_hours = max(1, int(parts[1]))
        save_settings(settings)
        telegram.bot.send_message(
            message.chat.id,
            f"✅ В список попадают заказы старше <b>{settings.min_age_hours}ч</b>.")

    def set_interval(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            telegram.bot.send_message(
                message.chat.id,
                "Формат: <code>/unconfirmed_every 24</code>\n"
                "Период автопроверки в часах, <code>0</code> отключает.")
            return
        settings = load_settings()
        settings.interval_hours = int(parts[1])
        save_settings(settings)
        telegram.bot.send_message(
            message.chat.id,
            f"✅ Автопроверка каждые <b>{settings.interval_hours}ч</b>."
            if settings.interval_hours else "✅ Автопроверка отключена.")

    telegram.msg_handler(check_now, commands=["unconfirmed"])
    telegram.msg_handler(set_age, commands=["unconfirmed_age"])
    telegram.msg_handler(set_interval, commands=["unconfirmed_every"])

    cardinal.add_telegram_commands(UUID, [
        ("unconfirmed", "заказы без подтверждения", True),
        ("unconfirmed_age", "возраст заказа для списка, часов", False),
        ("unconfirmed_every", "период автопроверки, часов", False),
    ])


def init(cardinal: Cardinal, *args) -> None:
    """Регистрирует команды Telegram."""
    _register_telegram(cardinal)
    settings = load_settings()
    logger.info(f"$MAGENTA[{NAME}]$RESET порог {settings.min_age_hours}ч, "
                f"автопроверка каждые {settings.interval_hours}ч")


def start_loop(cardinal: Cardinal, *args) -> None:
    """Запускает цикл автопроверки после инициализации аккаунта."""
    threading.Thread(target=_check_loop, args=(cardinal,), daemon=True).start()


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_INIT = [start_loop]
