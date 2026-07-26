"""
Статистика продаж.

Ведёт собственную историю продаж и считает по ней сводки. Своя история нужна
потому, что FunPay отдаёт только текущее состояние заказов: команда /profile
покажет активные продажи, но не ответит, сколько было продано в прошлый вторник
и какой лот приносит больше всех.

Записи копятся с момента установки плагина - за прошлое данных не появится.

Куда пишет:
    storage/stats/sales.jsonl

Команды Telegram:
    /stats          - сводка за 7 дней с графиком по дням
    /stats_month    - сводка за 30 дней с графиком по неделям
    /stats_export   - выгрузить всю историю в CSV (открывается в Excel)

Учитываются заказы, дошедшие до оплаты. Заказ фиксируется один раз: повторные
события по тому же ID игнорируются, иначе смена статуса удваивала бы суммы.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from logging import getLogger
from typing import TYPE_CHECKING

from telebot.types import InputFile, Message

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.updater.events import NewOrderEvent, OrderStatusChangedEvent

NAME = "Статистика продаж"
VERSION = "1.0.0"
DESCRIPTION = ("Собирает историю продаж и показывает сводки за неделю и месяц "
               "с графиками, топом лотов и постоянными покупателями. "
               "Экспорт всей истории в CSV для Excel.\n\n"
               "Команды: /stats, /stats_month, /stats_export")
CREDITS = "{{OWNER_TG}}"
UUID = "d5a2b8e6-4c71-4e93-8f20-6b9c3a1d7f58"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

logger = getLogger(f"4FP.{__name__}")

STATS_DIR = "storage/stats"
SALES_FILE = os.path.join(STATS_DIR, "sales.jsonl")
SEEN_FILE = os.path.join(STATS_DIR, "counted_orders.json")

CHART_WIDTH = 14
"""Максимальная длина полосы графика в символах."""

MAX_SEEN_ORDERS = 5000
"""Сколько ID учтённых заказов держать, чтобы файл не рос бесконечно."""

EXPORT_FILE = os.path.join(STATS_DIR, "sales-export.csv")
"""Куда пишется CSV перед отправкой в Telegram."""

_WRITE_LOCK = threading.Lock()
"""
Защищает связку «прочитать список учтённых - дописать продажу - сохранить список».

События заказов обрабатываются в одном потоке, но ORDER_STATUS_CHANGED может
прийти из потока Telegram-ПУ при ручном обновлении профиля, а плагины ядра
вправе вызывать record_sale откуда угодно.
"""


@dataclass(frozen=True)
class Sale:
    """Одна продажа."""

    timestamp: datetime
    order_id: str
    buyer: str
    lot: str
    amount: int
    price: float
    currency: str

    @property
    def total(self) -> float:
        """Сумма заказа. FunPay отдаёт price уже за весь заказ, а не за единицу."""
        return self.price


def _load_seen() -> list[str]:
    """
    Загружает ID уже учтённых заказов.

    Возвращается именно список, а не множество: порядок нужен, чтобы при
    обрезке истории отбрасывались самые старые ID, а не произвольные.

    :return: список ID, старые в начале.
    """
    if not os.path.exists(SEEN_FILE):
        return []
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return [str(item) for item in data] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_seen(seen: list[str]) -> None:
    """
    Сохраняет ID учтённых заказов, оставляя последние :data:`MAX_SEEN_ORDERS`.

    :param seen: список ID, старые в начале.
    """
    try:
        os.makedirs(STATS_DIR, exist_ok=True)
        with open(SEEN_FILE, "w", encoding="utf-8") as file:
            json.dump(seen[-MAX_SEEN_ORDERS:], file)
    except OSError:
        logger.debug("Не удалось сохранить список учтённых заказов", exc_info=True)


def record_sale(order_id: str, buyer: str, lot: str, amount: int,
                price: float, currency: str) -> bool:
    """
    Добавляет продажу в историю, если такой заказ ещё не учтён.

    Защита от повторов обязательна: по одному заказу приходит и NEW_ORDER,
    и ORDER_STATUS_CHANGED, а без проверки выручка удваивалась бы.

    :param order_id: ID заказа.
    :param buyer: ник покупателя.
    :param lot: описание лота.
    :param amount: количество.
    :param price: сумма заказа.
    :param currency: валюта.

    :return: True, если запись добавлена; False, если заказ уже был учтён.
    """
    with _WRITE_LOCK:
        seen = _load_seen()
        if order_id in seen:
            return False

        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "order_id": order_id,
            "buyer": buyer,
            "lot": lot,
            "amount": amount,
            "price": price,
            "currency": currency,
        }
        try:
            os.makedirs(STATS_DIR, exist_ok=True)
            with open(SALES_FILE, "a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.debug("Не удалось записать продажу", exc_info=True)
            return False

        seen.append(order_id)
        _save_seen(seen)
        return True


def load_sales(since: datetime | None = None) -> list[Sale]:
    """
    Читает историю продаж.

    :param since: если задано, вернуть только продажи не раньше этого момента.

    :return: список продаж, старые в начале.
    """
    if not os.path.exists(SALES_FILE):
        return []

    sales: list[Sale] = []
    with open(SALES_FILE, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                moment = datetime.fromisoformat(data["ts"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if since and moment < since:
                continue
            sales.append(Sale(
                timestamp=moment,
                order_id=str(data.get("order_id", "")),
                buyer=str(data.get("buyer", "")),
                lot=str(data.get("lot", "")),
                amount=int(data.get("amount") or 1),
                price=float(data.get("price") or 0),
                currency=str(data.get("currency", "")),
            ))
    return sales


def _bar(value: float, peak: float, width: int = CHART_WIDTH) -> str:
    """
    Рисует полосу графика.

    :param value: значение.
    :param peak: максимальное значение в наборе.
    :param width: максимальная длина полосы.

    :return: строка из блочных символов.
    """
    if peak <= 0:
        return ""
    filled = int(round(value / peak * width))
    # Ненулевое значение должно быть видно хотя бы одним символом.
    if value > 0 and filled == 0:
        filled = 1
    return "█" * filled


def _money(sales: list[Sale]) -> str:
    """
    Суммирует выручку по валютам.

    Валюты не складываются между собой: курс на момент продажи неизвестен,
    а пересчёт по текущему давал бы неверную историческую картину.

    :param sales: список продаж.

    :return: строка вида ``1234.5 RUB, 12 USD``.
    """
    totals: dict[str, float] = defaultdict(float)
    for sale in sales:
        totals[sale.currency or "?"] += sale.total
    if not totals:
        return "0"
    return ", ".join(f"{round(total, 2):g} {currency}"
                     for currency, total in sorted(totals.items()))


def build_daily_report(days: int = 7) -> str:
    """
    Собирает сводку по дням.

    :param days: за сколько последних дней.

    :return: готовый HTML-текст для Telegram.
    """
    since = datetime.now() - timedelta(days=days)
    sales = load_sales(since)
    if not sales:
        return (f"📊 За последние {days} дн. продаж не зафиксировано.\n\n"
                f"<i>История ведётся с момента установки плагина.</i>")

    by_day: dict[date, list[Sale]] = defaultdict(list)
    for sale in sales:
        by_day[sale.timestamp.date()].append(sale)

    today = date.today()
    day_range = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    peak = max((len(by_day.get(day, [])) for day in day_range), default=0)

    lines = [f"📊 <b>Продажи за {days} дн.</b>", ""]
    for day in day_range:
        day_sales = by_day.get(day, [])
        count = len(day_sales)
        bar = _bar(count, peak)
        lines.append(f"<code>{day:%d.%m}</code> {bar} <b>{count}</b>")

    lines += [
        "",
        f"🧾 Всего заказов: <b>{len(sales)}</b>",
        f"💰 Выручка: <b>{_money(sales)}</b>",
        f"👥 Уникальных покупателей: <b>{len({s.buyer for s in sales})}</b>",
    ]

    lines += _top_lots_section(sales)
    lines += _repeat_buyers_section(sales)
    return "\n".join(lines)


def build_weekly_report(days: int = 30) -> str:
    """
    Собирает сводку по неделям.

    :param days: за сколько последних дней.

    :return: готовый HTML-текст для Telegram.
    """
    since = datetime.now() - timedelta(days=days)
    sales = load_sales(since)
    if not sales:
        return (f"📊 За последние {days} дн. продаж не зафиксировано.\n\n"
                f"<i>История ведётся с момента установки плагина.</i>")

    by_week: dict[tuple[int, int], list[Sale]] = defaultdict(list)
    for sale in sales:
        year, week, _ = sale.timestamp.isocalendar()
        by_week[(year, week)].append(sale)

    ordered = sorted(by_week.items())
    peak = max((len(items) for _, items in ordered), default=0)

    lines = [f"📊 <b>Продажи за {days} дн. по неделям</b>", ""]
    for (year, week), items in ordered:
        monday = date.fromisocalendar(year, week, 1)
        sunday = date.fromisocalendar(year, week, 7)
        bar = _bar(len(items), peak)
        lines.append(f"<code>{monday:%d.%m}-{sunday:%d.%m}</code> {bar} <b>{len(items)}</b>")

    average = len(sales) / max(len(ordered), 1)
    lines += [
        "",
        f"🧾 Всего заказов: <b>{len(sales)}</b>",
        f"💰 Выручка: <b>{_money(sales)}</b>",
        f"📈 В среднем за неделю: <b>{average:.1f}</b>",
        f"👥 Уникальных покупателей: <b>{len({s.buyer for s in sales})}</b>",
    ]

    lines += _top_lots_section(sales)
    return "\n".join(lines)


def _top_lots_section(sales: list[Sale], limit: int = 5) -> list[str]:
    """
    Формирует блок «топ лотов».

    :param sales: список продаж.
    :param limit: сколько позиций показать.

    :return: строки блока.
    """
    counts: dict[str, int] = defaultdict(int)
    for sale in sales:
        counts[sale.lot or "без описания"] += 1
    if not counts:
        return []

    top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
    lines = ["", "🏆 <b>Топ лотов</b>"]
    for position, (lot, count) in enumerate(top, 1):
        title = lot if len(lot) <= 45 else f"{lot[:42]}..."
        lines.append(f"{position}. {title} - <b>{count}</b>")
    return lines


def _repeat_buyers_section(sales: list[Sale], limit: int = 3) -> list[str]:
    """
    Формирует блок «постоянные покупатели».

    :param sales: список продаж.
    :param limit: сколько позиций показать.

    :return: строки блока или пустой список, если повторных покупок не было.
    """
    counts: dict[str, int] = defaultdict(int)
    for sale in sales:
        counts[sale.buyer] += 1
    repeat = [(buyer, count) for buyer, count in counts.items() if count > 1]
    if not repeat:
        return []

    repeat.sort(key=lambda item: item[1], reverse=True)
    lines = ["", "🔁 <b>Вернулись за покупкой</b>"]
    for buyer, count in repeat[:limit]:
        lines.append(f"• {buyer} - <b>{count}</b>")
    return lines


def build_csv() -> bytes:
    """
    Собирает всю историю продаж в CSV.

    Разделитель - точка с запятой, кодировка - UTF-8 с BOM: в таком виде
    Excel на русской локали открывает файл сразу, без диалога импорта.

    :return: содержимое файла.
    """
    sales = load_sales()
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(["Дата", "Время", "ID заказа", "Покупатель", "Лот",
                     "Количество", "Сумма", "Валюта"])
    for sale in sales:
        writer.writerow([
            f"{sale.timestamp:%d.%m.%Y}",
            f"{sale.timestamp:%H:%M:%S}",
            sale.order_id,
            sale.buyer,
            sale.lot,
            sale.amount,
            f"{sale.total:g}".replace(".", ","),
            sale.currency,
        ])
    return buffer.getvalue().encode("utf-8-sig")


# --- Обработчики событий ---------------------------------------------------

def on_new_order(cardinal: Cardinal, event: NewOrderEvent, *args) -> None:
    """Фиксирует новый заказ."""
    order = event.order
    if record_sale(order_id=order.id,
                   buyer=order.buyer_username,
                   lot=order.description,
                   amount=order.amount or 1,
                   price=order.price or 0,
                   currency=str(order.currency) if order.currency else ""):
        logger.debug(f"Продажа {order.id} записана в статистику")


def on_order_status_changed(cardinal: Cardinal, event: OrderStatusChangedEvent, *args) -> None:
    """
    Дозаписывает заказ, если событие о его создании было пропущено.

    Бывает при перезапуске бота в момент оформления заказа: NEW_ORDER не пришёл,
    а смена статуса пришла. Повторный вызов безопасен - record_sale проверяет ID.
    """
    order = event.order
    record_sale(order_id=order.id,
                buyer=order.buyer_username,
                lot=order.description,
                amount=order.amount or 1,
                price=order.price or 0,
                currency=str(order.currency) if order.currency else "")


# --- Telegram --------------------------------------------------------------

def _register_telegram(cardinal: Cardinal) -> None:
    """Регистрирует команды статистики."""
    telegram = cardinal.telegram
    if telegram is None:
        return

    def weekly(message: Message) -> None:
        telegram.bot.send_message(message.chat.id, build_daily_report(7))

    def monthly(message: Message) -> None:
        telegram.bot.send_message(message.chat.id, build_weekly_report(30))

    def export(message: Message) -> None:
        sales = load_sales()
        if not sales:
            telegram.bot.send_message(message.chat.id, "📊 История продаж пуста.")
            return

        # Пишем во временный файл на диске, а не в BytesIO: имя файла берётся
        # из самого файла, и не приходится опираться на внутреннее устройство
        # InputFile конкретной версии библиотеки.
        export_path = os.path.join(STATS_DIR, f"sales-{datetime.now():%Y-%m-%d}.csv")
        try:
            with open(export_path, "wb") as file:
                file.write(build_csv())
            with open(export_path, "rb") as file:
                telegram.bot.send_document(
                    message.chat.id, InputFile(file),
                    caption=f"📊 История продаж: <b>{len(sales)}</b> записей\n\n"
                            f"Разделитель - точка с запятой, кодировка UTF-8 с BOM: "
                            f"Excel откроет как есть.")
        finally:
            # Выгрузка одноразовая, на диске её держать незачем.
            try:
                os.remove(export_path)
            except OSError:
                pass

    telegram.msg_handler(weekly, commands=["stats"])
    telegram.msg_handler(monthly, commands=["stats_month"])
    telegram.msg_handler(export, commands=["stats_export"])

    cardinal.add_telegram_commands(UUID, [
        ("stats", "статистика продаж за неделю", True),
        ("stats_month", "статистика продаж за месяц", True),
        ("stats_export", "выгрузить продажи в CSV", True),
    ])


def init(cardinal: Cardinal, *args) -> None:
    """Готовит каталог и регистрирует команды Telegram."""
    os.makedirs(STATS_DIR, exist_ok=True)
    _register_telegram(cardinal)
    logger.info(f"$MAGENTA[{NAME}]$RESET записей в истории: {len(load_sales())}")


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_ORDER_STATUS_CHANGED = [on_order_status_changed]
