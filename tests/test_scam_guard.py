"""
Тесты скам-фильтра.

Отдельно проверяется главное: ядро действительно не выдаёт товар, когда
обработчик выставил флаг блокировки. Без этой проверки плагин мог бы
«срабатывать» в логах, а товар всё равно уходил бы покупателю.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("telebot", reason="нужен pytelegrambotapi из requirements.txt")

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "scam_guard.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("plugins.scam_guard", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["plugins.scam_guard"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sg(workdir: Path):
    return _load_plugin()


# --- Распознавание формулировок --------------------------------------------

@pytest.mark.parametrize("text", [
    "давай вне фанпея договоримся",
    "может мимо funpay?",
    "переведи на карту, так дешевле",
    "отмени заказ пожалуйста",
    "верни деньги срочно",
    "напиши в тг, обсудим",
    "сделай возврат и я куплю снова",
])
def test_outside_deal_phrases_are_detected(sg, text: str) -> None:
    assert sg.looks_like_outside_deal(text) is not None


@pytest.mark.parametrize("text", [
    "здравствуйте, когда будет ключ?",
    "спасибо, всё пришло",
    "а есть скидка на два ключа?",
    "подскажите, для какого региона активация",
])
def test_normal_messages_are_not_flagged(sg, text: str) -> None:
    assert sg.looks_like_outside_deal(text) is None


def test_detection_is_case_insensitive(sg) -> None:
    assert sg.looks_like_outside_deal("ВНЕ ФАНПЕЯ") is not None


# --- Решение о задержке ----------------------------------------------------

def test_disabled_guard_never_holds(sg) -> None:
    settings = sg.GuardSettings(enabled=False, price_threshold=100)
    assert sg.evaluate_order(99999, "RUB", "buyer", settings) is None


def test_price_above_threshold_holds(sg) -> None:
    settings = sg.GuardSettings(enabled=True, price_threshold=5000)
    reason = sg.evaluate_order(5000, "RUB", "buyer", settings)
    assert reason is not None and "5000" in reason


def test_price_below_threshold_passes(sg) -> None:
    settings = sg.GuardSettings(enabled=True, price_threshold=5000)
    assert sg.evaluate_order(4999, "RUB", "buyer", settings) is None


def test_zero_threshold_disables_price_rule(sg) -> None:
    settings = sg.GuardSettings(enabled=True, price_threshold=0)
    assert sg.evaluate_order(999999, "RUB", "buyer", settings) is None


def test_marked_buyer_is_held(sg) -> None:
    sg.mark_suspect("shady", "в переписке: «вне фанпея»")
    settings = sg.GuardSettings(enabled=True, check_messages=True)
    reason = sg.evaluate_order(100, "RUB", "shady", settings)
    assert reason is not None and "вне фанпея" in reason


def test_unmarked_buyer_passes(sg) -> None:
    sg.mark_suspect("shady", "причина")
    settings = sg.GuardSettings(enabled=True, check_messages=True)
    assert sg.evaluate_order(100, "RUB", "honest", settings) is None


def test_suspect_reason_without_file(sg) -> None:
    assert sg.suspect_reason("anyone") is None


# --- Настройки -------------------------------------------------------------

def test_settings_default_to_disabled(sg) -> None:
    """
    Фильтр задерживает выдачу, поэтому включать его должен человек,
    а не факт установки плагина.
    """
    assert sg.load_settings().enabled is False


def test_settings_roundtrip(sg) -> None:
    sg.save_settings(sg.GuardSettings(enabled=True, price_threshold=1234.5))
    loaded = sg.load_settings()
    assert loaded.enabled is True
    assert loaded.price_threshold == 1234.5


def test_corrupted_settings_give_defaults(sg, workdir: Path) -> None:
    path = workdir / sg.SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ битый json", encoding="utf-8")
    assert sg.load_settings().enabled is False


# --- Статистика по часам ---------------------------------------------------

def test_order_hours_are_counted(sg) -> None:
    sg.record_order_hour(datetime(2026, 7, 26, 14, 0))
    sg.record_order_hour(datetime(2026, 7, 26, 14, 30))
    sg.record_order_hour(datetime(2026, 7, 26, 9, 0))

    activity = sg.load_activity()
    assert activity[14] == 2
    assert activity[9] == 1


def test_peak_hours_pick_busiest(sg) -> None:
    activity = {9: 1, 14: 50, 15: 40, 3: 0}
    assert sg.peak_hours(activity, top=2) == {14, 15}


def test_peak_hours_ignore_empty_hours(sg) -> None:
    assert sg.peak_hours({5: 0, 6: 0}, top=3) == set()


def test_peak_hours_on_empty_data(sg) -> None:
    assert sg.peak_hours({}) == set()


def test_activity_report_without_data(sg) -> None:
    assert "не зафиксировано" in sg.format_activity({})


def test_activity_report_marks_peak(sg) -> None:
    text = sg.format_activity({14: 50, 15: 40, 3: 1})
    assert "Пик" in text
    assert "14:00" in text
    assert "🔥" in text


def test_activity_report_shows_all_24_hours(sg) -> None:
    text = sg.format_activity({12: 10})
    for hour in (0, 12, 23):
        assert f"<code>{hour:02d}</code>" in text


def test_corrupted_activity_gives_empty(sg, workdir: Path) -> None:
    path = workdir / sg.ACTIVITY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1,2,3]", encoding="utf-8")
    assert sg.load_activity() == {}


def test_activity_ignores_invalid_hours(sg, workdir: Path) -> None:
    path = workdir / sg.ACTIVITY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"14": 5, "99": 3, "abc": 1}', encoding="utf-8")
    assert sg.load_activity() == {14: 5}


# --- Ядро уважает флаг блокировки -----------------------------------------

class FakeOrder:
    def __init__(self) -> None:
        self.id = "TEST123"
        self.buyer_username = "buyer"
        self.description = "Ключ Steam"
        self.price = 100.0
        self.currency = None
        self.amount = 1


class FakeEvent:
    def __init__(self) -> None:
        self.order = FakeOrder()


class FakeSection:
    """Двойник секции конфига автовыдачи."""

    def getboolean(self, key: str, fallback: bool = False) -> bool:
        return False

    def get(self, key: str, fallback=None):
        return fallback

    def __getitem__(self, key: str) -> str:
        return "$product"


class FakeCardinal:
    def __init__(self) -> None:
        self.blacklist = []
        self.bl_delivery_enabled = False
        self.pre_delivery_handlers = []
        self.post_delivery_handlers = []
        self.delivered = []
        self.MAIN_CFG = {"FunPay": _AlwaysOn()}

    def run_handlers(self, handlers_list, args):
        for handler in handlers_list:
            handler(*args)


class _AlwaysOn:
    def getboolean(self, key: str) -> bool:
        return True


def test_core_skips_delivery_when_blocked(monkeypatch) -> None:
    """
    Флаг delivery_blocked должен реально останавливать выдачу.
    Если ядро его проигнорирует, товар уйдёт покупателю несмотря на фильтр -
    именно эту ошибку тест и ловит.
    """
    import handlers

    delivered = []
    monkeypatch.setattr(handlers, "deliver_goods",
                        lambda c, e, *a: delivered.append(e.order.id))

    cardinal = FakeCardinal()
    event = FakeEvent()
    event.config_section_obj = FakeSection()
    event.delivery_blocked = False

    def blocking_handler(c, e):
        e.delivery_blocked = True
        e.delivery_block_reason = "тестовая причина"

    cardinal.pre_delivery_handlers.append(blocking_handler)
    handlers.deliver_product_handler(cardinal, event)

    assert delivered == [], "товар был выдан несмотря на флаг блокировки"


def test_core_delivers_when_not_blocked(monkeypatch) -> None:
    """Обратная проверка: без флага выдача идёт как обычно."""
    import handlers

    delivered = []
    monkeypatch.setattr(handlers, "deliver_goods",
                        lambda c, e, *a: delivered.append(e.order.id))

    cardinal = FakeCardinal()
    event = FakeEvent()
    event.config_section_obj = FakeSection()
    event.delivery_blocked = False

    handlers.deliver_product_handler(cardinal, event)
    assert delivered == ["TEST123"]


def test_pre_delivery_handler_sets_flag(sg) -> None:
    """Плагин выставляет флаг, когда правило сработало."""
    sg.save_settings(sg.GuardSettings(enabled=True, price_threshold=50))

    class Crd:
        telegram = None

    event = FakeEvent()
    sg.on_pre_delivery(Crd(), event)

    assert getattr(event, "delivery_blocked") is True
    assert "50" in getattr(event, "delivery_block_reason")


def test_pre_delivery_handler_leaves_normal_order_alone(sg) -> None:
    sg.save_settings(sg.GuardSettings(enabled=True, price_threshold=5000))

    class Crd:
        telegram = None

    event = FakeEvent()
    sg.on_pre_delivery(Crd(), event)

    assert getattr(event, "delivery_blocked", False) is False


# --- Отложенное поднятие ---------------------------------------------------

class FakeCategory:
    def __init__(self) -> None:
        self.id = 1
        self.name = "Steam"


def test_raise_not_postponed_without_enough_data(sg) -> None:
    """
    При малой статистике выводы о пиках были бы выдумкой, поэтому
    поднятие не откладывается.
    """
    for _ in range(10):
        sg.record_order_hour(datetime(2026, 7, 26, 14, 0))

    category = FakeCategory()
    sg.on_pre_lots_raise(None, category)
    assert getattr(category, "postpone_raise", 0) == 0


def test_raise_not_postponed_during_peak(sg, monkeypatch) -> None:
    now = datetime.now()
    for _ in range(60):
        sg.record_order_hour(now)

    category = FakeCategory()
    sg.on_pre_lots_raise(None, category)
    assert getattr(category, "postpone_raise", 0) == 0
