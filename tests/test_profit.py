"""
Тесты расчёта прибыли.

Проверяется арифметика и подбор себестоимости по названию лота -
то, из-за чего отчёт может незаметно показать неправильные деньги.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("telebot", reason="нужен pytelegrambotapi из requirements.txt")

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "profit.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("plugins.profit", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["plugins.profit"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pf(workdir: Path):
    return _load_plugin()


def sale(lot: str, price: float, amount: int = 1, currency: str = "RUB",
         days_ago: int = 0) -> dict:
    return {
        "ts": (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds"),
        "order_id": f"O-{lot}-{price}",
        "buyer": "buyer",
        "lot": lot,
        "amount": amount,
        "price": price,
        "currency": currency,
    }


# --- Подбор себестоимости --------------------------------------------------

def test_cost_matched_by_substring(pf) -> None:
    costs = pf.Costs(per_lot={"Ключ Steam": 100.0})
    assert costs.cost_for("Ключ Steam, Россия") == 100.0


def test_cost_match_is_case_insensitive(pf) -> None:
    costs = pf.Costs(per_lot={"ключ steam": 100.0})
    assert costs.cost_for("КЛЮЧ STEAM региональный") == 100.0


def test_longest_rule_wins(pf) -> None:
    """
    Более длинное правило конкретнее. Иначе общее правило перебивало бы
    точное и себестоимость считалась бы неверно.
    """
    costs = pf.Costs(per_lot={"Ключ": 50.0, "Ключ Steam Deluxe": 300.0})
    assert costs.cost_for("Ключ Steam Deluxe Edition") == 300.0
    assert costs.cost_for("Ключ Origin") == 50.0


def test_no_rule_gives_none(pf) -> None:
    assert pf.Costs(per_lot={"Ключ": 50.0}).cost_for("Аккаунт Netflix") is None


def test_empty_costs_object(pf) -> None:
    assert pf.Costs().cost_for("что угодно") is None
    assert pf.Costs().fee_percent == 0.0
    assert pf.Costs().per_lot == {}


# --- Арифметика ------------------------------------------------------------

def test_profit_subtracts_fee_and_cost(pf) -> None:
    costs = pf.Costs(fee_percent=10.0, per_lot={"Ключ": 60.0})
    rows = pf.calculate([sale("Ключ Steam", 100.0)], costs)

    assert len(rows) == 1
    row = rows[0]
    assert row.revenue == 100.0
    assert row.fee == 10.0
    assert row.cost == 60.0
    assert row.profit == 30.0
    assert row.margin_percent == pytest.approx(30.0)
    assert row.cost_known is True


def test_cost_multiplied_by_amount(pf) -> None:
    """Куплено 3 единицы - себестоимость тройная."""
    costs = pf.Costs(fee_percent=0.0, per_lot={"Ключ": 20.0})
    row = pf.calculate([sale("Ключ Steam", 100.0, amount=3)], costs)[0]

    assert row.cost == 60.0
    assert row.profit == 40.0


def test_zero_fee_means_no_deduction(pf) -> None:
    row = pf.calculate([sale("Ключ", 100.0)], pf.Costs(per_lot={"Ключ": 40.0}))[0]
    assert row.fee == 0.0
    assert row.profit == 60.0


def test_unknown_cost_is_flagged_not_guessed(pf) -> None:
    """
    Неизвестная себестоимость считается нулём, но строка помечается:
    иначе завышенная прибыль выглядела бы как настоящая.
    """
    row = pf.calculate([sale("Аккаунт", 100.0)], pf.Costs(fee_percent=10.0))[0]

    assert row.cost == 0.0
    assert row.cost_known is False
    assert row.profit == 90.0


def test_loss_is_negative(pf) -> None:
    costs = pf.Costs(fee_percent=20.0, per_lot={"Ключ": 90.0})
    row = pf.calculate([sale("Ключ", 100.0)], costs)[0]

    assert row.profit == pytest.approx(-10.0)


def test_sales_grouped_by_lot(pf) -> None:
    costs = pf.Costs(per_lot={"Ключ": 10.0})
    rows = pf.calculate([sale("Ключ A", 100.0), sale("Ключ A", 100.0),
                         sale("Ключ B", 50.0)], costs)

    assert len(rows) == 2
    lot_a = next(r for r in rows if r.lot == "Ключ A")
    assert lot_a.count == 2
    assert lot_a.revenue == 200.0
    assert lot_a.cost == 20.0


def test_currencies_are_not_mixed(pf) -> None:
    """Курс на момент продажи неизвестен, складывать валюты нельзя."""
    rows = pf.calculate([sale("Ключ", 100.0, currency="RUB"),
                         sale("Ключ", 5.0, currency="USD")], pf.Costs())

    assert len(rows) == 2
    assert {r.currency for r in rows} == {"RUB", "USD"}


def test_rows_sorted_by_profit_desc(pf) -> None:
    costs = pf.Costs(per_lot={"Дорогой": 10.0, "Дешёвый": 10.0})
    rows = pf.calculate([sale("Дешёвый", 20.0), sale("Дорогой", 500.0)], costs)

    assert rows[0].lot == "Дорогой"


def test_broken_sale_record_is_skipped(pf) -> None:
    bad = {"ts": datetime.now().isoformat(), "lot": "Ключ", "price": "не число"}
    rows = pf.calculate([bad, sale("Ключ", 100.0)], pf.Costs())

    assert len(rows) == 1
    assert rows[0].count == 1


def test_empty_sales_gives_no_rows(pf) -> None:
    assert pf.calculate([], pf.Costs()) == []


def test_zero_revenue_does_not_divide_by_zero(pf) -> None:
    row = pf.calculate([sale("Подарок", 0.0)], pf.Costs())[0]
    assert row.margin_percent == 0.0


# --- Чтение истории --------------------------------------------------------

def test_read_sales_filters_by_period(pf, workdir: Path) -> None:
    path = workdir / pf.SALES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sale("Свежий", 100.0, days_ago=1)) + "\n"
        + json.dumps(sale("Старый", 100.0, days_ago=60)) + "\n",
        encoding="utf-8")

    sales = pf.read_sales(30)
    assert [s["lot"] for s in sales] == ["Свежий"]


def test_read_sales_without_file(pf) -> None:
    assert pf.read_sales(30) == []


def test_read_sales_skips_corrupted_lines(pf, workdir: Path) -> None:
    path = workdir / pf.SALES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ битая строка\n" + json.dumps(sale("Ключ", 100.0)) + "\n",
                    encoding="utf-8")

    assert len(pf.read_sales(30)) == 1


# --- Хранение настроек -----------------------------------------------------

def test_costs_roundtrip(pf, workdir: Path) -> None:
    pf.save_costs(pf.Costs(fee_percent=12.5, per_lot={"Ключ": 99.0}))
    loaded = pf.load_costs()

    assert loaded.fee_percent == 12.5
    assert loaded.per_lot == {"Ключ": 99.0}


def test_missing_costs_file_gives_defaults(pf) -> None:
    costs = pf.load_costs()
    assert costs.fee_percent == pf.DEFAULT_FEE_PERCENT
    assert costs.per_lot == {}


def test_corrupted_costs_file_gives_defaults(pf, workdir: Path) -> None:
    path = workdir / pf.COSTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ это не json", encoding="utf-8")

    assert pf.load_costs().per_lot == {}


# --- Отчёты ---------------------------------------------------------------

def test_report_warns_about_unknown_cost(pf) -> None:
    rows = pf.calculate([sale("Аккаунт", 100.0)], pf.Costs(fee_percent=10.0))
    text = pf.format_report(rows, pf.Costs(fee_percent=10.0), 30)

    assert "не задана себестоимость" in text


def test_report_warns_about_zero_fee(pf) -> None:
    costs = pf.Costs(per_lot={"Ключ": 10.0})
    text = pf.format_report(pf.calculate([sale("Ключ", 100.0)], costs), costs, 30)

    assert "Комиссия площадки не задана" in text


def test_clean_report_has_no_warnings(pf) -> None:
    costs = pf.Costs(fee_percent=10.0, per_lot={"Ключ": 10.0})
    text = pf.format_report(pf.calculate([sale("Ключ", 100.0)], costs), costs, 30)

    assert "⚠️" not in text


def test_report_highlights_losing_lots(pf) -> None:
    costs = pf.Costs(fee_percent=20.0, per_lot={"Убыток": 95.0, "Профит": 10.0})
    rows = pf.calculate([sale("Убыток", 100.0), sale("Профит", 100.0)], costs)
    text = pf.format_report(rows, costs, 30)

    assert "В убыток" in text
    assert "Убыток" in text


def test_report_on_empty_history(pf) -> None:
    assert "продаж не найдено" in pf.format_report([], pf.Costs(), 30)


def test_costs_listing_when_empty(pf) -> None:
    text = pf.format_costs(pf.Costs())
    assert "не задана" in text
    assert "/setcost" in text


def test_costs_listing_shows_rules_specific_first(pf) -> None:
    """
    Конкретные правила показываются выше общих - в том же порядке, в котором
    их применяет cost_for(). Иначе список вводил бы в заблуждение.
    """
    costs = pf.Costs(fee_percent=10.0, per_lot={"Ключ": 50.0, "Ключ Steam Deluxe": 300.0})
    text = pf.format_costs(costs)

    assert text.index("<code>300</code>") < text.index("<code>50</code>")
    assert "10%" in text
