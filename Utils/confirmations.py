"""
Подтверждение критичных действий повторным вводом пароля.

Зачем. Авторизация в панели управления одноразовая: пароль вводится один раз,
дальше Telegram-аккаунт считается доверенным навсегда. Если чужой человек
получил доступ к разблокированному Telegram владельца, он может сменить
golden_key, восстановить чужой бэкап или выключить бота - без единой проверки.

Что делает модуль. Перед критичным действием бот запрашивает пароль панели
заново. Это второй фактор относительно «доступ к переписке уже есть»:
пароль хранится только как bcrypt-хеш и в самом Telegram его нет.

Ограничения, заложенные намеренно:

* **Срок жизни.** Запрос действует :data:`DEFAULT_TTL` секунд. Забытое
  незавершённое подтверждение не должно висеть до вечера.
* **Лимит попыток.** :data:`MAX_ATTEMPTS` неверных вводов - запрос сгорает.
  Иначе пароль можно подбирать перебором прямо в чате.
* **Привязка к пользователю и чату.** Подтвердить может только тот, кто
  запросил действие, и только в том же чате.

Модуль не знает ни про Telegram, ни про ядро: он хранит намерения и проверяет
пароль. Отправку сообщений и выполнение действия делает вызывающий код.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

DEFAULT_TTL = 120
"""Сколько секунд действует запрос подтверждения."""

MAX_ATTEMPTS = 3
"""Сколько неверных вводов пароля допускается до сгорания запроса."""


class CriticalAction(StrEnum):
    """
    Действия, требующие повторного ввода пароля.

    Список закрытый: он определяет, что именно считается критичным, и его
    видно целиком в одном месте.
    """

    CHANGE_GOLDEN_KEY = "change_golden_key"
    """Смена токена аккаунта FunPay."""

    RESTORE_BACKUP = "restore_backup"
    """Восстановление из присланного архива: перезапишет конфиги и товары."""

    POWER_OFF = "power_off"
    """Полное выключение бота."""

    UPLOAD_PLUGIN = "upload_plugin"
    """Установка плагина: исполняемый код с доступом к секретам."""

    CHANGE_AUTHORIZED_USERS = "change_authorized_users"
    """Изменение списка тех, кому разрешено управлять ботом."""


class VerdictReason(StrEnum):
    """Причина отказа при проверке подтверждения."""

    NO_PENDING = "no_pending"
    """Нет запроса, ожидающего подтверждения."""

    EXPIRED = "expired"
    """Срок запроса истёк."""

    WRONG_PASSWORD = "wrong_password"
    """Пароль не совпал, попытки ещё остались."""

    TOO_MANY_ATTEMPTS = "too_many_attempts"
    """Попытки исчерпаны, запрос сгорел."""


@dataclass
class PendingConfirmation:
    """Запрос подтверждения, ожидающий ввода пароля."""

    action: CriticalAction
    """Что именно подтверждается."""

    user_id: int
    """Telegram ID пользователя, запросившего действие."""

    chat_id: int
    """ID чата, в котором запрошено действие."""

    created_at: float
    """Момент создания запроса (time.time())."""

    payload: dict[str, Any] = field(default_factory=dict)
    """Данные, нужные для выполнения действия после подтверждения."""

    attempts: int = 0
    """Сколько неверных вводов пароля уже сделано."""

    def is_expired(self, ttl: int = DEFAULT_TTL, now: float | None = None) -> bool:
        """
        Истёк ли срок запроса.

        :param ttl: срок жизни в секундах.
        :param now: текущее время, для тестов.

        :return: True, если запрос больше не действителен.
        """
        return (now if now is not None else time.time()) - self.created_at > ttl

    def seconds_left(self, ttl: int = DEFAULT_TTL, now: float | None = None) -> int:
        """
        Сколько секунд осталось до истечения запроса.

        :param ttl: срок жизни в секундах.
        :param now: текущее время, для тестов.

        :return: остаток в секундах, не меньше нуля.
        """
        elapsed = (now if now is not None else time.time()) - self.created_at
        return max(0, int(ttl - elapsed))


@dataclass
class Verdict:
    """Результат проверки введённого пароля."""

    confirmed: bool
    """Действие подтверждено и может быть выполнено."""

    reason: VerdictReason | None = None
    """Причина отказа, если не подтверждено."""

    pending: PendingConfirmation | None = None
    """Запрос, к которому относится вердикт."""

    attempts_left: int = 0
    """Сколько попыток ввода пароля осталось."""


class ConfirmationManager:
    """
    Хранилище запросов подтверждения.

    Один экземпляр на бота. Запросы держатся только в памяти: после
    перезапуска все незавершённые подтверждения пропадают, и это правильно -
    подтверждать намерение, о котором владелец уже забыл, не нужно.
    """

    def __init__(self, ttl: int = DEFAULT_TTL, max_attempts: int = MAX_ATTEMPTS) -> None:
        """
        :param ttl: срок жизни запроса в секундах.
        :param max_attempts: допустимое число неверных вводов пароля.
        """
        self.ttl = ttl
        self.max_attempts = max_attempts
        self._pending: dict[int, PendingConfirmation] = {}

    def require(self, action: CriticalAction, user_id: int, chat_id: int,
                payload: dict[str, Any] | None = None,
                now: float | None = None) -> PendingConfirmation:
        """
        Регистрирует запрос подтверждения.

        Предыдущий незавершённый запрос того же пользователя заменяется:
        одновременно подтверждать два критичных действия не нужно, а старый
        запрос иначе остался бы висеть.

        :param action: подтверждаемое действие.
        :param user_id: Telegram ID пользователя.
        :param chat_id: ID чата.
        :param payload: данные для выполнения действия.
        :param now: текущее время, для тестов.

        :return: созданный запрос.
        """
        pending = PendingConfirmation(
            action=action,
            user_id=user_id,
            chat_id=chat_id,
            created_at=now if now is not None else time.time(),
            payload=dict(payload or {}),
        )
        self._pending[user_id] = pending
        return pending

    def get(self, user_id: int, now: float | None = None) -> PendingConfirmation | None:
        """
        Возвращает актуальный запрос пользователя.

        Истёкший запрос удаляется и не возвращается.

        :param user_id: Telegram ID пользователя.
        :param now: текущее время, для тестов.

        :return: запрос или None.
        """
        pending = self._pending.get(user_id)
        if pending is None:
            return None
        if pending.is_expired(self.ttl, now):
            del self._pending[user_id]
            return None
        return pending

    def cancel(self, user_id: int) -> None:
        """
        Отменяет запрос пользователя.

        :param user_id: Telegram ID пользователя.
        """
        self._pending.pop(user_id, None)

    def verify(self, user_id: int, chat_id: int, password: str,
               check_password: Callable[[str, str], bool], password_hash: str,
               now: float | None = None) -> Verdict:
        """
        Проверяет введённый пароль и решает, выполнять ли действие.

        Проверка пароля передаётся функцией, а не выполняется здесь: модуль
        не должен зависеть от bcrypt и от того, где лежит хеш.

        :param user_id: Telegram ID пользователя.
        :param chat_id: ID чата, в котором введён пароль.
        :param password: введённый пароль.
        :param check_password: функция сверки ``(пароль, хеш) -> bool``.
        :param password_hash: хеш пароля панели управления.
        :param now: текущее время, для тестов.

        :return: вердикт. При ``confirmed=True`` запрос уже удалён из хранилища,
            поэтому повторно подтвердить то же действие нельзя.
        """
        pending = self._pending.get(user_id)
        if pending is None:
            return Verdict(False, VerdictReason.NO_PENDING)

        if pending.is_expired(self.ttl, now):
            del self._pending[user_id]
            return Verdict(False, VerdictReason.EXPIRED, pending)

        # Подтверждать можно только в том чате, где действие запрошено.
        if pending.chat_id != chat_id:
            return Verdict(False, VerdictReason.NO_PENDING)

        try:
            password_ok = bool(password_hash) and check_password(password, password_hash)
        except Exception:
            # Битый хеш в конфиге не должен пропускать действие.
            password_ok = False

        if password_ok:
            del self._pending[user_id]
            return Verdict(True, None, pending)

        pending.attempts += 1
        if pending.attempts >= self.max_attempts:
            del self._pending[user_id]
            return Verdict(False, VerdictReason.TOO_MANY_ATTEMPTS, pending, 0)

        return Verdict(False, VerdictReason.WRONG_PASSWORD, pending,
                       self.max_attempts - pending.attempts)

    def purge_expired(self, now: float | None = None) -> int:
        """
        Удаляет истёкшие запросы.

        :param now: текущее время, для тестов.

        :return: сколько запросов удалено.
        """
        expired = [user_id for user_id, pending in self._pending.items()
                   if pending.is_expired(self.ttl, now)]
        for user_id in expired:
            del self._pending[user_id]
        return len(expired)

    @property
    def pending_count(self) -> int:
        """Сколько запросов сейчас ожидает подтверждения."""
        return len(self._pending)
