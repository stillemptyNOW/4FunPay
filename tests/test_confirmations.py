"""
Тесты подтверждения критичных действий.

Проверяется именно защитное поведение: срок жизни, лимит попыток, привязка
к пользователю и чату, невозможность подтвердить одно действие дважды.
"""

from __future__ import annotations

import pytest

from Utils.confirmations import (ConfirmationManager, CriticalAction, PendingConfirmation,
                                 VerdictReason)

PASSWORD = "CorrectHorse1"
PASSWORD_HASH = "hash-of-CorrectHorse1"
OWNER_ID = 111
OWNER_CHAT = 222


def check_password(password: str, password_hash: str) -> bool:
    """Заглушка сверки пароля вместо bcrypt."""
    return password_hash == f"hash-of-{password}"


@pytest.fixture
def manager() -> ConfirmationManager:
    return ConfirmationManager(ttl=100, max_attempts=3)


def require(manager: ConfirmationManager, now: float = 1000.0) -> PendingConfirmation:
    return manager.require(CriticalAction.CHANGE_GOLDEN_KEY, OWNER_ID, OWNER_CHAT,
                           payload={"value": "x" * 32}, now=now)


# --- Успешный путь ---------------------------------------------------------

def test_correct_password_confirms_action(manager: ConfirmationManager) -> None:
    require(manager)
    verdict = manager.verify(OWNER_ID, OWNER_CHAT, PASSWORD, check_password, PASSWORD_HASH, now=1000.0)

    assert verdict.confirmed is True
    assert verdict.pending is not None
    assert verdict.pending.action is CriticalAction.CHANGE_GOLDEN_KEY
    assert verdict.pending.payload == {"value": "x" * 32}


def test_confirmed_action_cannot_be_replayed(manager: ConfirmationManager) -> None:
    """
    После подтверждения запрос удаляется: повторный ввод того же пароля
    не должен выполнить действие второй раз.
    """
    require(manager)
    assert manager.verify(OWNER_ID, OWNER_CHAT, PASSWORD, check_password, PASSWORD_HASH, now=1000.0).confirmed
    second = manager.verify(OWNER_ID, OWNER_CHAT, PASSWORD, check_password, PASSWORD_HASH, now=1000.0)

    assert second.confirmed is False
    assert second.reason is VerdictReason.NO_PENDING


# --- Неверный пароль -------------------------------------------------------

def test_wrong_password_is_rejected_and_counts_attempt(manager: ConfirmationManager) -> None:
    require(manager)
    verdict = manager.verify(OWNER_ID, OWNER_CHAT, "неверный", check_password, PASSWORD_HASH, now=1000.0)

    assert verdict.confirmed is False
    assert verdict.reason is VerdictReason.WRONG_PASSWORD
    assert verdict.attempts_left == 2


def test_attempts_are_exhausted_and_request_burns(manager: ConfirmationManager) -> None:
    """Перебор пароля прямо в чате должен упираться в лимит."""
    require(manager)
    for _ in range(2):
        manager.verify(OWNER_ID, OWNER_CHAT, "неверный", check_password, PASSWORD_HASH, now=1000.0)

    third = manager.verify(OWNER_ID, OWNER_CHAT, "неверный", check_password, PASSWORD_HASH, now=1000.0)
    assert third.reason is VerdictReason.TOO_MANY_ATTEMPTS
    assert third.attempts_left == 0
    assert manager.pending_count == 0

    # Даже верный пароль после сгорания запроса ничего не подтверждает.
    after = manager.verify(OWNER_ID, OWNER_CHAT, PASSWORD, check_password, PASSWORD_HASH, now=1000.0)
    assert after.confirmed is False
    assert after.reason is VerdictReason.NO_PENDING


def test_empty_password_hash_never_confirms(manager: ConfirmationManager) -> None:
    """Пустой хеш в конфиге не должен означать «пускать всех»."""
    require(manager)
    verdict = manager.verify(OWNER_ID, OWNER_CHAT, "", check_password, "", now=1000.0)
    assert verdict.confirmed is False


def test_broken_hash_does_not_confirm(manager: ConfirmationManager) -> None:
    """Исключение внутри сверки пароля трактуется как отказ, а не как успех."""
    def raising_check(password: str, password_hash: str) -> bool:
        raise ValueError("битый хеш")

    require(manager)
    verdict = manager.verify(OWNER_ID, OWNER_CHAT, PASSWORD, raising_check, "мусор", now=1000.0)
    assert verdict.confirmed is False
    assert verdict.reason is VerdictReason.WRONG_PASSWORD


# --- Срок жизни ------------------------------------------------------------

def test_expired_request_is_rejected(manager: ConfirmationManager) -> None:
    require(manager, now=1000.0)
    verdict = manager.verify(OWNER_ID, OWNER_CHAT, PASSWORD, check_password, PASSWORD_HASH, now=1101.0)

    assert verdict.confirmed is False
    assert verdict.reason is VerdictReason.EXPIRED


def test_request_valid_just_before_deadline(manager: ConfirmationManager) -> None:
    require(manager, now=1000.0)
    verdict = manager.verify(OWNER_ID, OWNER_CHAT, PASSWORD, check_password, PASSWORD_HASH, now=1099.0)
    assert verdict.confirmed is True


def test_get_drops_expired_request(manager: ConfirmationManager) -> None:
    require(manager, now=1000.0)
    assert manager.get(OWNER_ID, now=1050.0) is not None
    assert manager.get(OWNER_ID, now=1200.0) is None
    assert manager.pending_count == 0


def test_seconds_left_counts_down(manager: ConfirmationManager) -> None:
    pending = require(manager, now=1000.0)
    assert pending.seconds_left(manager.ttl, now=1000.0) == 100
    assert pending.seconds_left(manager.ttl, now=1060.0) == 40
    assert pending.seconds_left(manager.ttl, now=9999.0) == 0


def test_purge_expired_removes_only_stale(manager: ConfirmationManager) -> None:
    manager.require(CriticalAction.POWER_OFF, 1, 1, now=1000.0)
    manager.require(CriticalAction.RESTORE_BACKUP, 2, 2, now=1080.0)

    assert manager.purge_expired(now=1120.0) == 1
    assert manager.pending_count == 1


# --- Привязка к пользователю и чату ---------------------------------------

def test_another_user_cannot_confirm(manager: ConfirmationManager) -> None:
    require(manager)
    verdict = manager.verify(999, OWNER_CHAT, PASSWORD, check_password, PASSWORD_HASH, now=1000.0)

    assert verdict.confirmed is False
    assert verdict.reason is VerdictReason.NO_PENDING
    # Запрос владельца при этом не должен пострадать.
    assert manager.get(OWNER_ID, now=1000.0) is not None


def test_confirmation_in_another_chat_is_rejected(manager: ConfirmationManager) -> None:
    """
    Действие запрошено в личке, подтверждение прислано в группу - отказ.
    Иначе критичное действие можно было бы протащить из чата,
    куда бота добавили посторонние.
    """
    require(manager)
    verdict = manager.verify(OWNER_ID, 555, PASSWORD, check_password, PASSWORD_HASH, now=1000.0)

    assert verdict.confirmed is False
    assert verdict.reason is VerdictReason.NO_PENDING


# --- Замена и отмена -------------------------------------------------------

def test_new_request_replaces_previous(manager: ConfirmationManager) -> None:
    manager.require(CriticalAction.POWER_OFF, OWNER_ID, OWNER_CHAT, now=1000.0)
    manager.require(CriticalAction.CHANGE_GOLDEN_KEY, OWNER_ID, OWNER_CHAT, now=1001.0)

    assert manager.pending_count == 1
    pending = manager.get(OWNER_ID, now=1001.0)
    assert pending is not None and pending.action is CriticalAction.CHANGE_GOLDEN_KEY


def test_cancel_removes_request(manager: ConfirmationManager) -> None:
    require(manager)
    manager.cancel(OWNER_ID)

    assert manager.pending_count == 0
    assert manager.verify(OWNER_ID, OWNER_CHAT, PASSWORD, check_password,
                          PASSWORD_HASH, now=1000.0).reason is VerdictReason.NO_PENDING


def test_cancel_of_absent_request_is_safe(manager: ConfirmationManager) -> None:
    manager.cancel(12345)


def test_requests_of_different_users_are_independent(manager: ConfirmationManager) -> None:
    manager.require(CriticalAction.POWER_OFF, 1, 10, now=1000.0)
    manager.require(CriticalAction.POWER_OFF, 2, 20, now=1000.0)

    manager.verify(1, 10, "неверный", check_password, PASSWORD_HASH, now=1000.0)

    assert manager.get(1, now=1000.0).attempts == 1
    assert manager.get(2, now=1000.0).attempts == 0


# --- Перечень критичных действий ------------------------------------------

def test_critical_actions_are_string_values() -> None:
    """StrEnum нужен, чтобы значение можно было положить в state Telegram-ПУ."""
    assert CriticalAction.CHANGE_GOLDEN_KEY == "change_golden_key"
    assert str(CriticalAction.POWER_OFF) == "power_off"


def test_critical_actions_are_unique() -> None:
    values = [action.value for action in CriticalAction]
    assert len(values) == len(set(values))
