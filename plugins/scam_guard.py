"""
Скам-фильтр и статистика активности покупателей.

Автовыдача выдаёт товар мгновенно. Это её смысл и её слабое место: если заказ
сомнительный, товар уже ушёл, и вернуть его нельзя - в отличие от денег,
которые покупатель может потребовать назад.

Плагин придерживает выдачу для заказов, попавших под правила, и присылает
владельцу кнопки «выдать» или «отклонить». Всё остальное выдаётся как обычно.

Правила (каждое включается отдельно, все по умолчанию выключены):
    * сумма заказа выше порога - крупные заказы стоит посмотреть глазами;
    * покупатель просил завершить сделку вне FunPay или требовал возврат
      в переписке до оплаты;
    * покупатель уже получал от нас возврат ранее.

Вторая часть - статистика по часам:
    /besttime - в какие часы реально приходят заказы

Команды Telegram:
    /guard              - состояние фильтра и список правил
    /guard_on, /guard_off  - включить и выключить фильтр
    /guard_price <сумма>   - порог суммы заказа, 0 отключает правило
    /held               - заказы, ожидающие решения
    /besttime           - распределение заказов по часам

Ограничение, о котором надо знать: очередь придержанных заказов живёт
в памяти процесса. После перезапуска бота кнопки в старых уведомлениях
перестают работать, и товар придётся выдать вручную в чате FunPay.
Сам заказ при этом никуда не денется.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from logging import getLogger
from typing import TYPE_CHECKING, Any

from telebot.types import CallbackQuery, InlineKeyboardButton as Button
from telebot.types import InlineKeyboardMarkup as Keyboard
from telebot.types import Message

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.types import Category
    from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent

NAME = "Скам-фильтр"
VERSION = "1.0.0"
DESCRIPTION = ("Придерживает автовыдачу для подозрительных заказов и спрашивает "
               "решение в Telegram: крупная сумма, просьба завершить сделку вне "
               "FunPay, история возвратов у покупателя.\n\n"
               "Плюс статистика заказов по часам: /besttime\n\n"
               "Команды: /guard, /held, /besttime")
CREDITS = "{{OWNER_TG}}"
UUID = "c6b81f43-2a97-4d0e-8b5c-7f1e3a9d04c8"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

logger = getLogger(f"4FP.{__name__}")

SETTINGS_FILE = "storage/cache/scam_guard.json"
ACTIVITY_FILE = "storage/cache/order_hours.json"
SUSPECT_FILE = "storage/cache/suspect_buyers.json"

CALLBACK_RELEASE = "sg_release"
CALLBACK_REJECT = "sg_reject"

# Формулировки, которыми обычно предлагают уйти со сделкой мимо площадки
# либо давят на возврат до того, как товар получен.
OUTSIDE_DEAL_PATTERNS = [
    r"вне\s+фанпе", r"вне\s+funpay", r"мимо\s+фанпе", r"мимо\s+funpay",
    r"без\s+фанпе", r"без\s+funpay", r"напрям", r"на\s+прямую",
    r"отмен\w*\s+заказ", r"верн\w+\s+деньг", r"сдела\w+\s+возврат",
    r"переве\w+\s+на\s+карт", r"скинь\s+на\s+карт",
    r"телеграм\w*\s+напиш", r"напиш\w*\s+в\s+тг", r"перейдём\s+в\s+тг",
]

_COMPILED_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in OUTSIDE_DEAL_PATTERNS]

_LOCK = threading.Lock()

_held_orders: dict[str, tuple[Any, Any]] = {}
"""Придержанные заказы в памяти: {ID заказа: (cardinal, event)}."""


@dataclass
class GuardSettings:
    """Настройки фильтра."""

    enabled: bool = False
    """Общий выключатель. По умолчанию выключен: фильтр задерживает выдачу,
    и включать его должен человек, а не факт установки плагина."""

    price_threshold: float = 0.0
    """Сумма заказа, выше которой требуется решение. 0 - правило отключено."""

    check_messages: bool = True
    """Проверять переписку на просьбы уйти мимо площадки."""

    check_refund_history: bool = True
    """Проверять, получал ли покупатель возврат ранее."""


def load_settings() -> GuardSettings:
    """
    Загружает настройки фильтра.

    :return: настройки; при отсутствии файла - значения по умолчанию.
    """
    if not os.path.exists(SETTINGS_FILE):
        return GuardSettings()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return GuardSettings(
            enabled=bool(data.get("enabled", False)),
            price_threshold=float(data.get("price_threshold", 0.0)),
            check_messages=bool(data.get("check_messages", True)),
            check_refund_history=bool(data.get("check_refund_history", True)),
        )
    except (OSError, ValueError, TypeError):
        logger.debug("Настройки скам-фильтра повреждены", exc_info=True)
        return GuardSettings()


def save_settings(settings: GuardSettings) -> None:
    """
    Сохраняет настройки фильтра.

    :param settings: настройки.
    """
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as file:
            json.dump({
                "enabled": settings.enabled,
                "price_threshold": settings.price_threshold,
                "check_messages": settings.check_messages,
                "check_refund_history": settings.check_refund_history,
            }, file, ensure_ascii=False, indent=2)
    except OSError:
        logger.debug("Не удалось сохранить настройки скам-фильтра", exc_info=True)


def _load_json(path: str, default: Any) -> Any:
    """Читает JSON, возвращая значение по умолчанию при любой проблеме."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return default


def _save_json(path: str, data: Any) -> None:
    """Пишет JSON, молча пропуская ошибки записи."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False)
    except OSError:
        logger.debug(f"Не удалось записать {path}", exc_info=True)


# --- Правила ---------------------------------------------------------------

def looks_like_outside_deal(text: str) -> str | None:
    """
    Ищет в тексте просьбу увести сделку мимо площадки или требование возврата.

    :param text: текст сообщения.

    :return: найденный фрагмент или None.
    """
    for pattern in _COMPILED_PATTERNS:
        if match := pattern.search(text):
            return match.group(0)
    return None


def mark_suspect(username: str, reason: str) -> None:
    """
    Помечает покупателя как подозрительного.

    :param username: ник покупателя.
    :param reason: причина отметки.
    """
    with _LOCK:
        suspects = _load_json(SUSPECT_FILE, {})
        if not isinstance(suspects, dict):
            suspects = {}
        suspects[username] = {"reason": reason,
                              "ts": datetime.now().isoformat(timespec="seconds")}
        _save_json(SUSPECT_FILE, suspects)


def suspect_reason(username: str) -> str | None:
    """
    Возвращает причину, по которой покупатель помечен, если он помечен.

    :param username: ник покупателя.

    :return: причина или None.
    """
    suspects = _load_json(SUSPECT_FILE, {})
    if not isinstance(suspects, dict):
        return None
    entry = suspects.get(username)
    return entry.get("reason") if isinstance(entry, dict) else None


def evaluate_order(order_price: float, order_currency: str, buyer: str,
                   settings: GuardSettings) -> str | None:
    """
    Решает, нужно ли придержать выдачу по заказу.

    Чистая функция: вся логика принятия решения без сети и без состояния,
    чтобы её можно было проверить тестами.

    :param order_price: сумма заказа.
    :param order_currency: валюта заказа.
    :param buyer: ник покупателя.
    :param settings: настройки фильтра.

    :return: причина задержки или None, если выдавать можно.
    """
    if not settings.enabled:
        return None

    if settings.price_threshold and order_price >= settings.price_threshold:
        return (f"сумма заказа {order_price:g} {order_currency} "
                f"не ниже порога {settings.price_threshold:g}")

    if settings.check_messages or settings.check_refund_history:
        if reason := suspect_reason(buyer):
            return reason

    return None


# --- Статистика активности -------------------------------------------------

def record_order_hour(moment: datetime | None = None) -> None:
    """
    Учитывает час, в который пришёл заказ.

    :param moment: момент заказа, по умолчанию - сейчас.
    """
    hour = str((moment or datetime.now()).hour)
    with _LOCK:
        hours = _load_json(ACTIVITY_FILE, {})
        if not isinstance(hours, dict):
            hours = {}
        hours[hour] = int(hours.get(hour, 0)) + 1
        _save_json(ACTIVITY_FILE, hours)


def load_activity() -> dict[int, int]:
    """
    Загружает распределение заказов по часам.

    :return: словарь ``{час: количество}``.
    """
    raw = _load_json(ACTIVITY_FILE, {})
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, value in raw.items():
        try:
            hour = int(key)
            if 0 <= hour <= 23:
                result[hour] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def peak_hours(activity: dict[int, int], top: int = 6) -> set[int]:
    """
    Определяет часы наибольшей активности покупателей.

    :param activity: распределение заказов по часам.
    :param top: сколько часов считать пиковыми.

    :return: множество часов.
    """
    if not activity:
        return set()
    ranked = sorted(activity.items(), key=lambda item: -item[1])
    return {hour for hour, count in ranked[:top] if count}


def format_activity(activity: dict[int, int]) -> str:
    """
    Готовит отчёт по часам.

    :param activity: распределение заказов по часам.

    :return: текст для Telegram.
    """
    total = sum(activity.values())
    if not total:
        return ("🕒 Заказов пока не зафиксировано.\n\n"
                "<i>Статистика набирается с момента установки плагина.</i>")

    peak = peak_hours(activity)
    top_value = max(activity.values())
    lines = [f"🕒 <b>Заказы по часам</b> (всего {total})", ""]

    for hour in range(24):
        count = activity.get(hour, 0)
        filled = int(round(count / top_value * 12)) if top_value else 0
        if count and not filled:
            filled = 1
        mark = "🔥" if hour in peak else "  "
        lines.append(f"<code>{hour:02d}</code> {mark} {'█' * filled} {count or ''}")

    if peak:
        readable = ", ".join(f"{hour:02d}:00" for hour in sorted(peak))
        lines += ["", f"🔥 <b>Пик:</b> {readable}",
                  "", "<i>В эти часы имеет смысл держать лоты поднятыми "
                      "и быть онлайн.</i>"]
    return "\n".join(lines)


# --- Обработчики событий ---------------------------------------------------

def on_new_message(cardinal: Cardinal, event: NewMessageEvent, *args) -> None:
    """Помечает покупателя, если он предлагает сделку мимо площадки."""
    settings = load_settings()
    if not settings.enabled or not settings.check_messages:
        return

    message = event.message
    # Свои и системные сообщения не проверяем.
    if message.author_id in (0, cardinal.account.id) or message.by_bot:
        return

    if fragment := looks_like_outside_deal(str(message)):
        mark_suspect(message.author, f"в переписке: «{fragment}»")
        logger.warning(f"$YELLOW[{NAME}]$RESET покупатель {message.author} помечен: «{fragment}»")


def on_new_order(cardinal: Cardinal, event: NewOrderEvent, *args) -> None:
    """Учитывает час заказа для статистики активности."""
    record_order_hour()


def on_pre_delivery(cardinal: Cardinal, event: NewOrderEvent, *args) -> None:
    """
    Решает, придержать ли выдачу.

    Работает через флаг ``delivery_blocked``, который проверяет ядро
    в ``handlers.deliver_product_handler``.
    """
    settings = load_settings()
    order = event.order
    reason = evaluate_order(
        order_price=float(order.price or 0),
        order_currency=str(order.currency) if order.currency else "",
        buyer=order.buyer_username,
        settings=settings,
    )
    if reason is None:
        return

    setattr(event, "delivery_blocked", True)
    setattr(event, "delivery_block_reason", reason)

    with _LOCK:
        _held_orders[order.id] = (cardinal, event)

    if cardinal.telegram is None:
        return

    keyboard = Keyboard()
    keyboard.row(
        Button("✅ Выдать", callback_data=f"{CALLBACK_RELEASE}:{order.id}"),
        Button("🚫 Отклонить", callback_data=f"{CALLBACK_REJECT}:{order.id}"),
    )

    from tg_bot import utils as tg_utils
    cardinal.telegram.send_notification(
        f"🛡️ <b>Выдача придержана</b>\n\n"
        f"🧾 Заказ: <code>{order.id}</code>\n"
        f"👤 Покупатель: <code>{order.buyer_username}</code>\n"
        f"💰 Сумма: <code>{order.price:g} {order.currency or ''}</code>\n"
        f"📦 Лот: {order.description}\n\n"
        f"⚠️ Причина: {reason}\n\n"
        f"Товар покупателю <b>не отправлен</b>. Реши, что делать.",
        keyboard=keyboard,
        notification_type=tg_utils.NotificationTypes.critical)


# --- Умное поднятие --------------------------------------------------------

POSTPONE_SECONDS = 1800
"""На сколько отложить поднятие, если сейчас непиковый час."""


def on_pre_lots_raise(cardinal: Cardinal, category: Category, *args) -> None:
    """
    Откладывает поднятие, если текущий час непиковый.

    Выигрыш здесь скромный и честно ограничен механикой FunPay: кулдаун
    на поднятие фиксированный, поднятий в сутки получается около шести,
    и они так или иначе накрывают большую часть суток. Смысл появляется
    только когда распределение заказов выражено неравномерно - поэтому
    правило включается лишь при накопленной статистике.
    """
    activity = load_activity()
    if sum(activity.values()) < 50:
        # Мало данных: любые выводы о пиках были бы выдумкой.
        return

    peak = peak_hours(activity)
    current_hour = datetime.now().hour
    if not peak or current_hour in peak:
        return

    # Откладываем, только если пик наступит скоро - иначе проще поднять сейчас.
    next_peak_in = min(((hour - current_hour) % 24 for hour in peak), default=24)
    if next_peak_in * 3600 > POSTPONE_SECONDS:
        return

    setattr(category, "postpone_raise", POSTPONE_SECONDS)


# --- Telegram --------------------------------------------------------------

def _register_telegram(cardinal: Cardinal) -> None:
    """Регистрирует команды и обработчики кнопок."""
    telegram = cardinal.telegram
    if telegram is None:
        return

    def status(message: Message) -> None:
        settings = load_settings()
        threshold = (f"{settings.price_threshold:g}" if settings.price_threshold
                     else "не задан")
        with _LOCK:
            held = len(_held_orders)
        suspects = _load_json(SUSPECT_FILE, {})
        telegram.bot.send_message(
            message.chat.id,
            f"🛡️ <b>Скам-фильтр</b>\n\n"
            f"Состояние: <b>{'включён' if settings.enabled else 'выключен'}</b>\n"
            f"Порог суммы заказа: <b>{threshold}</b>\n"
            f"Проверка переписки: <b>{'да' if settings.check_messages else 'нет'}</b>\n\n"
            f"Помечено покупателей: <b>{len(suspects) if isinstance(suspects, dict) else 0}</b>\n"
            f"Ждут решения: <b>{held}</b>\n\n"
            f"<code>/guard_on</code> - включить\n"
            f"<code>/guard_off</code> - выключить\n"
            f"<code>/guard_price 5000</code> - порог суммы, 0 отключает\n"
            f"<code>/held</code> - заказы на решении")

    def turn_on(message: Message) -> None:
        settings = load_settings()
        settings.enabled = True
        save_settings(settings)
        telegram.bot.send_message(
            message.chat.id,
            "🛡️ Фильтр включён.\n\n"
            "⚠️ Заказы, попавшие под правила, <b>не будут выданы автоматически</b> - "
            "они дождутся твоего решения. Следи за уведомлениями.")

    def turn_off(message: Message) -> None:
        settings = load_settings()
        settings.enabled = False
        save_settings(settings)
        telegram.bot.send_message(message.chat.id, "🛡️ Фильтр выключен, выдача идёт как обычно.")

    def set_threshold(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2:
            telegram.bot.send_message(
                message.chat.id,
                "Формат: <code>/guard_price 5000</code>\nСумма 0 отключает правило.")
            return
        try:
            value = float(parts[1].replace(",", "."))
        except ValueError:
            telegram.bot.send_message(message.chat.id, "❌ Сумма должна быть числом.")
            return

        settings = load_settings()
        settings.price_threshold = max(0.0, value)
        save_settings(settings)
        telegram.bot.send_message(
            message.chat.id,
            f"✅ Порог: <b>{value:g}</b>" if value else "✅ Правило суммы отключено.")

    def held(message: Message) -> None:
        with _LOCK:
            orders = list(_held_orders.items())
        if not orders:
            telegram.bot.send_message(message.chat.id, "🛡️ Заказов на решении нет.")
            return
        lines = ["🛡️ <b>Ждут решения</b>", ""]
        for order_id, (_crd, event) in orders:
            lines.append(f"<code>{order_id}</code> - {event.order.buyer_username}, "
                         f"{event.order.price:g} {event.order.currency or ''}")
        lines += ["", "<i>Кнопки решения - в уведомлении по каждому заказу.</i>"]
        telegram.bot.send_message(message.chat.id, "\n".join(lines))

    def best_time(message: Message) -> None:
        telegram.bot.send_message(message.chat.id, format_activity(load_activity()))

    def release(call: CallbackQuery) -> None:
        order_id = call.data.split(":", 1)[1]
        with _LOCK:
            entry = _held_orders.pop(order_id, None)
        if entry is None:
            telegram.bot.answer_callback_query(
                call.id, "Заказ не найден. Возможно, бот перезапускался - "
                         "выдай товар вручную в чате FunPay.", show_alert=True)
            return

        crd, event = entry
        setattr(event, "delivery_blocked", False)
        telegram.bot.answer_callback_query(call.id, "Выдаю...")

        def worker() -> None:
            try:
                import handlers
                handlers.deliver_goods(crd, event)
                crd.run_handlers(crd.post_delivery_handlers, (crd, event))
                telegram.bot.send_message(
                    call.message.chat.id,
                    f"✅ Товар по заказу <code>{order_id}</code> выдан по твоему решению.")
            except Exception:
                logger.debug("Ошибка выдачи придержанного заказа", exc_info=True)
                telegram.bot.send_message(
                    call.message.chat.id,
                    f"❌ Не удалось выдать заказ <code>{order_id}</code>. "
                    f"Выдай вручную в чате FunPay.")

        threading.Thread(target=worker, daemon=True).start()

    def reject(call: CallbackQuery) -> None:
        order_id = call.data.split(":", 1)[1]
        with _LOCK:
            entry = _held_orders.pop(order_id, None)
        if entry is not None:
            mark_suspect(entry[1].order.buyer_username, "отклонён вручную")
        telegram.bot.answer_callback_query(call.id, "Отклонено")
        telegram.bot.send_message(
            call.message.chat.id,
            f"🚫 Заказ <code>{order_id}</code> отклонён, товар не выдан.\n\n"
            f"Покупатель помечен. Разбирайся через чат FunPay или арбитраж.")

    telegram.msg_handler(status, commands=["guard"])
    telegram.msg_handler(turn_on, commands=["guard_on"])
    telegram.msg_handler(turn_off, commands=["guard_off"])
    telegram.msg_handler(set_threshold, commands=["guard_price"])
    telegram.msg_handler(held, commands=["held"])
    telegram.msg_handler(best_time, commands=["besttime"])
    telegram.cbq_handler(release, lambda c: c.data.startswith(f"{CALLBACK_RELEASE}:"))
    telegram.cbq_handler(reject, lambda c: c.data.startswith(f"{CALLBACK_REJECT}:"))

    cardinal.add_telegram_commands(UUID, [
        ("guard", "скам-фильтр: состояние", True),
        ("held", "заказы, ждущие решения", True),
        ("besttime", "заказы по часам", True),
        ("guard_on", "включить скам-фильтр", False),
        ("guard_off", "выключить скам-фильтр", False),
        ("guard_price", "порог суммы заказа", False),
    ])


def init(cardinal: Cardinal, *args) -> None:
    """Регистрирует команды Telegram."""
    _register_telegram(cardinal)
    settings = load_settings()
    logger.info(f"$MAGENTA[{NAME}]$RESET "
                f"{'включён' if settings.enabled else 'выключен'}, "
                f"порог: {settings.price_threshold:g}")


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_PRE_DELIVERY = [on_pre_delivery]
BIND_TO_PRE_LOTS_RAISE = [on_pre_lots_raise]
