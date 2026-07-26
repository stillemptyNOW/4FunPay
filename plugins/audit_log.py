"""
Аудит действий бота.

Ведёт отдельный журнал того, что бот сделал с аккаунтом и деньгами: заказы,
выдача товара, поднятие лотов, изменение состояния лотов, отправленные
сообщения. В отличие от logs/log.log, который ротируется по 20 МБ и содержит
всю отладку, этот журнал компактный, машиночитаемый (JSON Lines) и рассчитан
на то, что его читают через месяц, разбираясь в спорном заказе.

Куда пишет:
    storage/audit/audit-YYYY-MM.jsonl   - по файлу на месяц

Команды Telegram:
    /audit          - последние записи
    /audit_export   - выгрузить журнал текущего месяца файлом

Что НЕ попадает в журнал: golden_key, токен Telegram, выданные покупателям
товары (ключи, пароли от аккаунтов). Для товаров пишется только их количество -
иначе журнал сам стал бы файлом с товарами, который нельзя никому показывать.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from enum import StrEnum
from logging import getLogger
from typing import TYPE_CHECKING, Any

from telebot.types import InputFile, Message

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI import types
    from FunPayAPI.updater.events import NewOrderEvent, OrderStatusChangedEvent

NAME = "Аудит действий"
VERSION = "1.0.0"
DESCRIPTION = ("Ведёт отдельный компактный журнал действий бота в формате JSON Lines: "
               "заказы, выдача товара, поднятие и изменение состояния лотов. "
               "Секреты и содержимое выданных товаров в журнал не попадают.\n\n"
               "Команды: /audit, /audit_export")
CREDITS = "{{OWNER_TG}}"
UUID = "7c1f4a90-3e52-4b8d-9a17-2d6c8f0b5e41"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

logger = getLogger(f"4FP.{__name__}")

AUDIT_DIR = "storage/audit"
MAX_TAIL_RECORDS = 15
"""Сколько последних записей показывает /audit."""


class Action(StrEnum):
    """Тип записи в журнале аудита."""

    BOT_STARTED = "bot_started"
    NEW_ORDER = "new_order"
    ORDER_STATUS = "order_status_changed"
    DELIVERED = "product_delivered"
    DELIVERY_FAILED = "delivery_failed"
    LOTS_RAISED = "lots_raised"
    LOT_STATE = "lot_state_changed"


def _log_path(moment: datetime | None = None) -> str:
    """
    Возвращает путь до файла журнала за указанный месяц.

    :param moment: момент времени, по умолчанию - сейчас.

    :return: путь до файла.
    """
    moment = moment or datetime.now()
    return os.path.join(AUDIT_DIR, f"audit-{moment:%Y-%m}.jsonl")


def write(action: Action, **fields: Any) -> None:
    """
    Добавляет запись в журнал аудита.

    Ошибка записи не должна ронять обработчик события: аудит - вспомогательная
    функция, из-за неё бот не должен перестать выдавать товар.

    :param action: тип действия.
    :param fields: дополнительные поля записи.
    """
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": str(action),
        **fields,
    }
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        with open(_log_path(), "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("Не удалось записать в журнал аудита", exc_info=True)


def _format_record(record: dict) -> str:
    """
    Готовит запись журнала к показу в Telegram.

    :param record: запись журнала.

    :return: строка для отправки.
    """
    titles = {
        Action.BOT_STARTED: "запуск бота",
        Action.NEW_ORDER: "новый заказ",
        Action.ORDER_STATUS: "статус заказа",
        Action.DELIVERED: "выдан товар",
        Action.DELIVERY_FAILED: "ошибка выдачи",
        Action.LOTS_RAISED: "лоты подняты",
        Action.LOT_STATE: "состояние лота",
    }
    action = record.get("action", "?")
    title = titles.get(action, action)
    timestamp = record.get("ts", "")[:19].replace("T", " ")

    details = []
    for key in ("order_id", "buyer", "lot", "amount", "left", "status", "category", "state", "reason"):
        if (value := record.get(key)) not in (None, ""):
            details.append(f"{key}={value}")

    tail = f" | {', '.join(details)}" if details else ""
    return f"<code>{timestamp}</code> <b>{title}</b>{tail}"


def read_tail(limit: int = MAX_TAIL_RECORDS) -> list[dict]:
    """
    Читает последние записи журнала, при необходимости заглядывая в прошлый месяц.

    :param limit: сколько записей вернуть.

    :return: список записей, свежие в конце.
    """
    files = sorted(glob.glob(os.path.join(AUDIT_DIR, "audit-*.jsonl")))
    records: list[dict] = []
    # Идём от свежего файла к старым, пока не наберём нужное количество.
    for path in reversed(files):
        try:
            with open(path, "r", encoding="utf-8") as file:
                lines = file.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(records) >= limit:
                break
        if len(records) >= limit:
            break
    return list(reversed(records))


# --- Обработчики событий ---------------------------------------------------

def on_bot_started(cardinal: Cardinal, *args) -> None:
    """Фиксирует запуск бота."""
    write(Action.BOT_STARTED,
          version=cardinal.VERSION,
          account=cardinal.account.username,
          account_id=cardinal.account.id)


def on_new_order(cardinal: Cardinal, event: NewOrderEvent, *args) -> None:
    """Фиксирует новый заказ."""
    order = event.order
    write(Action.NEW_ORDER,
          order_id=order.id,
          buyer=order.buyer_username,
          buyer_id=order.buyer_id,
          amount=order.amount,
          price=order.price,
          currency=str(order.currency) if order.currency else None,
          lot=order.description)


def on_order_status_changed(cardinal: Cardinal, event: OrderStatusChangedEvent, *args) -> None:
    """Фиксирует смену статуса заказа."""
    order = event.order
    write(Action.ORDER_STATUS,
          order_id=order.id,
          buyer=order.buyer_username,
          status=str(order.status))


def on_delivery(cardinal: Cardinal, event: NewOrderEvent, *args) -> None:
    """
    Фиксирует выдачу товара.

    Атрибуты выставляет ``handlers.setup_event_attributes_handler``:
    ``delivered``, ``error``, ``error_text``, ``goods_delivered``, ``goods_left``.

    Содержимое выданного товара (``delivery_text``) в журнал НЕ пишется -
    только количество. Иначе журнал сам стал бы файлом с ключами и паролями,
    который нельзя никому показывать, и /audit_export стал бы опасной командой.
    """
    order = event.order
    left = getattr(event, "goods_left", None)

    if getattr(event, "error", 0):
        write(Action.DELIVERY_FAILED,
              order_id=order.id,
              buyer=order.buyer_username,
              lot=order.description,
              reason=getattr(event, "error_text", None))
        return

    if not getattr(event, "delivered", False):
        return

    write(Action.DELIVERED,
          order_id=order.id,
          buyer=order.buyer_username,
          lot=order.description,
          amount=getattr(event, "goods_delivered", order.amount),
          # -1 в goods_left означает "товарный файл не используется".
          left=None if left == -1 else left)


def on_lots_raised(cardinal: Cardinal, category: types.Category, *args) -> None:
    """Фиксирует поднятие лотов категории."""
    write(Action.LOTS_RAISED, category=category.name, category_id=category.id)


# --- Telegram --------------------------------------------------------------

def _register_telegram(cardinal: Cardinal) -> None:
    """Регистрирует команды /audit и /audit_export."""
    telegram = cardinal.telegram
    if telegram is None:
        return

    def send_tail(message: Message) -> None:
        records = read_tail()
        if not records:
            telegram.bot.send_message(message.chat.id, "📋 Журнал аудита пока пуст.")
            return
        header = f"📋 <b>Последние {len(records)} записей аудита</b>\n\n"
        body = "\n".join(_format_record(record) for record in records)
        telegram.bot.send_message(message.chat.id, header + body)

    def export(message: Message) -> None:
        path = _log_path()
        if not os.path.exists(path):
            telegram.bot.send_message(message.chat.id, "📋 За текущий месяц записей нет.")
            return
        with open(path, "rb") as file:
            telegram.bot.send_document(
                message.chat.id, InputFile(file),
                caption=f"📋 Журнал аудита за {datetime.now():%B %Y}\n\n"
                        f"Формат: JSON Lines, одна запись на строку.")

    telegram.msg_handler(send_tail, commands=["audit"])
    telegram.msg_handler(export, commands=["audit_export"])

    cardinal.add_telegram_commands(UUID, [
        ("audit", "последние записи аудита", True),
        ("audit_export", "выгрузить журнал аудита", True),
    ])


def init(cardinal: Cardinal, *args) -> None:
    """Готовит каталог журнала и регистрирует команды Telegram."""
    os.makedirs(AUDIT_DIR, exist_ok=True)
    _register_telegram(cardinal)
    logger.info(f"$MAGENTA[{NAME}]$RESET журнал: {_log_path()}")


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [on_bot_started]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_ORDER_STATUS_CHANGED = [on_order_status_changed]
BIND_TO_POST_DELIVERY = [on_delivery]
BIND_TO_POST_LOTS_RAISE = [on_lots_raised]
