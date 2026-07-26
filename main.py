"""
Точка входа 4FunPay.

Порядок работы:
    1. Проверка и досборка зависимостей, которые могли не установиться.
    2. Переход в каталог проекта и создание рабочих каталогов.
    3. Инициализация логгера и вывод баннера.
    4. Первичная настройка, если основной конфиг отсутствует.
    5. Загрузка и валидация конфигов.
    6. Запуск ядра.

Проект основан на открытом коде FunPayCardinal (Woopertail, sidor0912);
подробности в NOTICE.md.
"""

from __future__ import annotations

import logging.config
import os
import subprocess
import sys
import time

# --- Досборка зависимостей -------------------------------------------------
# Некоторые пакеты добавлялись в requirements.txt позже, чем пользователи
# развернули проект, поэтому их наличие проверяется отдельно. Установка идёт
# через отдельный процесс pip - обращаться к приватному API pip нельзя,
# он меняется между версиями.

_OPTIONAL_DEPENDENCIES: dict[str, str] = {
    "lxml": "lxml>=5.3.0",
    "bcrypt": "bcrypt>=4.2.0",
    "socks": "pysocks>=1.7.1",
}


def _ensure_dependencies() -> None:
    """Устанавливает отсутствующие зависимости из :data:`_OPTIONAL_DEPENDENCIES`."""
    import importlib.util

    missing = [spec for module, spec in _OPTIONAL_DEPENDENCIES.items()
               if importlib.util.find_spec(module) is None]
    if not missing:
        return
    print(f"Устанавливаю отсутствующие зависимости: {', '.join(missing)}")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-U", *missing],
                       check=False, timeout=600)
    except Exception as exc:
        print(f"Не удалось установить зависимости автоматически: {exc}\n"
              f"Установи их вручную: pip install -U -r requirements.txt")


_ensure_dependencies()

import colorama
from colorama import Fore, Style

import branding
import Utils.cardinal_tools
import Utils.config_loader as cfg_loader
import Utils.exceptions as excs
from cardinal import Cardinal
from first_setup import first_setup, SetupNotInteractiveError
from locales.localizer import Localizer
from Utils import secrets
from Utils.logger import LOGGER_CONFIG

# Переменные из .env подгружаются до чтения конфига, чтобы они могли
# перекрыть значения секретов. Уже заданные переменные окружения
# (systemd, docker compose) приоритетнее файла.
secrets.load_dotenv_file()

VERSION = branding.VERSION

BANNER = rf"""{Fore.CYAN}{Style.BRIGHT}
    ,--------------------------------------------.
    |                                            |
    |     /|   ____             ___              |
    |    / |  |  __|_ _ _ __   | _ \__ _ _  _    |
    |   /__|  | |_ | | | '_ \  |  _/ _` | || |   |
    |      |  |  _|| | | | | | | | | (_| | || |  |
    |      |  |_|   \_,_|_| |_| |_|  \__,_|\_, | |
    |                                      |___/ |
    |                                            |
    `--------------------------------------------'
{Style.RESET_ALL}"""

# --- Рабочий каталог и структура папок -------------------------------------

if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))
else:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

WORK_DIRS = ("configs", "logs", "storage", "storage/cache", "storage/plugins",
             "storage/products", "plugins")
for directory in WORK_DIRS:
    os.makedirs(directory, exist_ok=True)

WORK_FILES = ("configs/auto_delivery.cfg", "configs/auto_response.cfg")
for file_path in WORK_FILES:
    if not os.path.exists(file_path):
        with open(file_path, "w", encoding="utf-8"):
            pass

# --- Логгер и баннер -------------------------------------------------------

colorama.init()
Utils.cardinal_tools.set_console_title(f"{branding.CONSOLE_TITLE} v{VERSION}")

logging.config.dictConfig(LOGGER_CONFIG)
logging.raiseExceptions = False
logger = logging.getLogger("main")
logger.debug("------------------------------------------------------------------")

print(BANNER)
print(f"{Fore.CYAN}{Style.BRIGHT}{branding.BOT_NAME}{Style.RESET_ALL} "
      f"{Fore.WHITE}v{VERSION}{Style.RESET_ALL}\n")
print(f"{Fore.MAGENTA}{Style.BRIGHT} * Репозиторий:  {Fore.BLUE}{branding.REPO_URL}{Style.RESET_ALL}")
print(f"{Fore.MAGENTA}{Style.BRIGHT} * Владелец:     {Fore.BLUE}{branding.OWNER_TG}{Style.RESET_ALL}")
print(f"{Fore.MAGENTA}{Style.BRIGHT} * Поддержка:    {Fore.BLUE}{branding.SUPPORT_CHAT}{Style.RESET_ALL}\n")

# --- Первичная настройка ---------------------------------------------------

if not os.path.exists("configs/_main.cfg"):
    try:
        first_setup()
    except SetupNotInteractiveError:
        # Типичный случай: сервис включили до того, как прошли первичную настройку.
        logger.error("Конфиг configs/_main.cfg отсутствует, а запустить мастер настройки "
                     "не получается: программа работает без интерактивного ввода.")
        logger.error("Останови сервис и пройди настройку вручную:")
        logger.error(f"  sudo systemctl stop {branding.SERVICE_NAME}@$USER")
        logger.error(f"  cd ~/{branding.SERVICE_NAME} && ~/pyvenv/bin/python main.py")
        logger.error(f"  sudo systemctl start {branding.SERVICE_NAME}@$USER")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Настройка прервана. Конфиг не сохранён.")
        sys.exit(1)
    sys.exit()

# --- PID-файл при запуске как systemd-сервис -------------------------------

if sys.platform == "linux" and os.getenv(branding.RUNTIME_ENV_FLAG, "0") == "1":
    import getpass

    pid_dir = f"/run/{branding.SERVICE_NAME}/{getpass.getuser()}"
    try:
        os.makedirs(pid_dir, exist_ok=True)
        with open(f"{pid_dir}/{branding.SERVICE_NAME}.pid", "w") as pid_file:
            pid_file.write(str(os.getpid()))
        logger.info(f"$GREENPID-файл создан, PID процесса: {os.getpid()}")
    except OSError as exc:
        logger.warning(f"Не удалось записать PID-файл в {pid_dir}: {exc}")

# --- Загрузка конфигов -----------------------------------------------------

try:
    logger.info("$MAGENTAЗагружаю конфиг _main.cfg...")
    MAIN_CFG = cfg_loader.load_main_config("configs/_main.cfg")
    localizer = Localizer()
    _ = localizer.translate

    logger.info("$MAGENTAЗагружаю конфиг auto_response.cfg...")
    AR_CFG = cfg_loader.load_auto_response_config("configs/auto_response.cfg")
    RAW_AR_CFG = cfg_loader.load_raw_auto_response_config("configs/auto_response.cfg")

    logger.info("$MAGENTAЗагружаю конфиг auto_delivery.cfg...")
    AD_CFG = cfg_loader.load_auto_delivery_config("configs/auto_delivery.cfg")
except excs.ConfigParseError as e:
    logger.error(e)
    logger.error("Не могу продолжить с некорректным конфигом. Завершаю работу.")
    time.sleep(5)
    sys.exit(1)
except UnicodeDecodeError:
    logger.error("Не удалось прочитать конфиг как UTF-8. Проверь, что кодировка файла - UTF-8, "
                 "а перевод строки - LF (не CRLF).")
    logger.error("Завершаю работу.")
    time.sleep(5)
    sys.exit(1)
except Exception:
    logger.critical("Непредвиденная ошибка при загрузке конфигов.")
    logger.warning("TRACEBACK", exc_info=True)
    logger.error("Завершаю работу.")
    time.sleep(5)
    sys.exit(1)

localizer = Localizer()

# --- Проверка секретов -----------------------------------------------------
# Формат конфига уже проверен выше. Здесь проверяется, что golden_key и токен
# Telegram реально заданы - в конфиге или в переменных окружения. Без этого
# бот падал бы позже и менее внятно: на первом запросе к FunPay.

if problems := secrets.check_startup_secrets(MAIN_CFG):
    logger.error("Не могу запуститься, конфигурация неполная:")
    for number, problem in enumerate(problems, 1):
        logger.error(f"  {number}. {problem}")
    logger.error("Исправь перечисленное и запусти снова.")
    time.sleep(5)
    sys.exit(1)

logger.info(f"$CYANgolden_key: {secrets.mask(secrets.golden_key(MAIN_CFG))}")

# --- Запуск ядра -----------------------------------------------------------

try:
    Cardinal(MAIN_CFG, AD_CFG, AR_CFG, RAW_AR_CFG, VERSION).init().run()
except KeyboardInterrupt:
    logger.info("Получен Ctrl+C, завершаю работу.")
    sys.exit(0)
except Exception:
    logger.critical(f"В работе {branding.BOT_NAME} произошла необработанная ошибка.")
    logger.warning("TRACEBACK", exc_info=True)
    logger.critical("Завершаю работу.")
    time.sleep(5)
    sys.exit(1)
