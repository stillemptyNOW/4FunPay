"""
Тесты монитора цен.

Проверяется расчёт позиции и обнаружение изменений на выдуманных лотах.
Сеть не задействована: ``analyze_subcategory`` и ``detect_changes`` -
чистые функции, они и есть вся логика плагина.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("telebot", reason="нужен pytelegrambotapi из requirements.txt")

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "price_monitor.py"

OUR_ID = 1000
"""ID нашего аккаунта в тестах."""


def _load_plugin():
    """Загружает плагин так же, как это делает ядро."""
    spec = importlib.util.spec_from_file_location("plugins.price_monitor", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["plugins.price_monitor"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pm(workdir: Path):
    """Плагин в чистом рабочем каталоге."""
    return _load_plugin()


class FakeSeller:
    """Минимальный двойник SellerShortcut."""

    def __init__(self, seller_id: int, username: str, online: bool = False,
                 reviews: int = 0) -> None:
        self.id = seller_id
        self.username = username
        self.online = online
        self.reviews = reviews


class FakeLot:
    """Минимальный двойник LotShortcut."""

    def __init__(self, price: float, seller: FakeSeller | None, description: str = "Ключ",
                 auto: bool = False, promo: bool = False, amount: int | None = None) -> None:
        self.price = price
        self.seller = seller
        self.description = description
        self.auto = auto
        self.promo = promo
        self.amount = amount
        self.currency = "RUB"


def ours(price: float, **kwargs) -> FakeLot:
    return FakeLot(price, FakeSeller(OUR_ID, "my_shop"), **kwargs)


def rival(price: float, name: str = "rival", **kwargs) -> FakeLot:
    return FakeLot(price, FakeSeller(hash(name) % 9999 + 1, name), **kwargs)


# --- Расчёт позиции --------------------------------------------------------

def test_cheapest_lot_is_first(pm) -> None:
    lots = [ours(100), rival(150, "a"), rival(200, "b")]
    positions = pm.analyze_subcategory(lots, OUR_ID, "Ключи, Steam")

    assert len(positions) == 1
    assert positions[0].rank == 1
    assert positions[0].total == 3
    assert positions[0].cheaper == []
    assert positions[0].in_top is True
    assert positions[0].best_competitor_price is None


def test_rank_counts_cheaper_rivals(pm) -> None:
    lots = [rival(50, "a"), rival(70, "b"), ours(100), rival(200, "c")]
    position = pm.analyze_subcategory(lots, OUR_ID, "Ключи")[0]

    assert position.rank == 3
    assert len(position.cheaper) == 2
    assert position.best_competitor_price == 50
    # Конкуренты идут от самого дешёвого.
    assert [c.price for c in position.cheaper] == [50, 70]


def test_out_of_top_is_detected(pm) -> None:
    lots = [rival(i * 10, f"r{i}") for i in range(1, 6)] + [ours(100)]
    position = pm.analyze_subcategory(lots, OUR_ID, "Ключи")[0]

    assert position.rank == 6
    assert position.in_top is False


def test_promo_lots_excluded_from_ranking(pm) -> None:
    """
    Промо-лоты стоят выше выдачи независимо от цены, поэтому в сравнении
    цен не участвуют - иначе позиция считалась бы неверно.
    """
    lots = [rival(10, "promo_guy", promo=True), rival(150, "a"), ours(100)]
    position = pm.analyze_subcategory(lots, OUR_ID, "Ключи")[0]

    assert position.total == 2
    assert position.rank == 1
    assert position.cheaper == []


def test_our_other_lots_are_not_competitors(pm) -> None:
    """Свой второй лот дешевле не должен считаться конкурентом себе."""
    lots = [ours(50, description="Лот А"), ours(100, description="Лот Б"), rival(150, "a")]
    positions = pm.analyze_subcategory(lots, OUR_ID, "Ключи")

    assert len(positions) == 2
    expensive = next(p for p in positions if p.our_price == 100)
    assert expensive.rank == 2
    assert expensive.cheaper == []


def test_competitor_flags_are_captured(pm) -> None:
    lots = [rival(50, "fast", auto=True, amount=7), ours(100)]
    lots[0].seller.online = True

    position = pm.analyze_subcategory(lots, OUR_ID, "Ключи")[0]
    competitor = position.cheaper[0]

    assert competitor.auto is True
    assert competitor.online is True
    assert competitor.amount == 7
    assert "авто" in competitor.format()
    assert "онлайн" in competitor.format()


def test_lot_without_seller_does_not_crash(pm) -> None:
    """Разметка FunPay иногда не отдаёт продавца - это не должно ронять цикл."""
    lots = [FakeLot(50, None), ours(100)]
    positions = pm.analyze_subcategory(lots, OUR_ID, "Ключи")

    assert len(positions) == 1
    assert positions[0].rank == 2


def test_no_positions_when_we_have_no_lots(pm) -> None:
    assert pm.analyze_subcategory([rival(50, "a"), rival(70, "b")], OUR_ID, "Ключи") == []


def test_empty_subcategory(pm) -> None:
    assert pm.analyze_subcategory([], OUR_ID, "Ключи") == []


# --- Обнаружение изменений -------------------------------------------------

def make_position(pm, rank: int, best: float | None, description: str = "Ключ"):
    cheaper = [pm.Competitor("rival", best, False, False, 0, None)] if best is not None else []
    return pm.LotPosition(
        subcategory_name="Ключи",
        lot_description=description,
        our_price=100,
        currency="RUB",
        rank=rank,
        total=10,
        cheaper=cheaper,
    )


def test_first_run_produces_no_alerts(pm) -> None:
    """
    На первом проходе прошлого среза нет. Уведомлять не о чем - иначе
    установка плагина обернулась бы пачкой алертов по всем лотам.
    """
    positions = [make_position(pm, rank=5, best=50)]
    assert pm.detect_changes(positions, {}) == []


def test_drop_out_of_top_is_reported(pm) -> None:
    position = make_position(pm, rank=7, best=50)
    previous = {pm.position_key(position): {"rank": 2, "best": 50}}

    changes = pm.detect_changes([position], previous)
    assert len(changes) == 1
    assert changes[0][1] == "dropped"


def test_movement_inside_top_is_not_reported(pm) -> None:
    """Сдвиг с 1 на 2 место внутри топа решения не требует."""
    position = make_position(pm, rank=2, best=50)
    previous = {pm.position_key(position): {"rank": 1, "best": 50}}

    assert pm.detect_changes([position], previous) == []


def test_new_cheaper_competitor_is_reported(pm) -> None:
    position = make_position(pm, rank=2, best=40)
    previous = {pm.position_key(position): {"rank": 2, "best": 60}}

    changes = pm.detect_changes([position], previous)
    assert len(changes) == 1
    assert changes[0][1] == "undercut"


def test_competitor_raising_price_is_not_reported(pm) -> None:
    position = make_position(pm, rank=2, best=80)
    previous = {pm.position_key(position): {"rank": 2, "best": 60}}

    assert pm.detect_changes([position], previous) == []


def test_first_competitor_appearing_is_reported(pm) -> None:
    """Были первыми без конкурентов, кто-то встал дешевле."""
    position = make_position(pm, rank=2, best=90)
    previous = {pm.position_key(position): {"rank": 1, "best": None}}

    changes = pm.detect_changes([position], previous)
    assert changes and changes[0][1] == "undercut"


# --- Хранение среза --------------------------------------------------------

def test_state_roundtrip(pm, workdir: Path) -> None:
    pm.save_state({"Ключи|Лот": {"rank": 3, "best": 55.5}})
    assert pm.load_state() == {"Ключи|Лот": {"rank": 3, "best": 55.5}}


def test_missing_state_file_gives_empty(pm) -> None:
    assert pm.load_state() == {}


def test_corrupted_state_gives_empty(pm, workdir: Path) -> None:
    path = workdir / pm.STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ это не json", encoding="utf-8")
    assert pm.load_state() == {}


# --- Форматирование --------------------------------------------------------

def test_alert_contains_actionable_data(pm) -> None:
    position = make_position(pm, rank=7, best=50)
    text = pm.format_alert(position, "dropped")

    assert "вытеснен" in text
    assert "Ключи" in text
    assert "7" in text and "10" in text
    assert "rival" in text


def test_undercut_alert_has_different_header(pm) -> None:
    position = make_position(pm, rank=2, best=50)
    assert "подрезали" in pm.format_alert(position, "undercut")


def test_snapshot_reports_top_count(pm) -> None:
    positions = [make_position(pm, rank=1, best=None, description="A"),
                 make_position(pm, rank=9, best=50, description="B")]
    text = pm.format_snapshot(positions)

    assert "Лотов отслеживается" in text
    assert "<b>2</b>" in text
    assert "🟢" in text and "🔴" in text


def test_snapshot_on_empty_input(pm) -> None:
    assert "не найдено" in pm.format_snapshot([])


def test_snapshot_shows_worst_positions_first(pm) -> None:
    positions = [make_position(pm, rank=1, best=None, description="Лучший"),
                 make_position(pm, rank=20, best=10, description="Худший")]
    text = pm.format_snapshot(positions)
    assert text.index("Худший") < text.index("Лучший")
