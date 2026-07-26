"""
Монитор цен конкурентов.

На FunPay покупатель почти всегда берёт из первых строк списка, а список
отсортирован по цене. Поэтому вопрос «на каком я месте» важнее вопроса
«сколько я поставил». Плагин отвечает на первый.

Что делает:
    * периодически смотрит подкатегории, в которых у тебя есть активные лоты;
    * считает твою позицию среди всех продавцов по цене;
    * присылает уведомление, когда тебя вытеснили из топа или подрезали цену;
    * показывает, у кого из тех, кто ниже, есть автовыдача и кто сейчас онлайн -
      это то, с чем реально конкурирует твой лот.

Команды Telegram:
    /prices - текущий срез по всем твоим подкатегориям

Плагин НИЧЕГО НЕ МЕНЯЕТ на сайте. Только чтение и уведомления: решение
о цене принимает человек. Автоматическая правка цен - это регулярные записи
на сайт, заметные со стороны FunPay, и такой риск берут осознанно, а не
получают побочным эффектом установки плагина.

Нагрузка на сайт: один GET-запрос на подкатегорию, между запросами пауза
:data:`REQUEST_DELAY`. При 20 подкатегориях один цикл это 20 запросов,
размазанных примерно на минуту, раз в :data:`DEFAULT_INTERVAL` секунд.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from logging import getLogger
from threading import Thread
from typing import TYPE_CHECKING

from telebot.types import Message

from FunPayAPI.common.enums import SubCategoryTypes

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.types import LotShortcut, SubCategory

NAME = "Монитор цен"
VERSION = "1.0.0"
DESCRIPTION = ("Следит за ценами конкурентов в твоих подкатегориях и присылает "
               "уведомление, когда тебя вытеснили из топа или подрезали цену. "
               "Показывает, у кого из тех, кто ниже, есть автовыдача и кто онлайн.\n\n"
               "Только чтение: цены на сайте плагин не меняет.\n\n"
               "Команда: /prices")
CREDITS = "{{OWNER_TG}}"
UUID = "e8c34b71-9d26-4a58-b3f7-5c1e9a0d842b"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

logger = getLogger(f"4FP.{__name__}")

STATE_FILE = "storage/cache/price_monitor.json"

DEFAULT_INTERVAL = 1800
"""Пауза между полными циклами проверки, секунды."""

REQUEST_DELAY = 3
"""
Пауза между запросами подкатегорий, секунды.

FunPay отдаёт 429 при частых запросах. Значение согласовано с requestsDelay
из основного конфига по смыслу: лучше проверять реже, чем получить блокировку.
"""

TOP_POSITIONS = 3
"""Со какого места считается, что лот «в топе»."""

STARTUP_DELAY = 120
"""
Задержка перед первым циклом, секунды.

Нужна, чтобы не мешать инициализации ядра: на старте оно и так делает
несколько запросов к сайту.
"""


@dataclass
class Competitor:
    """Лот конкурента, стоящий дешевле нашего."""

    username: str
    price: float
    auto: bool
    online: bool
    reviews: int
    amount: int | None

    def format(self) -> str:
        """
        Готовит строку для уведомления.

        :return: описание конкурента.
        """
        marks = []
        if self.auto:
            marks.append("авто")
        if self.online:
            marks.append("онлайн")
        if self.amount is not None:
            marks.append(f"{self.amount} шт")
        suffix = f" ({', '.join(marks)})" if marks else ""
        return f"{self.price:g} - {self.username}{suffix}"


@dataclass
class LotPosition:
    """Положение нашего лота в подкатегории."""

    subcategory_name: str
    lot_description: str
    our_price: float
    currency: str
    rank: int
    """Место по цене, начиная с 1."""

    total: int
    """Сколько всего лотов в подкатегории."""

    cheaper: list[Competitor] = field(default_factory=list)
    """Конкуренты дешевле нас, от самого дешёвого."""

    @property
    def in_top(self) -> bool:
        """Находится ли лот в первых :data:`TOP_POSITIONS` позициях."""
        return self.rank <= TOP_POSITIONS

    @property
    def best_competitor_price(self) -> float | None:
        """Цена самого дешёвого конкурента или None, если мы первые."""
        return self.cheaper[0].price if self.cheaper else None


def analyze_subcategory(lots: list[LotShortcut], our_id: int,
                        subcategory_name: str) -> list[LotPosition]:
    """
    Считает позиции наших лотов среди всех лотов подкатегории.

    Чистая функция: разбор и расчёт без сети, чтобы это можно было проверить
    тестами на выдуманных лотах.

    :param lots: все опубликованные лоты подкатегории.
    :param our_id: ID нашего аккаунта.
    :param subcategory_name: название подкатегории для текста уведомлений.

    :return: список позиций наших лотов.
    """
    # Промо-лоты стоят выше выдачи независимо от цены, в честном сравнении
    # цен они участвовать не должны.
    ranked = sorted((lot for lot in lots if not lot.promo), key=lambda lot: lot.price)

    positions: list[LotPosition] = []
    for index, lot in enumerate(ranked):
        if lot.seller is None or lot.seller.id != our_id:
            continue

        cheaper = [
            Competitor(
                username=other.seller.username if other.seller else "?",
                price=other.price,
                auto=bool(other.auto),
                online=bool(other.seller.online) if other.seller else False,
                reviews=other.seller.reviews if other.seller else 0,
                amount=other.amount,
            )
            for other in ranked[:index]
            if other.seller is None or other.seller.id != our_id
        ]

        positions.append(LotPosition(
            subcategory_name=subcategory_name,
            lot_description=lot.description or "без описания",
            our_price=lot.price,
            currency=str(lot.currency) if lot.currency else "",
            rank=index + 1,
            total=len(ranked),
            cheaper=cheaper,
        ))
    return positions


def load_state() -> dict[str, dict]:
    """
    Загружает прошлый срез позиций.

    :return: словарь ``{ключ лота: {"rank": int, "best": float | None}}``.
    """
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, dict]) -> None:
    """
    Сохраняет срез позиций.

    :param state: словарь среза.
    """
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False)
    except OSError:
        logger.debug("Не удалось сохранить срез монитора цен", exc_info=True)


def position_key(position: LotPosition) -> str:
    """
    Строит стабильный ключ лота для сравнения срезов.

    :param position: позиция лота.

    :return: ключ.
    """
    return f"{position.subcategory_name}|{position.lot_description}"


def detect_changes(positions: list[LotPosition],
                   previous: dict[str, dict]) -> list[tuple[LotPosition, str]]:
    """
    Ищет изменения, о которых стоит сообщить.

    Сообщаем только о том, что требует решения:
    вылет из топа и появление нового более дешёвого конкурента. Обычные
    колебания глубоко внизу списка владельца не касаются.

    :param positions: текущие позиции.
    :param previous: прошлый срез.

    :return: список пар «позиция, причина».
    """
    changes = []
    for position in positions:
        key = position_key(position)
        old = previous.get(key)
        if old is None:
            # Первый проход: запоминаем, но не спамим уведомлениями.
            continue

        old_rank = old.get("rank")
        old_best = old.get("best")

        if old_rank is not None and position.rank > old_rank and not position.in_top:
            changes.append((position, "dropped"))
            continue

        best = position.best_competitor_price
        if best is not None and (old_best is None or best < old_best):
            changes.append((position, "undercut"))
    return changes


def format_alert(position: LotPosition, reason: str) -> str:
    """
    Готовит текст уведомления.

    :param position: позиция лота.
    :param reason: ``dropped`` либо ``undercut``.

    :return: текст для Telegram.
    """
    header = "📉 <b>Лот вытеснен из топа</b>" if reason == "dropped" else "⚠️ <b>Тебя подрезали по цене</b>"
    lines = [
        header,
        "",
        f"🎮 {position.subcategory_name}",
        f"📦 {position.lot_description}",
        f"💰 Твоя цена: <b>{position.our_price:g} {position.currency}</b>",
        f"📊 Место: <b>{position.rank}</b> из {position.total}",
    ]
    if position.cheaper:
        lines += ["", "<b>Дешевле тебя:</b>"]
        lines += [f"• {competitor.format()}" for competitor in position.cheaper[:5]]
        if len(position.cheaper) > 5:
            lines.append(f"• ...и ещё {len(position.cheaper) - 5}")
    return "\n".join(lines)


def format_snapshot(positions: list[LotPosition]) -> str:
    """
    Готовит текст полного среза для команды /prices.

    :param positions: текущие позиции.

    :return: текст для Telegram.
    """
    if not positions:
        return ("📊 Активных лотов не найдено.\n\n"
                "<i>Монитор смотрит только те подкатегории, где у тебя есть лоты "
                "в продаже.</i>")

    in_top = sum(1 for position in positions if position.in_top)
    lines = [
        "📊 <b>Позиции по цене</b>",
        "",
        f"Лотов отслеживается: <b>{len(positions)}</b>",
        f"В топ-{TOP_POSITIONS}: <b>{in_top}</b> из {len(positions)}",
        "",
    ]

    # Сначала самые проблемные - те, кто ниже всех.
    for position in sorted(positions, key=lambda p: -p.rank)[:15]:
        mark = "🟢" if position.in_top else "🔴"
        description = position.lot_description
        if len(description) > 38:
            description = f"{description[:35]}..."
        lines.append(f"{mark} <b>{position.rank}</b>/{position.total} "
                     f"{position.our_price:g}{position.currency} - {description}")

    if len(positions) > 15:
        lines.append(f"\n<i>...и ещё {len(positions) - 15} лотов</i>")
    return "\n".join(lines)


def collect_positions(cardinal: Cardinal) -> list[LotPosition]:
    """
    Обходит подкатегории с нашими лотами и считает позиции.

    :param cardinal: экземпляр ядра.

    :return: позиции всех наших лотов.
    """
    profile = cardinal.profile
    if profile is None:
        return []

    positions: list[LotPosition] = []
    subcategories = profile.get_sorted_lots(2)

    for subcategory in subcategories:
        # Лоты игровой валюты живут в другом разделе и сравниваются иначе.
        if subcategory.type is SubCategoryTypes.CURRENCY:
            continue
        try:
            public_lots = cardinal.account.get_subcategory_public_lots(
                subcategory.type, subcategory.id)
        except Exception:
            logger.debug(f"Не удалось получить лоты подкатегории {subcategory.id}", exc_info=True)
            continue

        positions.extend(analyze_subcategory(public_lots, cardinal.account.id,
                                             subcategory.fullname))
        time.sleep(REQUEST_DELAY)

    return positions


def _monitor_loop(cardinal: Cardinal) -> None:
    """
    Бесконечный цикл проверки цен.

    :param cardinal: экземпляр ядра.
    """
    time.sleep(STARTUP_DELAY)
    while True:
        try:
            positions = collect_positions(cardinal)
            previous = load_state()
            changes = detect_changes(positions, previous)

            save_state({position_key(p): {"rank": p.rank, "best": p.best_competitor_price}
                        for p in positions})

            if changes and cardinal.telegram:
                from tg_bot import utils as tg_utils
                for position, reason in changes[:5]:
                    cardinal.telegram.send_notification(
                        format_alert(position, reason),
                        notification_type=tg_utils.NotificationTypes.other)
                    time.sleep(1)
                if len(changes) > 5:
                    cardinal.telegram.send_notification(
                        f"📉 Ещё {len(changes) - 5} лотов сдвинулись по позиции. "
                        f"Полный срез: /prices",
                        notification_type=tg_utils.NotificationTypes.other)

            logger.info(f"$MAGENTA[{NAME}]$RESET проверено лотов: {len(positions)}, "
                        f"изменений: {len(changes)}")
        except Exception:
            logger.debug("Ошибка в цикле монитора цен", exc_info=True)
        time.sleep(DEFAULT_INTERVAL)


def _register_telegram(cardinal: Cardinal) -> None:
    """Регистрирует команду /prices."""
    telegram = cardinal.telegram
    if telegram is None:
        return

    def prices(message: Message) -> None:
        telegram.bot.send_message(message.chat.id, "📊 Собираю срез цен, это займёт до минуты...")

        def worker() -> None:
            try:
                positions = collect_positions(cardinal)
                telegram.bot.send_message(message.chat.id, format_snapshot(positions))
            except Exception:
                logger.debug("Не удалось собрать срез цен", exc_info=True)
                telegram.bot.send_message(message.chat.id, "❌ Не удалось получить данные с FunPay.")

        Thread(target=worker, daemon=True).start()

    telegram.msg_handler(prices, commands=["prices"])
    cardinal.add_telegram_commands(UUID, [("prices", "позиции по цене", True)])


def init(cardinal: Cardinal, *args) -> None:
    """Регистрирует команду Telegram."""
    _register_telegram(cardinal)
    logger.info(f"$MAGENTA[{NAME}]$RESET цикл проверки каждые {DEFAULT_INTERVAL // 60} мин")


def start(cardinal: Cardinal, *args) -> None:
    """
    Запускает цикл мониторинга.

    Привязано к POST_INIT, а не к PRE_INIT: цикл обращается к
    ``cardinal.profile``, а он заполняется в конце инициализации ядра.
    """
    Thread(target=_monitor_loop, args=(cardinal,), daemon=True).start()


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_INIT = [start]
