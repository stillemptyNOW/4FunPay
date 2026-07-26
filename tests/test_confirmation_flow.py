"""
Интеграционные тесты подтверждения критичных действий в панели управления.

Проверяется связка `TGBot.require_confirmation` + `TGBot.confirm_password`:
что действие не выполняется без пароля, выполняется с ним, и что пароль
удаляется из чата. Telegram подменён двойником, сеть не задействована.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("telebot", reason="нужен pytelegrambotapi из requirements.txt")

from tg_bot import CBT
from tg_bot.bot import TGBot
from Utils.confirmations import CriticalAction

PASSWORD = "CorrectHorse1"
OWNER_ID = 4242
CHAT_ID = 777


class FakeBot:
    """Двойник telebot.TeleBot: копит отправленное и удалённое."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.deleted: list[tuple[int, int]] = []
        self._next_id = 1000

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        self._next_id += 1
        return SimpleNamespace(id=self._next_id, chat=SimpleNamespace(id=chat_id))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def make_message(text: str, message_id: int = 1):
    """Собирает минимальное сообщение Telegram."""
    return SimpleNamespace(
        text=text,
        id=message_id,
        chat=SimpleNamespace(id=CHAT_ID),
        from_user=SimpleNamespace(id=OWNER_ID, username="owner"),
    )


@pytest.fixture
def tg(workdir, monkeypatch: pytest.MonkeyPatch) -> TGBot:
    """
    Экземпляр TGBot без подключения к Telegram.

    Создаётся в обход __init__: настоящий конструктор поднимает telebot.TeleBot
    и читает кэши, а для проверки логики подтверждения нужны только
    несколько полей.
    """
    from Utils.cardinal_tools import hash_password
    from Utils.confirmations import ConfirmationManager

    config = {"Telegram": {"secretKeyHash": hash_password(PASSWORD)}}
    cardinal = SimpleNamespace(MAIN_CFG=config, instance_id=1, account=None)

    bot = TGBot.__new__(TGBot)
    bot.cardinal = cardinal
    bot.bot = FakeBot()
    bot.user_states = {}
    bot.confirmations = ConfirmationManager(ttl=100, max_attempts=3)
    return bot


def _pending_state(tg: TGBot) -> str | None:
    state = tg.get_state(CHAT_ID, OWNER_ID)
    return state["state"] if state else None


# --- Запрос подтверждения --------------------------------------------------

def test_require_confirmation_sets_state_and_asks(tg: TGBot) -> None:
    executed = []
    tg.require_confirmation(make_message("/golden_key"), CriticalAction.CHANGE_GOLDEN_KEY,
                            lambda m: executed.append(m))

    assert _pending_state(tg) == CBT.CONFIRM_PASSWORD
    assert "подтверждение" in tg.bot.sent[0].lower()
    assert "смена golden_key" in tg.bot.sent[0]
    assert executed == [], "действие не должно выполняться до ввода пароля"


# --- Успешное подтверждение ------------------------------------------------

def test_correct_password_runs_action(tg: TGBot) -> None:
    executed = []
    tg.require_confirmation(make_message("/golden_key"), CriticalAction.CHANGE_GOLDEN_KEY,
                            lambda m: executed.append(m))
    tg.confirm_password(make_message(PASSWORD, message_id=2))

    assert len(executed) == 1, "действие должно выполниться ровно один раз"
    assert _pending_state(tg) is None


def test_password_message_is_deleted_from_chat(tg: TGBot) -> None:
    """Пароль не должен оставаться в истории переписки."""
    tg.require_confirmation(make_message("/golden_key"), CriticalAction.CHANGE_GOLDEN_KEY,
                            lambda m: None)
    tg.confirm_password(make_message(PASSWORD, message_id=99))

    assert (CHAT_ID, 99) in tg.bot.deleted


def test_action_cannot_be_replayed(tg: TGBot) -> None:
    executed = []
    tg.require_confirmation(make_message("/golden_key"), CriticalAction.CHANGE_GOLDEN_KEY,
                            lambda m: executed.append(m))
    tg.confirm_password(make_message(PASSWORD, message_id=2))
    tg.confirm_password(make_message(PASSWORD, message_id=3))

    assert len(executed) == 1, "повторный ввод пароля не должен выполнить действие снова"


# --- Неверный пароль -------------------------------------------------------

def test_wrong_password_does_not_run_action(tg: TGBot) -> None:
    executed = []
    tg.require_confirmation(make_message("/golden_key"), CriticalAction.CHANGE_GOLDEN_KEY,
                            lambda m: executed.append(m))
    tg.confirm_password(make_message("неверный", message_id=2))

    assert executed == []
    assert "Осталось попыток" in tg.bot.sent[-1]
    # Состояние возвращено: можно попробовать ещё раз.
    assert _pending_state(tg) == CBT.CONFIRM_PASSWORD


def test_attempts_run_out_and_action_is_lost(tg: TGBot) -> None:
    executed = []
    tg.require_confirmation(make_message("/golden_key"), CriticalAction.CHANGE_GOLDEN_KEY,
                            lambda m: executed.append(m))
    for i in range(3):
        tg.confirm_password(make_message("неверный", message_id=10 + i))

    assert "Попытки исчерпаны" in tg.bot.sent[-1]
    tg.confirm_password(make_message(PASSWORD, message_id=20))
    assert executed == [], "после сгорания запроса верный пароль не должен помочь"


# --- Подключение к реальным командам ---------------------------------------

def test_change_cookie_requires_confirmation_first(tg: TGBot, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    /golden_key не должен сразу включать режим ввода ключа.
    Сначала пароль, и только потом состояние CHANGE_GOLDEN_KEY.
    """
    monkeypatch.setattr("Utils.secrets.is_from_env", lambda _var: False)

    tg.act_change_cookie(make_message("/golden_key"))
    assert _pending_state(tg) == CBT.CONFIRM_PASSWORD

    tg.confirm_password(make_message(PASSWORD, message_id=2))
    assert _pending_state(tg) == CBT.CHANGE_GOLDEN_KEY


def test_change_cookie_blocked_when_key_from_env(tg: TGBot, monkeypatch: pytest.MonkeyPatch) -> None:
    """Если ключ задан окружением, подтверждение даже не запрашивается."""
    monkeypatch.setattr("Utils.secrets.is_from_env", lambda _var: True)

    tg.act_change_cookie(make_message("/golden_key"))

    assert _pending_state(tg) is None
    assert "окружения" in tg.bot.sent[0]


def test_power_off_requires_confirmation(tg: TGBot) -> None:
    tg.ask_power_off(make_message("/power_off"))

    assert _pending_state(tg) == CBT.CONFIRM_PASSWORD
    assert "выключение бота" in tg.bot.sent[0]
