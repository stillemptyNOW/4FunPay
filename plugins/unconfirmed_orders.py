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
from Utils import llm
from Utils.dispute_check import ChatLine, Decision, OrderCase, classify, split

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

    use_ai: bool = False
    """
    Разбирать ли переписки моделью.

    Выключено по умолчанию: требует настроенного ключа, и переписки покупателей
    уходят на сторонний сервер. Включать должен человек, понимающий это.
    """


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
            use_ai=bool(data.get("use_ai", False)),
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
                "use_ai": settings.use_ai,
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


def collect_cases(cardinal: Cardinal, orders: list[OrderShortcut],
                  now: datetime | None = None) -> list[OrderCase]:
    """
    Догружает переписки по заказам и готовит их к разбору моделью.

    Истории берутся одним запросом на всю пачку через ``get_chats_histories``,
    а не по запросу на заказ: у активного магазина зависших заказов бывает
    несколько десятков, и поштучные запросы упёрлись бы в 429.

    :param cardinal: экземпляр ядра.
    :param orders: зависшие заказы.
    :param now: текущее время, для тестов.

    :return: заказы вместе с перепиской.
    """
    moment = now or datetime.now()
    chats_data = {order.chat_id: order.buyer_username for order in orders if order.chat_id}

    histories: dict[int | str, list] = {}
    if chats_data:
        try:
            histories = cardinal.account.get_chats_histories(chats_data)
        except Exception:
            logger.warning(f"[{NAME}] не удалось получить истории чатов")
            logger.debug("TRACEBACK", exc_info=True)

    account_id = cardinal.account.id
    cases = []
    for order in orders:
        messages = histories.get(order.chat_id, [])
        lines = []
        for message in messages:
            text = str(message) if message.text is None else message.text
            if not text:
                continue
            lines.append(ChatLine(
                author=message.author or "",
                text=text,
                is_seller=message.author_id == account_id,
            ))
        cases.append(OrderCase(
            order_id=order.id,
            buyer=order.buyer_username,
            lot=order.description or "",
            age_hours=int((moment - order.date).total_seconds() // 3600),
            lines=lines,
        ))
    return cases


def build_ai_ticket_text(confirm_ids: list[str], dispute_ids: list[str]) -> str:
    """
    Собирает текст заявки с двумя списками, как требует форма поддержки.

    :param confirm_ids: заказы, где покупатель забыл подтвердить.
    :param dispute_ids: заказы, где ситуация неоднозначная.

    :return: текст заявки.
    """
    lines = [TICKET_HEADER, ""]
    for number, order_id in enumerate(confirm_ids, 1):
        lines.append(f"{number} - #{order_id}")

    if dispute_ids:
        lines += ["", "Заказы, по которым ситуация неоднозначная "
                      "(прошу не подтверждать, разбираюсь отдельно):", ""]
        for number, order_id in enumerate(dispute_ids, 1):
            lines.append(f"{number} - #{order_id}")
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


def build_ai_report(orders: list[OrderShortcut], decisions: list[Decision],
                    now: datetime | None = None) -> tuple[str, str]:
    """
    Готовит отчёт с разбором переписок и текст заявки с двумя списками.

    :param orders: зависшие заказы.
    :param decisions: решения по ним.
    :param now: текущее время, для тестов.

    :return: кортеж ``(текст сообщения, текст заявки)``.
    """
    confirm, dispute = split(decisions)
    by_id = {order.id: order for order in orders}
    ticket = build_ai_ticket_text([d.order_id for d in confirm],
                                  [d.order_id for d in dispute])

    lines = [f"🤖 <b>Разбор переписок: {len(decisions)} заказов</b>", ""]

    if confirm:
        lines.append(f"✅ <b>Забыли подтвердить ({len(confirm)})</b> — в первый список:")
        for decision in confirm:
            order = by_id.get(decision.order_id)
            suffix = f" · {order.price:g} {order.currency or ''}" if order else ""
            lines.append(f"  <code>#{decision.order_id}</code>{suffix}\n"
                         f"     <i>{decision.reason}</i>")
        lines.append("")

    if dispute:
        lines.append(f"⚠️ <b>Спорные ({len(dispute)})</b> — во второй список:")
        for decision in dispute:
            order = by_id.get(decision.order_id)
            suffix = f" · {order.buyer_username}" if order else ""
            lines.append(f"  <code>#{decision.order_id}</code>{suffix}\n"
                         f"     <i>{decision.reason}</i>")
        lines.append("")

    lines += [
        "📋 <b>Текст заявки</b> (нажми, чтобы скопировать):",
        f"<code>{ticket}</code>",
        "",
        "⚠️ <b>Разбор сделала модель — проверь первый список глазами.</b>",
        "Она настроена перестраховываться: при любом сомнении заказ уходит "
        "в спорные. Но ответственность за заявку на тебе, а ошибка в первом "
        "списке отклоняет её целиком.",
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
        return 0

    report, _ticket = analyze(cardinal, stale, settings)
    _notify(cardinal, report)
    return len(stale)


def analyze(cardinal: Cardinal, stale: list[OrderShortcut],
            settings: Settings) -> tuple[str, str]:
    """
    Готовит отчёт по зависшим заказам, при необходимости разобрав переписки.

    Если разбор моделью выключен или недоступен, возвращается обычный отчёт
    одним списком: отсутствие ИИ не должно лишать владельца самой сводки.

    :param cardinal: экземпляр ядра.
    :param stale: зависшие заказы.
    :param settings: настройки плагина.

    :return: кортеж ``(текст сообщения, текст заявки)``.
    """
    if not settings.use_ai:
        return build_report(stale)

    if not llm.load_config().is_ready:
        logger.warning(f"[{NAME}] разбор включён, но клиент модели не настроен")
        report, ticket = build_report(stale)
        return ("⚠️ Разбор переписок включён, но модель не настроена "
                "(<code>/ai_key</code>, <code>/ai_test</code>).\n\n" + report), ticket

    try:
        cases = collect_cases(cardinal, stale)
        decisions = classify(cases)
        return build_ai_report(stale, decisions)
    except Exception:
        logger.warning(f"[{NAME}] разбор не удался, отдаю обычный список")
        logger.debug("TRACEBACK", exc_info=True)
        report, ticket = build_report(stale)
        return ("⚠️ Не удалось разобрать переписки, список без разбора.\n\n" + report), ticket


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
        note = " и разбираю переписки" if settings.use_ai else ""
        telegram.bot.send_message(
            message.chat.id,
            f"🔍 Смотрю раздел продаж (заказы старше {settings.min_age_hours}ч){note}...")

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
                report, _ticket = analyze(cardinal, stale, settings)
                keyboard = Keyboard().add(
                    Button("📨 Открыть форму поддержки", url=SUPPORT_FORM_URL))
                telegram.bot.send_message(message.chat.id, report, reply_markup=keyboard)
            except Exception:
                logger.debug("Ошибка проверки по команде", exc_info=True)
                telegram.bot.send_message(
                    message.chat.id, "❌ Не удалось получить список продаж. Смотри /logs")

        threading.Thread(target=worker, daemon=True).start()

    def toggle_ai(message: Message) -> None:
        settings = load_settings()
        settings.use_ai = not settings.use_ai
        save_settings(settings)
        if not settings.use_ai:
            telegram.bot.send_message(message.chat.id, "🤖 Разбор переписок выключен.")
            return
        ready = llm.load_config().is_ready
        telegram.bot.send_message(
            message.chat.id,
            "🤖 <b>Разбор переписок включён.</b>\n\n"
            "Перед составлением заявки бот отправит переписки по зависшим заказам "
            "модели и разложит заказы на два списка.\n\n"
            "⚠️ Переписки с покупателями при этом уходят на сторонний сервер "
            "модели. Учитывай это.\n\n"
            + ("✅ Клиент модели настроен." if ready else
               "❌ Клиент не настроен: задай ключ командой <code>/ai_key</code> "
               "и проверь связь через <code>/ai_test</code>."))

    def set_ai_key(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        # Сообщение с ключом удаляем сразу, как и в случае с golden_key.
        try:
            telegram.bot.delete_message(message.chat.id, message.id)
        except Exception:
            logger.debug("Не удалось удалить сообщение с ключом", exc_info=True)

        if len(parts) < 2 or not parts[1].strip():
            telegram.bot.send_message(
                message.chat.id, "Формат: <code>/ai_key sk_live_...</code>")
            return

        config = llm.load_config()
        config.api_key = parts[1].strip()
        llm.save_config(config)
        telegram.bot.send_message(
            message.chat.id,
            f"✅ Ключ сохранён: <code>{llm.mask_key(config.api_key)}</code>\n"
            f"Файл: <code>{llm.SETTINGS_FILE}</code> (права 600, в git не попадает).\n\n"
            f"Проверь связь: <code>/ai_test</code>")

    def set_ai_url(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            telegram.bot.send_message(
                message.chat.id,
                f"Формат: <code>/ai_url http://host:port/v1</code>\n"
                f"Сейчас: <code>{llm.load_config().base_url}</code>")
            return
        config = llm.load_config()
        config.base_url = parts[1].strip().rstrip("/")
        llm.save_config(config)
        telegram.bot.send_message(message.chat.id, f"✅ URL: <code>{config.base_url}</code>")

    def set_ai_model(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            telegram.bot.send_message(
                message.chat.id,
                f"Формат: <code>/ai_model gpt-4o-mini</code>\n"
                f"Сейчас: <code>{llm.load_config().model}</code>\n"
                f"Список доступных покажет <code>/ai_test</code>")
            return
        config = llm.load_config()
        config.model = parts[1].strip()
        llm.save_config(config)
        telegram.bot.send_message(message.chat.id, f"✅ Модель: <code>{config.model}</code>")

    def test_ai(message: Message) -> None:
        config = llm.load_config()
        telegram.bot.send_message(
            message.chat.id,
            f"🤖 <b>Настройки модели</b>\n\n"
            f"URL: <code>{config.base_url}</code>\n"
            f"Модель: <code>{config.model}</code>\n"
            f"Ключ: <code>{llm.mask_key(config.resolved_key())}</code>\n\n"
            f"Проверяю связь...")

        def worker() -> None:
            client = llm.LLMClient(config)
            try:
                models = client.list_models()
            except llm.LLMError as exc:
                telegram.bot.send_message(
                    message.chat.id, f"❌ Список моделей недоступен:\n<code>{exc}</code>")
                return

            shown = "\n".join(f"  • <code>{name}</code>" for name in models[:25])
            more = f"\n<i>...и ещё {len(models) - 25}</i>" if len(models) > 25 else ""
            telegram.bot.send_message(
                message.chat.id, f"✅ Доступно моделей: <b>{len(models)}</b>\n{shown}{more}")

            try:
                answer = client.complete(
                    "Отвечай одним словом.",
                    "Ответь словом: работает", max_tokens=20)
                telegram.bot.send_message(
                    message.chat.id,
                    f"✅ Модель <code>{config.model}</code> отвечает:\n"
                    f"<code>{answer.strip()[:200]}</code>")
            except llm.LLMError as exc:
                telegram.bot.send_message(
                    message.chat.id,
                    f"❌ Модель <code>{config.model}</code> не отвечает:\n<code>{exc}</code>\n\n"
                    f"Выбери другую из списка выше: <code>/ai_model ИМЯ</code>")

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
    telegram.msg_handler(toggle_ai, commands=["ai_check"])
    telegram.msg_handler(set_ai_key, commands=["ai_key"])
    telegram.msg_handler(set_ai_url, commands=["ai_url"])
    telegram.msg_handler(set_ai_model, commands=["ai_model"])
    telegram.msg_handler(test_ai, commands=["ai_test"])

    cardinal.add_telegram_commands(UUID, [
        ("unconfirmed", "заказы без подтверждения", True),
        ("ai_check", "вкл/выкл разбор переписок моделью", True),
        ("ai_test", "проверить связь с моделью", False),
        ("ai_key", "задать ключ API модели", False),
        ("ai_url", "задать URL API модели", False),
        ("ai_model", "выбрать модель", False),
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
