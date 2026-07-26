#!/usr/bin/env bash
#
# Установщик 4FunPay для Ubuntu 22.04 / 24.04 (и совместимых Debian-систем).
#
# Что делает:
#   1. Ставит системные пакеты и Python 3.11+ (из штатных репозиториев,
#      при необходимости - из deadsnakes PPA).
#   2. Готовит venv в ~/pyvenv и ставит зависимости.
#   3. Раскладывает код в ~/4funpay (клонирует репозиторий или использует текущий).
#   4. Регистрирует systemd-юнит 4funpay@<пользователь>.
#   5. Запускает мастер первичной настройки и включает автозапуск.
#
# Запуск:
#   bash install.sh
#
# Скрипт не требует прав root целиком - sudo вызывается только там, где нужно.
# Запускать от имени того пользователя, под которым будет работать бот.

set -euo pipefail

# --- Настройки --------------------------------------------------------------

readonly SERVICE_NAME="4funpay"
readonly APP_DIR="${HOME}/${SERVICE_NAME}"
readonly VENV_DIR="${HOME}/pyvenv"
readonly REPO_URL="https://github.com/stillemptyNOW/4FunPay.git"
readonly MIN_PY_MINOR=11

# --- Оформление вывода ------------------------------------------------------

if [[ -t 1 ]]; then
    readonly C_RED=$'\e[1;31m'
    readonly C_GREEN=$'\e[1;32m'
    readonly C_YELLOW=$'\e[1;33m'
    readonly C_CYAN=$'\e[1;36m'
    readonly C_OFF=$'\e[0m'
else
    readonly C_RED='' C_GREEN='' C_YELLOW='' C_CYAN='' C_OFF=''
fi

step()  { printf '\n%s==>%s %s\n' "${C_CYAN}" "${C_OFF}" "$1"; }
ok()    { printf '%s  ok%s %s\n' "${C_GREEN}" "${C_OFF}" "$1"; }
warn()  { printf '%s  !!%s %s\n' "${C_YELLOW}" "${C_OFF}" "$1"; }
die()   { printf '\n%sОШИБКА:%s %s\n' "${C_RED}" "${C_OFF}" "$1" >&2; exit 1; }

# --- Проверки окружения -----------------------------------------------------

step "Проверяю окружение"

[[ ${EUID} -ne 0 ]] || die "Не запускай установщик от root. Нужен обычный пользователь, под которым будет работать бот (см. README, раздел про создание пользователя)."

command -v sudo >/dev/null 2>&1 || die "Не найден sudo. Установи его: apt install sudo"

sudo -v >/dev/null 2>&1 || die "У пользователя ${USER} нет прав sudo."

if ! command -v systemctl >/dev/null 2>&1; then
    die "Не найден systemd. Этот установщик рассчитан на systemd-систему (Ubuntu/Debian). Для контейнеров используй Docker-вариант, см. README."
fi

ok "Пользователь: ${USER}, домашний каталог: ${HOME}"

# --- Системные пакеты -------------------------------------------------------

step "Обновляю список пакетов"
sudo apt-get update -qq || die "apt-get update не выполнился. Проверь сеть и /etc/apt/sources.list"
ok "список пакетов обновлён"

step "Ставлю базовые пакеты"
sudo apt-get install -y -qq git curl ca-certificates locales \
    || die "Не удалось установить базовые пакеты."
ok "git, curl, ca-certificates установлены"

# Локаль en_US.UTF-8 нужна, потому что юнит запускается с LANG=en_US.utf8.
if ! locale -a 2>/dev/null | grep -qi '^en_US\.utf-\?8$'; then
    step "Генерирую локаль en_US.UTF-8"
    sudo locale-gen en_US.UTF-8 >/dev/null
    ok "локаль сгенерирована"
fi

# --- Python 3.11+ -----------------------------------------------------------

step "Ищу Python ${MIN_PY_MINOR}+"

find_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "${candidate}" >/dev/null 2>&1; then
            local minor
            minor="$("${candidate}" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)"
            local major
            major="$("${candidate}" -c 'import sys; print(sys.version_info.major)' 2>/dev/null || echo 0)"
            if [[ "${major}" -eq 3 && "${minor}" -ge ${MIN_PY_MINOR} ]]; then
                printf '%s' "${candidate}"
                return 0
            fi
        fi
    done
    return 1
}

install_python_from_apt() {
    local pkg
    for pkg in python3.12 python3.11; do
        if apt-cache show "${pkg}" >/dev/null 2>&1; then
            step "Ставлю ${pkg} из репозиториев дистрибутива"
            if sudo apt-get install -y -qq "${pkg}" "${pkg}-venv" "${pkg}-dev"; then
                ok "${pkg} установлен"
                return 0
            fi
            warn "не удалось установить ${pkg}, пробую следующий вариант"
        fi
    done
    return 1
}

install_python_from_deadsnakes() {
    step "Подключаю PPA deadsnakes (в репозиториях дистрибутива подходящего Python нет)"
    sudo apt-get install -y -qq software-properties-common \
        || die "Не удалось установить software-properties-common."
    sudo add-apt-repository -y ppa:deadsnakes/ppa >/dev/null \
        || die "Не удалось подключить ppa:deadsnakes/ppa. Установи Python ${MIN_PY_MINOR}+ вручную и запусти скрипт снова."
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3.12 python3.12-venv python3.12-dev \
        || die "Не удалось установить python3.12 из deadsnakes."
    ok "python3.12 установлен из deadsnakes"
}

if PYTHON_BIN="$(find_python)"; then
    ok "найден ${PYTHON_BIN} ($("${PYTHON_BIN}" --version 2>&1))"
else
    install_python_from_apt || install_python_from_deadsnakes
    PYTHON_BIN="$(find_python)" || die "Python ${MIN_PY_MINOR}+ так и не найден в PATH."
    ok "будет использован ${PYTHON_BIN} ($("${PYTHON_BIN}" --version 2>&1))"
fi

# Пакет venv может отсутствовать отдельно от интерпретатора.
if ! "${PYTHON_BIN}" -c 'import venv' >/dev/null 2>&1; then
    step "Ставлю модуль venv для ${PYTHON_BIN}"
    sudo apt-get install -y -qq "${PYTHON_BIN}-venv" \
        || die "Не удалось установить ${PYTHON_BIN}-venv."
    ok "venv доступен"
fi

# --- Код приложения ---------------------------------------------------------

step "Готовлю каталог приложения ${APP_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${APP_DIR}/main.py" ]]; then
    ok "код уже на месте, обновление кода не трогаю (для этого: git pull)"
elif [[ -f "${SCRIPT_DIR}/main.py" ]]; then
    if [[ "${SCRIPT_DIR}" == "${APP_DIR}" ]]; then
        ok "установщик запущен из каталога приложения"
    else
        step "Переношу код из ${SCRIPT_DIR} в ${APP_DIR}"
        mkdir -p "${APP_DIR}"
        # Копируем вместе с .git, чтобы дальше работал git pull.
        cp -a "${SCRIPT_DIR}/." "${APP_DIR}/"
        ok "код скопирован"
    fi
elif [[ "${REPO_URL}" == *"{{"* ]]; then
    die "REPO_URL в install.sh не заполнен, а рядом со скриптом нет main.py.
   Либо запусти install.sh из клонированного репозитория, либо впиши адрес
   своего репозитория в переменную REPO_URL в начале этого файла."
else
    step "Клонирую ${REPO_URL}"
    git clone "${REPO_URL}" "${APP_DIR}" || die "git clone не удался."
    ok "репозиторий склонирован"
fi

cd "${APP_DIR}"
[[ -f main.py ]] || die "В ${APP_DIR} нет main.py - код разложен неверно."

# --- Виртуальное окружение --------------------------------------------------

step "Готовлю виртуальное окружение ${VENV_DIR}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    "${PYTHON_BIN}" -m venv "${VENV_DIR}" || die "Не удалось создать venv в ${VENV_DIR}."
    ok "venv создан"
else
    ok "venv уже существует"
fi

"${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip setuptools wheel \
    || warn "не удалось обновить pip - продолжаю с текущей версией"

step "Устанавливаю зависимости из requirements.txt"
"${VENV_DIR}/bin/pip" install --quiet --upgrade -r requirements.txt \
    || die "Не удалось установить зависимости. Смотри вывод pip выше."
ok "зависимости установлены"

# --- systemd ----------------------------------------------------------------

step "Регистрирую systemd-юнит"

readonly UNIT_SRC="${APP_DIR}/${SERVICE_NAME}@.service"
readonly UNIT_DST="/etc/systemd/system/${SERVICE_NAME}@.service"

[[ -f "${UNIT_SRC}" ]] || die "Не найден файл юнита ${UNIT_SRC}."

# Юнит рассчитан на пути /home/<user>/4funpay и /home/<user>/pyvenv.
if [[ "${APP_DIR}" != "${HOME}/${SERVICE_NAME}" || "${VENV_DIR}" != "${HOME}/pyvenv" ]]; then
    warn "нестандартные пути установки - проверь WorkingDirectory и ExecStart в ${UNIT_DST}"
fi

sudo install -m 644 "${UNIT_SRC}" "${UNIT_DST}" || die "Не удалось скопировать юнит."
sudo systemctl daemon-reload
ok "юнит установлен: ${UNIT_DST}"

# --- Первичная настройка ----------------------------------------------------

readonly CONFIG_FILE="${APP_DIR}/configs/_main.cfg"

if [[ -f "${CONFIG_FILE}" ]]; then
    step "Конфиг уже существует, мастер настройки пропускаю"
    ok "${CONFIG_FILE}"
else
    step "Запускаю мастер первичной настройки"
    printf '%sПодготовь заранее:%s\n' "${C_YELLOW}" "${C_OFF}"
    printf '  1. golden_key - cookie с funpay.com (DevTools -> Application -> Cookies)\n'
    printf '  2. токен Telegram-бота от @BotFather\n'
    printf '  3. пароль, которым будешь входить в панель управления\n\n'

    # Мастер запускается интерактивно, поэтому stdin не перенаправляем.
    LANG=en_US.utf8 "${VENV_DIR}/bin/python" main.py || true

    [[ -f "${CONFIG_FILE}" ]] || die "Мастер настройки не создал ${CONFIG_FILE}. Запусти вручную: cd ${APP_DIR} && ${VENV_DIR}/bin/python main.py"
    ok "конфиг создан"
fi

# Конфиг содержит golden_key и токен - закрываем доступ остальным.
chmod 700 "${APP_DIR}/configs" 2>/dev/null || true
chmod 600 "${CONFIG_FILE}" 2>/dev/null || true

# --- Запуск -----------------------------------------------------------------

step "Включаю автозапуск и стартую сервис"
sudo systemctl enable --now "${SERVICE_NAME}@${USER}.service" \
    || die "Не удалось запустить сервис. Смотри: sudo journalctl -u ${SERVICE_NAME}@${USER} -n 50"

sleep 3
if sudo systemctl is-active --quiet "${SERVICE_NAME}@${USER}.service"; then
    ok "сервис запущен"
else
    warn "сервис не в состоянии active - смотри логи командой ниже"
fi

# --- Итог -------------------------------------------------------------------

cat <<EOF

${C_GREEN}Установка завершена.${C_OFF}

Каталог приложения:  ${APP_DIR}
Виртуальное окружение: ${VENV_DIR}
Сервис:              ${SERVICE_NAME}@${USER}

${C_CYAN}Основные команды${C_OFF}
  Логи в реальном времени:  sudo journalctl -u ${SERVICE_NAME}@${USER} -f
  Последние 100 строк:      sudo journalctl -u ${SERVICE_NAME}@${USER} -n 100
  Состояние:                sudo systemctl status ${SERVICE_NAME}@${USER}
  Перезапуск:               sudo systemctl restart ${SERVICE_NAME}@${USER}
  Остановка:                sudo systemctl stop ${SERVICE_NAME}@${USER}
  Убрать из автозапуска:    sudo systemctl disable ${SERVICE_NAME}@${USER}

${C_CYAN}Обновление${C_OFF}
  sudo systemctl stop ${SERVICE_NAME}@${USER}
  cd ${APP_DIR} && git pull
  ${VENV_DIR}/bin/pip install -U -r requirements.txt
  sudo systemctl start ${SERVICE_NAME}@${USER}

Дальше настраивай бота через Telegram: напиши ему пароль, затем /menu
EOF
