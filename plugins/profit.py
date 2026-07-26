"""
Себестоимость и реальная прибыль.

Выручка и прибыль на FunPay это разные числа, и разница больше, чем кажется:
с каждой продажи площадка берёт комиссию, а товар ты чаще всего купил.
Лот может быть первым по обороту и убыточным одновременно.

Плагин считает третье число - то, которое остаётся:

    прибыль = цена - комиссия площадки - себестоимость товара

Откуда данные:
    * продажи - из истории плагина «Статистика продаж» (storage/stats/sales.jsonl);
    * себестоимость и процент комиссии - из configs/costs.json, правится
      командами в Telegram.

Команды Telegram:
    /profit           - прибыль за 30 дней: итог, по лотам, худшие позиции
    /costs            - список заданной себестоимости и процент комиссии
    /setcost <сумма> <часть названия лота>  - задать себестоимость
    /setfee <процент>  - задать комиссию площадки

Себестоимость привязывается по подстроке названия лота, а не по точному
совпадению: названия лотов на FunPay содержат параметры заказа и меняются.
Если подходит несколько правил, берётся самое длинное совпадение -
оно конкретнее.
"""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging import getLogger
from typing import TYPE_CHECKING

from telebot.types import Message

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "Прибыль"
VERSION = "1.0.0"
DESCRIPTION = ("Считает реальную прибыль вместо оборота: вычитает комиссию площадки "
               "и себестоимость товара. Показывает, какой лот на самом деле "
               "зарабатывает, а какой продаётся в убыток.\n\n"
               "Требует включённого плагина «Статистика продаж» - берёт продажи из его истории.\n\n"
               "Команды: /profit, /costs, /setcost, /setfee")
CREDITS = "{{OWNER_TG}}"
UUID = "a4f9d2c7-6b13-4e85-9f0a-3d7c8e15b6f2"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

logger = getLogger(f"4FP.{__name__}")

COSTS_FILE = "configs/costs.json"
SALES_FILE = "storage/stats/sales.jsonl"
"""История продаж плагина «Статистика продаж». Читаем, не пишем."""

DEFAULT_FEE_PERCENT = 0.0
"""
Комиссия площадки по умолчанию.

Ноль, а не «примерно 10%»: подставленное наугад число выглядело бы как
настоящий расчёт. Пока не задашь /setfee, прибыль считается без комиссии
и в отчёте об этом написано.
"""

_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class Costs:
    """Настройки расчёта прибыли."""

    fee_percent: float = DEFAULT_FEE_PERCENT
    """Процент, который забирает площадка."""

    per_lot: dict[str, float] = None
    """Себестоимость по подстроке названия лота."""

    def __post_init__(self) -> None:
        if self.per_lot is None:
            object.__setattr__(self, "per_lot", {})

    def cost_for(self, lot_name: str) -> float | None:
        """
        Подбирает себестоимость для названия лота.

        При нескольких подходящих правилах берётся самое длинное совпадение:
        оно описывает лот конкретнее.

        :param lot_name: описание лота из заказа.

        :return: себестоимость или None, если правило не задано.
        """
        lowered = lot_name.lower()
        matches = [(pattern, cost) for pattern, cost in self.per_lot.items()
                   if pattern.lower() in lowered]
        if not matches:
            return None
        return max(matches, key=lambda item: len(item[0]))[1]


def load_costs() -> Costs:
    """
    Загружает настройки прибыли.

    :return: настройки; при отсутствии или порче файла - значения по умолчанию.
    """
    if not os.path.exists(COSTS_FILE):
        return Costs()
    try:
        with open(COSTS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return Costs(
            fee_percent=float(data.get("fee_percent", DEFAULT_FEE_PERCENT)),
            per_lot={str(k): float(v) for k, v in (data.get("per_lot") or {}).items()},
        )
    except (OSError, ValueError, TypeError, AttributeError):
        logger.debug("Файл себестоимости повреждён, использую значения по умолчанию", exc_info=True)
        return Costs()


def save_costs(costs: Costs) -> None:
    """
    Сохраняет настройки прибыли.

    :param costs: настройки.
    """
    try:
        os.makedirs(os.path.dirname(COSTS_FILE), exist_ok=True)
        with open(COSTS_FILE, "w", encoding="utf-8") as file:
            json.dump({"fee_percent": costs.fee_percent, "per_lot": costs.per_lot},
                      file, ensure_ascii=False, indent=2)
    except OSError:
        logger.debug("Не удалось сохранить файл себестоимости", exc_info=True)


@dataclass
class ProfitRow:
    """Результат расчёта по одному лоту."""

    lot: str
    count: int
    revenue: float
    fee: float
    cost: float
    currency: str
    cost_known: bool

    @property
    def profit(self) -> float:
        """Прибыль после комиссии и себестоимости."""
        return self.revenue - self.fee - self.cost

    @property
    def margin_percent(self) -> float:
        """Доля прибыли в выручке, процентов."""
        return (self.profit / self.revenue * 100) if self.revenue else 0.0


def read_sales(days: int) -> list[dict]:
    """
    Читает продажи за период из истории плагина статистики.

    :param days: за сколько последних дней.

    :return: список записей о продажах.
    """
    if not os.path.exists(SALES_FILE):
        return []

    since = datetime.now() - timedelta(days=days)
    sales = []
    with open(SALES_FILE, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                moment = datetime.fromisoformat(record["ts"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if moment >= since:
                sales.append(record)
    return sales


def calculate(sales: list[dict], costs: Costs) -> list[ProfitRow]:
    """
    Считает прибыль по каждому лоту.

    Валюты не смешиваются: строки группируются по паре «лот + валюта»,
    потому что курс на момент продажи неизвестен.

    :param sales: записи о продажах.
    :param costs: настройки себестоимости и комиссии.

    :return: строки расчёта, самые прибыльные первыми.
    """
    grouped: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "revenue": 0.0, "cost": 0.0, "known": True})

    for sale in sales:
        lot = str(sale.get("lot") or "без описания")
        currency = str(sale.get("currency") or "")
        try:
            price = float(sale.get("price") or 0)
            amount = int(sale.get("amount") or 1)
        except (TypeError, ValueError):
            continue

        bucket = grouped[(lot, currency)]
        bucket["count"] += 1
        bucket["revenue"] += price

        unit_cost = costs.cost_for(lot)
        if unit_cost is None:
            bucket["known"] = False
        else:
            bucket["cost"] += unit_cost * amount

    rows = []
    for (lot, currency), bucket in grouped.items():
        fee = bucket["revenue"] * costs.fee_percent / 100
        rows.append(ProfitRow(
            lot=lot,
            count=bucket["count"],
            revenue=round(bucket["revenue"], 2),
            fee=round(fee, 2),
            cost=round(bucket["cost"], 2),
            currency=currency,
            cost_known=bucket["known"],
        ))
    return sorted(rows, key=lambda row: -row.profit)


def format_report(rows: list[ProfitRow], costs: Costs, days: int) -> str:
    """
    Готовит отчёт по прибыли.

    :param rows: строки расчёта.
    :param costs: настройки.
    :param days: период отчёта в днях.

    :return: текст для Telegram.
    """
    if not rows:
        return ("💰 За период продаж не найдено.\n\n"
                "<i>Прибыль считается по истории плагина «Статистика продаж». "
                "Убедись, что он включён.</i>")

    lines = [f"💰 <b>Прибыль за {days} дн.</b>", ""]

    # Итоги по валютам: складывать разные валюты нельзя.
    totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"revenue": 0.0, "fee": 0.0, "cost": 0.0, "profit": 0.0})
    for row in rows:
        bucket = totals[row.currency or "?"]
        bucket["revenue"] += row.revenue
        bucket["fee"] += row.fee
        bucket["cost"] += row.cost
        bucket["profit"] += row.profit

    for currency, bucket in sorted(totals.items()):
        lines += [
            f"<b>{currency}</b>",
            f"  Оборот:       <code>{bucket['revenue']:g}</code>",
            f"  Комиссия:     <code>-{bucket['fee']:g}</code>",
            f"  Себестоимость:<code>-{bucket['cost']:g}</code>",
            f"  <b>Прибыль:      {bucket['profit']:g}</b>",
            "",
        ]

    unknown = [row for row in rows if not row.cost_known]
    if unknown:
        lines.append(f"⚠️ У {len(unknown)} лотов не задана себестоимость - "
                     f"их прибыль завышена. Задать: /setcost")
    if not costs.fee_percent:
        lines.append("⚠️ Комиссия площадки не задана (0%). Задать: /setfee")
    if unknown or not costs.fee_percent:
        lines.append("")

    lines.append("<b>По лотам</b>")
    for row in rows[:10]:
        name = row.lot if len(row.lot) <= 32 else f"{row.lot[:29]}..."
        mark = "" if row.cost_known else " ⚠️"
        lines.append(f"{row.profit:+g}{row.currency} ({row.margin_percent:.0f}%) "
                     f"×{row.count} - {name}{mark}")

    losing = [row for row in rows if row.profit < 0]
    if losing:
        lines += ["", f"🔻 <b>В убыток: {len(losing)}</b>"]
        for row in losing[:5]:
            name = row.lot if len(row.lot) <= 32 else f"{row.lot[:29]}..."
            lines.append(f"{row.profit:+g}{row.currency} - {name}")

    return "\n".join(lines)


def format_costs(costs: Costs) -> str:
    """
    Готовит список заданной себестоимости.

    :param costs: настройки.

    :return: текст для Telegram.
    """
    lines = ["📋 <b>Себестоимость и комиссия</b>", ""]
    fee = f"{costs.fee_percent:g}%" if costs.fee_percent else "не задана (0%)"
    lines += [f"Комиссия площадки: <b>{fee}</b>", ""]

    if not costs.per_lot:
        lines += ["Себестоимость не задана ни для одного лота.", "",
                  "Задать: <code>/setcost 150 Ключ Steam</code>",
                  "Правило сработает для любого лота, в названии которого",
                  "встречается «Ключ Steam»."]
        return "\n".join(lines)

    lines.append("<b>Правила</b> (от конкретных к общим):")
    for pattern, cost in sorted(costs.per_lot.items(), key=lambda item: -len(item[0])):
        lines.append(f"<code>{cost:g}</code> - {pattern}")
    lines += ["", "Удалить правило: <code>/setcost 0 &lt;часть названия&gt;</code>"]
    return "\n".join(lines)


def _register_telegram(cardinal: Cardinal) -> None:
    """Регистрирует команды расчёта прибыли."""
    telegram = cardinal.telegram
    if telegram is None:
        return

    def profit(message: Message) -> None:
        costs = load_costs()
        rows = calculate(read_sales(30), costs)
        telegram.bot.send_message(message.chat.id, format_report(rows, costs, 30))

    def show_costs(message: Message) -> None:
        telegram.bot.send_message(message.chat.id, format_costs(load_costs()))

    def set_cost(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3:
            telegram.bot.send_message(
                message.chat.id,
                "Формат: <code>/setcost &lt;сумма&gt; &lt;часть названия лота&gt;</code>\n\n"
                "Например: <code>/setcost 150 Ключ Steam</code>\n"
                "Сумма 0 удаляет правило.")
            return
        try:
            value = float(parts[1].replace(",", "."))
        except ValueError:
            telegram.bot.send_message(message.chat.id, "❌ Сумма должна быть числом.")
            return

        pattern = parts[2].strip()
        with _WRITE_LOCK:
            costs = load_costs()
            per_lot = dict(costs.per_lot)
            if value <= 0:
                removed = per_lot.pop(pattern, None)
                save_costs(Costs(costs.fee_percent, per_lot))
                telegram.bot.send_message(
                    message.chat.id,
                    f"🗑️ Правило «{pattern}» удалено." if removed is not None
                    else f"❌ Правила «{pattern}» не было.")
                return
            per_lot[pattern] = value
            save_costs(Costs(costs.fee_percent, per_lot))
        telegram.bot.send_message(
            message.chat.id,
            f"✅ Себестоимость <b>{value:g}</b> для лотов, содержащих «{pattern}».")

    def set_fee(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2:
            telegram.bot.send_message(
                message.chat.id,
                "Формат: <code>/setfee &lt;процент&gt;</code>\n\n"
                "Например: <code>/setfee 12</code>\n"
                "Точный процент смотри в своих завершённых заказах на FunPay: "
                "разница между ценой заказа и суммой, зачисленной на баланс.")
            return
        try:
            value = float(parts[1].replace(",", ".").rstrip("%"))
        except ValueError:
            telegram.bot.send_message(message.chat.id, "❌ Процент должен быть числом.")
            return
        if not 0 <= value < 100:
            telegram.bot.send_message(message.chat.id, "❌ Процент должен быть от 0 до 100.")
            return

        with _WRITE_LOCK:
            costs = load_costs()
            save_costs(Costs(value, dict(costs.per_lot)))
        telegram.bot.send_message(message.chat.id, f"✅ Комиссия площадки: <b>{value:g}%</b>")

    telegram.msg_handler(profit, commands=["profit"])
    telegram.msg_handler(show_costs, commands=["costs"])
    telegram.msg_handler(set_cost, commands=["setcost"])
    telegram.msg_handler(set_fee, commands=["setfee"])

    cardinal.add_telegram_commands(UUID, [
        ("profit", "реальная прибыль за месяц", True),
        ("costs", "себестоимость и комиссия", True),
        ("setcost", "задать себестоимость лота", False),
        ("setfee", "задать комиссию площадки", False),
    ])


def init(cardinal: Cardinal, *args) -> None:
    """Регистрирует команды Telegram."""
    _register_telegram(cardinal)
    costs = load_costs()
    logger.info(f"$MAGENTA[{NAME}]$RESET правил себестоимости: {len(costs.per_lot)}, "
                f"комиссия: {costs.fee_percent:g}%")


BIND_TO_PRE_INIT = [init]
