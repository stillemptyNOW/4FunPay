"""
Тесты выдачи товаров из товарного файла.

Главное, что здесь проверяется, - отсутствие коллизий: один и тот же ключ
не должен уйти двум покупателям при одновременных заказах.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

# cardinal_tools тянет bcrypt, requests, psutil и FunPayAPI. Если окружение
# неполное, тест честно пропускается, а не падает с ImportError.
cardinal_tools = pytest.importorskip("Utils.cardinal_tools",
                                     reason="нужны зависимости из requirements.txt")
exceptions = pytest.importorskip("Utils.exceptions")


def write_products(path: Path, count: int) -> None:
    """Создаёт товарный файл с указанным количеством строк."""
    path.write_text("\n".join(f"key-{i:04d}" for i in range(count)), encoding="utf-8")


# --- Базовое поведение -----------------------------------------------------

def test_takes_requested_amount_and_removes_it(workdir: Path) -> None:
    path = workdir / "storage/products/keys.txt"
    write_products(path, 5)

    products, left = cardinal_tools.get_products(str(path), 2)

    assert products == ["key-0000", "key-0001"]
    assert left == 3
    assert path.read_text(encoding="utf-8").splitlines() == ["key-0002", "key-0003", "key-0004"]


def test_counts_products_ignoring_blank_lines(workdir: Path) -> None:
    path = workdir / "storage/products/keys.txt"
    path.write_text("a\n\nb\n\n\nc\n", encoding="utf-8")
    assert cardinal_tools.count_products(str(path)) == 3


def test_count_products_of_missing_file_is_zero(workdir: Path) -> None:
    assert cardinal_tools.count_products(str(workdir / "storage/products/absent.txt")) == 0


def test_empty_file_raises(workdir: Path) -> None:
    path = workdir / "storage/products/keys.txt"
    path.write_text("", encoding="utf-8")
    with pytest.raises(exceptions.NoProductsError):
        cardinal_tools.get_products(str(path), 1)


def test_not_enough_products_raises_and_keeps_file(workdir: Path) -> None:
    """При нехватке товара файл не должен пострадать."""
    path = workdir / "storage/products/keys.txt"
    write_products(path, 2)

    with pytest.raises(exceptions.NotEnoughProductsError):
        cardinal_tools.get_products(str(path), 5)

    assert path.read_text(encoding="utf-8").splitlines() == ["key-0000", "key-0001"]


def test_add_products_returns_them_to_the_front(workdir: Path) -> None:
    """
    Возврат товара в начало файла используется, когда сообщение покупателю
    не отправилось: этот товар должен уйти следующему покупателю первым.
    """
    path = workdir / "storage/products/keys.txt"
    write_products(path, 2)

    cardinal_tools.add_products(str(path), ["returned-key"], at_zero_position=True)

    assert path.read_text(encoding="utf-8").splitlines()[0] == "returned-key"


# --- Отсутствие коллизий ---------------------------------------------------

def test_concurrent_delivery_never_hands_out_same_key(workdir: Path) -> None:
    """
    20 потоков одновременно забирают по одному товару из файла на 20 позиций.
    Без блокировки в get_products часть потоков прочитала бы файл до того,
    как другие его перезапишут, и один ключ ушёл бы нескольким покупателям.
    """
    path = workdir / "storage/products/keys.txt"
    total = 20
    write_products(path, total)

    handed_out: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(total)

    def worker() -> None:
        start.wait()
        try:
            products, _left = cardinal_tools.get_products(str(path), 1)
        except BaseException as exc:
            with lock:
                errors.append(exc)
            return
        with lock:
            handed_out.extend(products)

    threads = [threading.Thread(target=worker) for _ in range(total)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"неожиданные ошибки при параллельной выдаче: {errors}"
    assert len(handed_out) == total, "выдано не столько товаров, сколько запрошено"
    assert len(set(handed_out)) == total, "один и тот же товар выдан несколько раз"
    assert path.read_text(encoding="utf-8").strip() == "", "файл должен опустеть"


def test_concurrent_multi_amount_delivery_is_consistent(workdir: Path) -> None:
    """То же при выдаче по несколько единиц за заказ."""
    path = workdir / "storage/products/keys.txt"
    total = 40
    per_order = 4
    orders = total // per_order
    write_products(path, total)

    handed_out: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(orders)

    def worker() -> None:
        start.wait()
        products, _left = cardinal_tools.get_products(str(path), per_order)
        with lock:
            handed_out.extend(products)

    threads = [threading.Thread(target=worker) for _ in range(orders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(handed_out) == total
    assert len(set(handed_out)) == total


def test_lock_is_shared_between_equivalent_paths(workdir: Path) -> None:
    """
    Блокировка привязана к абсолютному пути: относительная и абсолютная запись
    одного файла должны брать одну и ту же блокировку.
    """
    relative = "storage/products/keys.txt"
    absolute = str(workdir / relative)
    write_products(workdir / relative, 1)

    assert cardinal_tools._products_lock(relative) is cardinal_tools._products_lock(absolute)
