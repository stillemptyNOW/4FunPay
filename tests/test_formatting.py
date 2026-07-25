"""
Тесты форматирования текстов: подстановка переменных в автовыдачу и автоответы,
разбор сообщения на части, перевод секунд в строку.

Всё это чистая логика без сети - именно то, что ломается тише всего:
неверная подстановка переменной уходит покупателю и заметна не сразу.
"""

from __future__ import annotations

from datetime import datetime

import pytest

cardinal_tools = pytest.importorskip("Utils.cardinal_tools",
                                     reason="нужны зависимости из requirements.txt")
types = pytest.importorskip("FunPayAPI.types")


# --- time_to_str -----------------------------------------------------------

@pytest.mark.parametrize("seconds, expected", [
    (0, "0 сек"),
    (5, "5сек"),
    (60, "1мин"),
    (61, "1мин 1сек"),
    (3600, "1ч"),
    (3661, "1ч 1мин 1сек"),
    (86400, "1д"),
    (90061, "1д 1ч 1мин 1сек"),
])
def test_time_to_str(seconds: int, expected: str) -> None:
    assert cardinal_tools.time_to_str(seconds) == expected


# --- get_month_name --------------------------------------------------------

def test_month_name_is_in_genitive_case() -> None:
    assert cardinal_tools.get_month_name(1) == "Января"
    assert cardinal_tools.get_month_name(12) == "Декабря"


def test_month_name_out_of_range_falls_back() -> None:
    """Некорректный номер месяца не должен приводить к IndexError."""
    assert cardinal_tools.get_month_name(13) == "Января"


# --- safe_text -------------------------------------------------------------

def test_safe_text_breaks_up_mentions() -> None:
    """
    Ник покупателя подставляется в сообщение. safe_text вставляет невидимые
    разделители, чтобы ник не превратился в упоминание или ссылку.
    """
    result = cardinal_tools.safe_text("username")
    assert result != "username"
    assert result.replace("⁣", "") == "username"


# --- format_order_text -----------------------------------------------------

def make_order(order_id: str = "ABC123", buyer: str = "buyer1",
               description: str = "Ключ Steam") -> "types.OrderShortcut":
    """Собирает минимальный заказ для проверки подстановок."""
    return types.OrderShortcut(
        id_=order_id,
        description=description,
        price=100.0,
        currency=types.Currency.RUB,
        buyer_username=buyer,
        buyer_id=555,
        chat_id=777,
        status=types.OrderStatuses.PAID,
        date=datetime.now(),
        subcategory_name="Ключи, Steam",
        subcategory=None,
        html="",
        dont_search_amount=True,
    )


def test_order_id_and_link_are_substituted() -> None:
    text = cardinal_tools.format_order_text("Заказ $order_id: $order_link", make_order())
    assert "ABC123" in text
    assert "https://funpay.com/orders/ABC123/" in text


def test_username_is_substituted_safely() -> None:
    text = cardinal_tools.format_order_text("Привет, $username!", make_order(buyer="buyer1"))
    assert "$username" not in text
    assert text.replace("⁣", "") == "Привет, buyer1!"


def test_unknown_variable_is_left_as_is() -> None:
    """
    Неизвестная переменная остаётся в тексте: так пользователь видит опечатку
    в шаблоне, а не получает пустое место в сообщении покупателю.
    """
    text = cardinal_tools.format_order_text("$no_such_variable", make_order())
    assert text == "$no_such_variable"


def test_product_placeholder_is_untouched_by_order_formatting() -> None:
    """
    $product подставляется отдельно, из товарного файла. Форматирование заказа
    не должно его затрагивать.
    """
    text = cardinal_tools.format_order_text("Товар: $product ($order_id)", make_order())
    assert "$product" in text
    assert "ABC123" in text


def test_date_variables_are_substituted() -> None:
    text = cardinal_tools.format_order_text("$date | $time | $date_text", make_order())
    for variable in ("$date", "$time", "$date_text"):
        assert variable not in text
