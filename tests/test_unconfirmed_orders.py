"""
Тесты поиска неподтверждённых заказов.

Главное, что проверяется, — формат текста заявки: он вставляется в форму
поддержки FunPay, где ошибка в списке приводит к отклонению заявки целиком.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("telebot", reason="нужен pytelegrambotapi из requirements.txt")

from FunPayAPI.common.enums import Currency, OrderStatuses

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "unconfirmed_orders.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("plugins.unconfirmed_orders", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["plugins.unconfirmed_orders"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def uo(workdir: Path):
    return _load_plugin()


class FakeOrder:
    """Двойник OrderShortcut: нужны только поля, которые читает плагин."""

    def __init__(self, order_id: str, hours_ago: float, buyer: str = "buyer",
                 status: OrderStatuses = OrderStatuses.PAID,
                 price: float = 100.0, description: str = "Ключ Steam",
                 now: datetime | None = None) -> None:
        self.id = order_id
        self.date = (now or datetime.now()) - timedelta(hours=hours_ago)
        self.buyer_username = buyer
        self.status = status
        self.price = price
        self.currency = Currency.RUB
        self.description = description


# --- Отбор по возрасту -----------------------------------------------------

def test_orders_older_than_threshold_are_selected(uo) -> None:
    now = datetime(2026, 7, 26, 12, 0)
    orders = [FakeOrder("A", 30, now=now), FakeOrder("B", 2, now=now)]

    stale = uo.filter_stale(orders, 24, now=now)
    assert [order.id for order in stale] == ["A"]


def test_order_exactly_at_threshold_is_selected(uo) -> None:
    now = datetime(2026, 7, 26, 12, 0)
    stale = uo.filter_stale([FakeOrder("A", 24, now=now)], 24, now=now)
    assert len(stale) == 1


def test_fresh_orders_are_ignored(uo) -> None:
    now = datetime(2026, 7, 26, 12, 0)
    orders = [FakeOrder("A", 1, now=now), FakeOrder("B", 23.9, now=now)]
    assert uo.filter_stale(orders, 24, now=now) == []


def test_stale_orders_sorted_oldest_first(uo) -> None:
    """Самые старые сверху: по ним деньги висят дольше всех."""
    now = datetime(2026, 7, 26, 12, 0)
    orders = [FakeOrder("NEW", 25, now=now), FakeOrder("OLD", 200, now=now),
              FakeOrder("MID", 50, now=now)]

    stale = uo.filter_stale(orders, 24, now=now)
    assert [order.id for order in stale] == ["OLD", "MID", "NEW"]


def test_custom_threshold_is_respected(uo) -> None:
    now = datetime(2026, 7, 26, 12, 0)
    orders = [FakeOrder("A", 50, now=now), FakeOrder("B", 30, now=now)]

    assert len(uo.filter_stale(orders, 48, now=now)) == 1
    assert len(uo.filter_stale(orders, 24, now=now)) == 2


def test_empty_input_gives_empty_result(uo) -> None:
    assert uo.filter_stale([], 24) == []


# --- Текст заявки ----------------------------------------------------------

def test_ticket_text_matches_expected_format(uo) -> None:
    """
    Текст вставляется в форму поддержки, поэтому формат зафиксирован:
    заголовок, пустая строка, нумерация вида «N - #ID».
    """
    now = datetime(2026, 7, 26, 12, 0)
    orders = [FakeOrder("ABC123", 30, now=now),
              FakeOrder("DEF456", 40, now=now),
              FakeOrder("GHI789", 50, now=now)]

    assert uo.build_ticket_text(orders) == (
        "Здравствуйте, пожалуйста подтвердите данные заказы:\n"
        "\n"
        "1 - #ABC123\n"
        "2 - #DEF456\n"
        "3 - #GHI789"
    )


def test_ticket_numbering_starts_at_one(uo) -> None:
    text = uo.build_ticket_text([FakeOrder("ONLY", 30)])
    assert "1 - #ONLY" in text
    assert "0 - " not in text


def test_ticket_ids_carry_hash_prefix(uo) -> None:
    """В форме заказы указываются с решёткой."""
    text = uo.build_ticket_text([FakeOrder("XYZ", 30)])
    assert "#XYZ" in text


def test_empty_ticket_has_header_only(uo) -> None:
    assert uo.build_ticket_text([]).strip() == uo.TICKET_HEADER


# --- Отчёт в Telegram ------------------------------------------------------

def test_report_contains_ticket_and_orders(uo) -> None:
    now = datetime(2026, 7, 26, 12, 0)
    orders = [FakeOrder("ABC123", 30, buyer="vasya", now=now)]

    report, ticket = uo.build_report(orders, now=now)
    assert "ABC123" in report
    assert "vasya" in report
    assert ticket in report


def test_report_warns_about_manual_review(uo) -> None:
    """
    Предупреждение обязано быть: FunPay отклоняет заявку целиком,
    если в первом списке окажется спорный заказ.
    """
    report, _ = uo.build_report([FakeOrder("A", 30)])
    assert "сверь чаты" in report.lower() or "сверь" in report.lower()
    assert "второй" in report.lower()


def test_report_shows_order_count(uo) -> None:
    report, _ = uo.build_report([FakeOrder("A", 30), FakeOrder("B", 40)])
    assert "2" in report


def test_age_is_rendered_in_days_and_hours(uo) -> None:
    now = datetime(2026, 7, 26, 12, 0)
    assert uo._age_text(FakeOrder("A", 50, now=now), now=now) == "2д 2ч"
    assert uo._age_text(FakeOrder("B", 5, now=now), now=now) == "5ч"


# --- Настройки -------------------------------------------------------------

def test_default_settings(uo) -> None:
    settings = uo.load_settings()
    assert settings.min_age_hours == 24
    assert settings.interval_hours == 24


def test_settings_roundtrip(uo) -> None:
    uo.save_settings(uo.Settings(min_age_hours=48, interval_hours=12, last_check=123.0))
    loaded = uo.load_settings()
    assert loaded.min_age_hours == 48
    assert loaded.interval_hours == 12
    assert loaded.last_check == 123.0


def test_corrupted_settings_fall_back_to_defaults(uo, workdir: Path) -> None:
    path = workdir / uo.SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("не json", encoding="utf-8")
    assert uo.load_settings().min_age_hours == 24


def test_interval_zero_disables_autocheck(uo) -> None:
    uo.save_settings(uo.Settings(interval_hours=0))
    assert uo.load_settings().interval_hours == 0


# --- Сбор заказов ----------------------------------------------------------

class FakeAccountWithSales:
    """Двойник аккаунта: отдаёт заранее заданные страницы продаж."""

    def __init__(self, pages: list[tuple[str | None, list]]) -> None:
        self.pages = pages
        self.calls = 0

    def get_sales(self, start_from=None, **kwargs):
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return page[0], page[1], "ru", {}


class FakeCardinalWithSales:
    def __init__(self, account) -> None:
        self.account = account
        self.telegram = None


def test_only_paid_orders_are_collected(uo, monkeypatch) -> None:
    monkeypatch.setattr(uo.time, "sleep", lambda _s: None)
    orders = [FakeOrder("PAID1", 30),
              FakeOrder("CLOSED", 30, status=OrderStatuses.CLOSED),
              FakeOrder("PAID2", 30)]
    cardinal = FakeCardinalWithSales(FakeAccountWithSales([(None, orders)]))

    collected = uo.fetch_paid_orders(cardinal)
    assert [order.id for order in collected] == ["PAID1", "PAID2"]


def test_pagination_is_followed(uo, monkeypatch) -> None:
    monkeypatch.setattr(uo.time, "sleep", lambda _s: None)
    pages = [("next1", [FakeOrder("A", 30)]),
             (None, [FakeOrder("B", 30)])]
    cardinal = FakeCardinalWithSales(FakeAccountWithSales(pages))

    collected = uo.fetch_paid_orders(cardinal)
    assert [order.id for order in collected] == ["A", "B"]


def test_duplicate_orders_across_pages_counted_once(uo, monkeypatch) -> None:
    monkeypatch.setattr(uo.time, "sleep", lambda _s: None)
    duplicate = FakeOrder("SAME", 30)
    pages = [("next1", [duplicate]), (None, [duplicate])]
    cardinal = FakeCardinalWithSales(FakeAccountWithSales(pages))

    assert len(uo.fetch_paid_orders(cardinal)) == 1


def test_page_limit_is_respected(uo, monkeypatch) -> None:
    """Бесконечная история продаж не должна приводить к бесконечному обходу."""
    monkeypatch.setattr(uo.time, "sleep", lambda _s: None)
    account = FakeAccountWithSales([("always-next", [FakeOrder("A", 30)])])
    cardinal = FakeCardinalWithSales(account)

    uo.fetch_paid_orders(cardinal, max_pages=3)
    assert account.calls == 3


def test_network_error_does_not_raise(uo, monkeypatch) -> None:
    """Сбой запроса не должен ронять цикл автопроверки."""
    monkeypatch.setattr(uo.time, "sleep", lambda _s: None)

    class FailingAccount:
        def get_sales(self, **kwargs):
            raise ConnectionError("сеть недоступна")

    cardinal = FakeCardinalWithSales(FailingAccount())
    assert uo.fetch_paid_orders(cardinal) == []
