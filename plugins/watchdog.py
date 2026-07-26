"""
Watchdog: контроль живости и оповещение о перезапусках.

Решает две задачи, которые systemd сам не закрывает.

1. **Оповещение о перезапуске.** ``Restart=on-failure`` поднимет упавший
   процесс, но владелец об этом не узнает. Плагин при старте сравнивает время
   с предыдущей отметкой и, если прошлый запуск завершился не по команде
   ``/restart``, присылает в Telegram уведомление с длительностью простоя.
   Пять перезапусков за час - повод посмотреть логи, а не считать, что всё хорошо.

2. **Heartbeat.** Раз в минуту обновляет ``storage/cache/heartbeat``. Файл
   читает HEALTHCHECK в Dockerfile: процесс может быть жив, а поллинг событий
   при этом стоять - тогда контейнер выглядит рабочим, но бот ничего не делает.
   Отметка обновляется в том же потоке, что и цикл событий, поэтому её
   устаревание означает реальную остановку работы.

Команды Telegram:
    /health - состояние: аптайм, последний перезапуск, их количество за сутки
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from logging import getLogger
from threading import Thread
from typing import TYPE_CHECKING

from telebot.types import Message

from Utils import cardinal_tools

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "Watchdog"
VERSION = "1.0.0"
DESCRIPTION = ("Оповещает в Telegram о незапланированных перезапусках бота и ведёт "
               "heartbeat-файл для healthcheck. Помогает заметить, что бот падает "
               "по кругу, вместо того чтобы считать его работающим.\n\n"
               "Команда: /health")
CREDITS = "{{OWNER_TG}}"
UUID = "b3e77c14-8a92-4f6b-9c05-1e4d7a2f8b63"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

logger = getLogger(f"4FP.{__name__}")

STATE_FILE = "storage/cache/watchdog_state.json"
HEARTBEAT_FILE = "storage/cache/heartbeat"
HEARTBEAT_INTERVAL = 60
"""Как часто обновляется отметка живости, секунды."""

GRACEFUL_SHUTDOWN_MARKER = "storage/cache/graceful_shutdown"
"""
Файл-признак штатной остановки.

Создаётся при перезапуске через /restart и при выключении через /power_off.
Если при следующем старте файла нет - значит процесс завершился аварийно.
"""

RESTART_WINDOW = 86400
"""Окно, за которое считаются перезапуски, секунды."""


@dataclass
class WatchdogState:
    """Состояние watchdog между запусками бота."""

    last_seen: float = 0.0
    """Время последнего heartbeat предыдущего запуска."""

    restarts: list[float] = field(default_factory=list)
    """Времена аварийных перезапусков."""

    def recent_restarts(self, window: int = RESTART_WINDOW) -> list[float]:
        """
        Отбирает перезапуски за последнее окно времени.

        :param window: длина окна в секундах.

        :return: список временных меток.
        """
        threshold = time.time() - window
        return [moment for moment in self.restarts if moment >= threshold]


def load_state() -> WatchdogState:
    """
    Читает состояние из кэша.

    :return: состояние; при отсутствии или порче файла - пустое.
    """
    if not os.path.exists(STATE_FILE):
        return WatchdogState()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return WatchdogState(last_seen=float(data.get("last_seen", 0)),
                             restarts=[float(x) for x in data.get("restarts", [])])
    except (OSError, ValueError, TypeError):
        logger.debug("Файл состояния watchdog повреждён, начинаю с чистого", exc_info=True)
        return WatchdogState()


def save_state(state: WatchdogState) -> None:
    """
    Сохраняет состояние в кэш.

    :param state: состояние для записи.
    """
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(asdict(state), file)
    except OSError:
        logger.debug("Не удалось сохранить состояние watchdog", exc_info=True)


def touch_heartbeat() -> None:
    """Обновляет время модификации heartbeat-файла."""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as file:
            file.write(str(int(time.time())))
    except OSError:
        logger.debug("Не удалось обновить heartbeat", exc_info=True)


def mark_graceful_shutdown() -> None:
    """
    Помечает остановку как штатную.

    Вызывается из ``/restart`` и ``/power_off`` через monkey-patch в :func:`init`.
    """
    try:
        os.makedirs(os.path.dirname(GRACEFUL_SHUTDOWN_MARKER), exist_ok=True)
        with open(GRACEFUL_SHUTDOWN_MARKER, "w", encoding="utf-8") as file:
            file.write(str(int(time.time())))
    except OSError:
        logger.debug("Не удалось поставить метку штатной остановки", exc_info=True)


def _consume_graceful_marker() -> bool:
    """
    Проверяет и снимает метку штатной остановки.

    :return: True, если предыдущее завершение было штатным.
    """
    if not os.path.exists(GRACEFUL_SHUTDOWN_MARKER):
        return False
    try:
        os.remove(GRACEFUL_SHUTDOWN_MARKER)
    except OSError:
        pass
    return True


def _heartbeat_loop() -> None:
    """
    Бесконечный цикл обновления отметки живости.

    Отметка нужна двум потребителям: HEALTHCHECK в Dockerfile и команде /health.
    Время последнего heartbeat также сохраняется в состояние - по нему при
    следующем запуске считается длительность простоя.
    """
    state = load_state()
    while True:
        touch_heartbeat()
        state.last_seen = time.time()
        save_state(state)
        time.sleep(HEARTBEAT_INTERVAL)


def _report_restart(cardinal: Cardinal, state: WatchdogState, downtime: float) -> None:
    """
    Сообщает в Telegram об аварийном перезапуске.

    :param cardinal: экземпляр ядра.
    :param state: состояние watchdog.
    :param downtime: длительность простоя в секундах.
    """
    if cardinal.telegram is None:
        return

    recent = state.recent_restarts()
    text = (f"♻️ <b>Бот перезапущен после аварийного завершения.</b>\n\n"
            f"⏱ Простой: <code>{cardinal_tools.time_to_str(int(downtime))}</code>\n"
            f"📊 Аварийных перезапусков за сутки: <code>{len(recent)}</code>")

    if len(recent) >= 5:
        text += ("\n\n⚠️ <b>Перезапусков слишком много.</b> Похоже, бот падает по кругу.\n"
                 "Посмотри логи: команда /logs или на сервере\n"
                 "<code>sudo journalctl -u 4funpay@$USER -n 200</code>")

    from tg_bot import utils as tg_utils
    Thread(target=cardinal.telegram.send_notification,
           args=(text,),
           kwargs={"notification_type": tg_utils.NotificationTypes.critical},
           daemon=True).start()


def _register_telegram(cardinal: Cardinal) -> None:
    """Регистрирует команду /health."""
    telegram = cardinal.telegram
    if telegram is None:
        return

    def health(message: Message) -> None:
        state = load_state()
        recent = state.recent_restarts()
        uptime = int(time.time() - cardinal.start_time)

        if os.path.exists(HEARTBEAT_FILE):
            age = int(time.time() - os.path.getmtime(HEARTBEAT_FILE))
            beat = f"<code>{age} сек назад</code>"
            if age > HEARTBEAT_INTERVAL * 3:
                beat += " ⚠️"
        else:
            beat = "<code>нет отметки</code> ⚠️"

        last_restart = "не было"
        if state.restarts:
            ago = int(time.time() - max(state.restarts))
            last_restart = f"{cardinal_tools.time_to_str(ago)} назад"

        telegram.bot.send_message(
            message.chat.id,
            f"🩺 <b>Состояние</b>\n\n"
            f"⏱ Аптайм: <code>{cardinal_tools.time_to_str(uptime)}</code>\n"
            f"💓 Heartbeat: {beat}\n"
            f"♻️ Последний аварийный перезапуск: <code>{last_restart}</code>\n"
            f"📊 Аварийных перезапусков за сутки: <code>{len(recent)}</code>\n"
            f"🔌 Поллинг событий FunPay: "
            f"<code>{'запущен' if cardinal.runner else 'ядро ещё инициализируется'}</code>")

    telegram.msg_handler(health, commands=["health"])
    cardinal.add_telegram_commands(UUID, [("health", "состояние бота и аптайм", True)])


def init(cardinal: Cardinal, *args) -> None:
    """
    Определяет причину предыдущей остановки и запускает heartbeat.

    :param cardinal: экземпляр ядра.
    """
    state = load_state()
    graceful = _consume_graceful_marker()

    # Аварийным считается завершение, при котором не было метки штатной остановки
    # и при этом от предыдущего запуска осталась отметка живости.
    if not graceful and state.last_seen:
        downtime = time.time() - state.last_seen
        state.restarts.append(time.time())
        # Храним только окно, иначе список растёт бесконечно.
        state.restarts = state.recent_restarts(RESTART_WINDOW * 7)
        save_state(state)
        logger.warning(f"$YELLOW[{NAME}]$RESET предыдущий запуск завершился аварийно, "
                       f"простой: {cardinal_tools.time_to_str(int(downtime))}")
        _report_restart(cardinal, state, downtime)
    elif graceful:
        logger.info(f"$MAGENTA[{NAME}]$RESET предыдущая остановка была штатной")

    # Штатные остановки помечаем, чтобы не считать их падениями.
    _patch_shutdown_handlers()
    _register_telegram(cardinal)

    touch_heartbeat()
    Thread(target=_heartbeat_loop, daemon=True).start()
    logger.info(f"$MAGENTA[{NAME}]$RESET heartbeat каждые {HEARTBEAT_INTERVAL} сек")


def _patch_shutdown_handlers() -> None:
    """
    Оборачивает функции перезапуска и выключения, чтобы отметить остановку штатной.

    Ядро не предоставляет хука на выход (``BIND_TO_POST_STOP`` не вызывается при
    ``/restart``, потому что процесс заменяется через ``os.execl``), поэтому
    единственный способ отличить штатный перезапуск от падения - обернуть
    сами функции остановки.
    """
    if getattr(cardinal_tools.restart_program, "_watchdog_patched", False):
        return

    original_restart = cardinal_tools.restart_program
    original_shutdown = cardinal_tools.shut_down

    def restart_program_with_marker(*args, **kwargs):
        mark_graceful_shutdown()
        return original_restart(*args, **kwargs)

    def shut_down_with_marker(*args, **kwargs):
        mark_graceful_shutdown()
        return original_shutdown(*args, **kwargs)

    restart_program_with_marker._watchdog_patched = True
    shut_down_with_marker._watchdog_patched = True
    cardinal_tools.restart_program = restart_program_with_marker
    cardinal_tools.shut_down = shut_down_with_marker


BIND_TO_PRE_INIT = [init]
