"""
Тесты плагина статистики продаж.

Проверяются чистые функции: запись без дублей, агрегация, формирование CSV.
Сеть и Telegram не задействованы.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Плагин импортирует telebot для типов. При неполном окружении - пропуск.
pytest.importorskip("telebot", reason="нужен pytelegrambotapi из requirements.txt")

import importlib.util
import sys

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "sales_stats.py"


def _load_plugin():
    """Загружает плагин как модуль так же, как это делает ядро."""
    spec = importlib.util.spec_from_file_location("plugins.sales_stats", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["plugins.sales_stats"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stats(workdir: Path):
    """Свежий экземпляр плагина в чистом рабочем каталоге."""
    return _load_plugin()


# --- Запись продаж ---------------------------------------------------------

def test_record_sale_writes_one_line(stats, workdir: Path) -> None:
    assert stats.record_sale("A-1", "buyer", "Ключ Steam", 1, 100.0, "RUB") is True

    lines = (workdir / stats.SALES_FILE).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["order_id"] == "A-1"
    assert record["buyer"] == "buyer"
    assert record["price"] == 100.0


def test_duplicate_order_is_not_counted_twice(stats) -> None:
    """
    Смена статуса приходит по тому же заказу. Без защиты от повторов
    выручка удваивалась бы на каждом событии.
    """
    assert stats.record_sale("A-1", "buyer", "Лот", 1, 100.0, "RUB") is True
    assert stats.record_sale("A-1", "buyer", "Лот", 1, 100.0, "RUB") is False

    assert len(stats.load_sales()) == 1


def test_load_sales_skips_corrupted_lines(stats, workdir: Path) -> None:
    """Битая строка не должна ронять всю статистику."""
    path = workdir / stats.SALES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ts": datetime.now().isoformat(), "order_id": "ok",
                    "buyer": "b", "lot": "l", "amount": 1, "price": 10, "currency": "RUB"}) + "\n"
        "{ это не json\n"
        + json.dumps({"нет обязательных полей": True}) + "\n",
        encoding="utf-8")

    sales = stats.load_sales()
    assert len(sales) == 1
    assert sales[0].order_id == "ok"


def test_load_sales_respects_since(stats, workdir: Path) -> None:
    path = workdir / stats.SALES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    new = datetime.now().isoformat(timespec="seconds")
    path.write_text(
        json.dumps({"ts": old, "order_id": "old", "buyer": "b", "lot": "l",
                    "amount": 1, "price": 10, "currency": "RUB"}) + "\n"
        + json.dumps({"ts": new, "order_id": "new", "buyer": "b", "lot": "l",
                      "amount": 1, "price": 10, "currency": "RUB"}) + "\n",
        encoding="utf-8")

    recent = stats.load_sales(since=datetime.now() - timedelta(days=1))
    assert [sale.order_id for sale in recent] == ["new"]


# --- Агрегация -------------------------------------------------------------

def test_money_does_not_mix_currencies(stats) -> None:
    """
    Валюты не складываются: курс на момент продажи неизвестен, а пересчёт
    по текущему исказил бы историю.
    """
    stats.record_sale("A-1", "b", "l", 1, 100.0, "RUB")
    stats.record_sale("A-2", "b", "l", 1, 5.0, "USD")

    summary = stats._money(stats.load_sales())
    assert "100 RUB" in summary
    assert "5 USD" in summary


def test_daily_report_on_empty_history(stats) -> None:
    report = stats.build_daily_report(7)
    assert "не зафиксировано" in report


def test_daily_report_counts_orders(stats) -> None:
    for index in range(3):
        stats.record_sale(f"A-{index}", "buyer", "Лот", 1, 50.0, "RUB")

    report = stats.build_daily_report(7)
    assert "Всего заказов" in report
    assert "<b>3</b>" in report


def test_top_lots_orders_by_frequency(stats) -> None:
    stats.record_sale("A-1", "b1", "Популярный лот", 1, 10, "RUB")
    stats.record_sale("A-2", "b2", "Популярный лот", 1, 10, "RUB")
    stats.record_sale("A-3", "b3", "Редкий лот", 1, 10, "RUB")

    lines = stats._top_lots_section(stats.load_sales())
    joined = "\n".join(lines)
    assert joined.index("Популярный лот") < joined.index("Редкий лот")


def test_repeat_buyers_section_empty_without_repeats(stats) -> None:
    stats.record_sale("A-1", "buyer1", "Лот", 1, 10, "RUB")
    assert stats._repeat_buyers_section(stats.load_sales()) == []


def test_repeat_buyers_section_lists_returning_buyer(stats) -> None:
    stats.record_sale("A-1", "buyer1", "Лот", 1, 10, "RUB")
    stats.record_sale("A-2", "buyer1", "Лот", 1, 10, "RUB")

    lines = stats._repeat_buyers_section(stats.load_sales())
    assert any("buyer1" in line for line in lines)


def test_bar_scales_to_peak(stats) -> None:
    assert stats._bar(10, 10) == "█" * stats.CHART_WIDTH
    assert stats._bar(0, 10) == ""
    # Ненулевое значение обязано быть видно хотя бы одним символом.
    assert stats._bar(1, 1000) == "█"


def test_bar_handles_zero_peak(stats) -> None:
    assert stats._bar(0, 0) == ""


# --- Экспорт ---------------------------------------------------------------

def test_csv_export_has_bom_and_semicolons(stats) -> None:
    """
    Excel на русской локали открывает CSV без диалога импорта только при
    UTF-8 с BOM и точке с запятой как разделителе.
    """
    stats.record_sale("A-1", "покупатель", "Ключ Steam", 2, 199.5, "RUB")

    data = stats.build_csv()
    assert data.startswith(b"\xef\xbb\xbf")

    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    assert rows[0][0] == "Дата"
    assert rows[1][2] == "A-1"
    assert rows[1][3] == "покупатель"
    # Дробный разделитель - запятая, иначе Excel не распознает число.
    assert rows[1][6] == "199,5"


def test_csv_export_is_empty_but_valid_without_sales(stats) -> None:
    rows = list(csv.reader(io.StringIO(stats.build_csv().decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 1
    assert rows[0][0] == "Дата"
