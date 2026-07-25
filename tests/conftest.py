"""
Общая обвязка для тестов.

Тесты не обращаются к funpay.com и к api.telegram.org: там, где нужен
аккаунт, используется двойник :class:`FakeAccount`. Каждый тест работает
в своём временном каталоге, потому что код проекта много где рассчитывает
на относительные пути вида ``storage/products/...``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Тесты запускаются из корня проекта, но добавляем путь явно,
# чтобы работал и вызов вида `pytest tests/`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Переносит рабочий каталог в временный и создаёт структуру папок проекта.

    :return: путь до временного рабочего каталога.
    """
    for directory in ("configs", "logs", "storage/cache", "storage/products",
                      "storage/stats", "storage/audit", "plugins"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убирает переменные окружения проекта, чтобы тесты не влияли друг на друга."""
    for name in list(os.environ):
        if name.startswith("FOURFP_"):
            monkeypatch.delenv(name, raising=False)


class FakeAccount:
    """
    Двойник :class:`FunPayAPI.account.Account` для тестов.

    Повторяет только те атрибуты и методы, которые нужны проверяемому коду,
    и ничего не отправляет в сеть. Все вызовы записываются в :attr:`calls`,
    чтобы тест мог проверить, что именно бот попытался сделать.
    """

    def __init__(self, golden_key: str = "a" * 32, user_agent: str | None = None,
                 username: str = "test_shop", user_id: int = 1234567,
                 raise_on_get: Exception | None = None) -> None:
        self.golden_key = golden_key
        self.user_agent = user_agent
        self.username = username
        self.id = user_id
        self.active_sales = 0
        self.active_purchases = 0
        self.csrf_token = "csrf-token-stub"
        self.phpsessid = "phpsessid-stub"
        self.total_balance = 0
        self.locale = "ru"
        self.runner = None

        self._raise_on_get = raise_on_get
        self._initiated = False
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    @property
    def is_initiated(self) -> bool:
        return self._initiated

    def get(self, update_phpsessid: bool = False) -> "FakeAccount":
        """Имитирует загрузку главной страницы FunPay."""
        self._record("get", update_phpsessid=update_phpsessid)
        if self._raise_on_get is not None:
            raise self._raise_on_get
        self._initiated = True
        return self

    def send_message(self, chat_id: int | str, text: str, *args: Any, **kwargs: Any) -> Any:
        """Имитирует отправку сообщения в чат FunPay."""
        self._record("send_message", chat_id, text, *args, **kwargs)
        return object()

    def send_image(self, chat_id: int | str, image_id: int, *args: Any, **kwargs: Any) -> Any:
        """Имитирует отправку изображения в чат FunPay."""
        self._record("send_image", chat_id, image_id, *args, **kwargs)
        return object()

    def raise_lots(self, category_id: int) -> int:
        """Имитирует поднятие лотов; возвращает время до следующей попытки."""
        self._record("raise_lots", category_id)
        return 3600

    def get_chat_by_name(self, name: str, *args: Any, **kwargs: Any) -> None:
        self._record("get_chat_by_name", name)
        return None


@pytest.fixture
def fake_account() -> FakeAccount:
    """Готовый двойник аккаунта FunPay."""
    return FakeAccount()
