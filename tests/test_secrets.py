"""Тесты работы с секретами: приоритет окружения, маскирование, предстартовые проверки."""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

import pytest

from Utils import secrets

VALID_KEY = "0123456789abcdef0123456789abcdef"
VALID_TOKEN = "1234567890:AAbbCcDdEeFfGgHhIiJjKkLlMmNnOoPp"


def make_config(golden_key: str = VALID_KEY, token: str = VALID_TOKEN,
                telegram_enabled: str = "1", secret_hash: str = "$2b$12$stub") -> ConfigParser:
    """Собирает минимальный конфиг для проверок."""
    config = ConfigParser(delimiters=(":",), interpolation=None)
    config.optionxform = str
    config.read_dict({
        "FunPay": {"golden_key": golden_key},
        "Telegram": {"enabled": telegram_enabled, "token": token, "secretKeyHash": secret_hash},
    })
    return config


# --- Приоритет источников --------------------------------------------------

def test_golden_key_read_from_config(clean_env: None) -> None:
    assert secrets.golden_key(make_config()) == VALID_KEY


def test_env_overrides_config(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(secrets.GOLDEN_KEY_ENV, "f" * 32)
    assert secrets.golden_key(make_config()) == "f" * 32


def test_empty_env_does_not_override(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустая переменная не должна затирать рабочее значение из конфига."""
    monkeypatch.setenv(secrets.GOLDEN_KEY_ENV, "   ")
    assert secrets.golden_key(make_config()) == VALID_KEY


def test_missing_section_returns_empty_string(clean_env: None) -> None:
    config = ConfigParser()
    assert secrets.resolve(config, "FunPay", "golden_key", secrets.GOLDEN_KEY_ENV) == ""


def test_is_from_env_reflects_environment(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    assert secrets.is_from_env(secrets.GOLDEN_KEY_ENV) is False
    monkeypatch.setenv(secrets.GOLDEN_KEY_ENV, VALID_KEY)
    assert secrets.is_from_env(secrets.GOLDEN_KEY_ENV) is True


# --- Маскирование ----------------------------------------------------------

def test_mask_hides_middle_of_secret() -> None:
    masked = secrets.mask(VALID_KEY)
    assert masked == "0123...cdef"
    assert VALID_KEY not in masked


def test_mask_reports_absent_secret() -> None:
    assert secrets.mask("") == "<не задан>"


def test_mask_does_not_leak_short_secrets() -> None:
    """Короткое значение маскируется целиком: показывать нечего."""
    assert secrets.mask("abcd") == "****"
    assert secrets.mask("abcdefgh", visible=4) == "********"


# --- Предстартовые проверки -----------------------------------------------

def test_valid_config_has_no_problems(clean_env: None) -> None:
    assert secrets.check_startup_secrets(make_config()) == []


def test_missing_golden_key_is_reported(clean_env: None) -> None:
    problems = secrets.check_startup_secrets(make_config(golden_key=""))
    assert len(problems) == 1
    assert "golden_key" in problems[0]
    # Сообщение должно объяснять, что делать, а не просто констатировать проблему.
    assert secrets.GOLDEN_KEY_ENV in problems[0]
    assert "Cookies" in problems[0]


def test_golden_key_from_env_satisfies_check(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустое значение в конфиге допустимо, если ключ задан окружением."""
    monkeypatch.setenv(secrets.GOLDEN_KEY_ENV, VALID_KEY)
    assert secrets.check_startup_secrets(make_config(golden_key="")) == []


def test_wrong_golden_key_length_is_reported(clean_env: None) -> None:
    problems = secrets.check_startup_secrets(make_config(golden_key="tooshort"))
    assert len(problems) == 1
    assert "8" in problems[0] and str(secrets.GOLDEN_KEY_LENGTH) in problems[0]


def test_missing_token_reported_only_when_telegram_enabled(clean_env: None) -> None:
    disabled = secrets.check_startup_secrets(make_config(token="", telegram_enabled="0"))
    assert disabled == []

    enabled = secrets.check_startup_secrets(make_config(token="", telegram_enabled="1"))
    assert any("токен" in problem.lower() for problem in enabled)


def test_malformed_token_is_reported(clean_env: None) -> None:
    problems = secrets.check_startup_secrets(make_config(token="not-a-token"))
    assert any("формат" in problem.lower() for problem in problems)


def test_missing_password_hash_is_reported(clean_env: None) -> None:
    problems = secrets.check_startup_secrets(make_config(secret_hash=""))
    assert any("secretKeyHash" in problem for problem in problems)


def test_all_problems_reported_at_once(clean_env: None) -> None:
    """
    Проверки не должны прекращаться на первой ошибке: иначе пользователь
    исправляет по одному пункту за запуск.
    """
    problems = secrets.check_startup_secrets(
        make_config(golden_key="", token="", secret_hash=""))
    assert len(problems) == 3


# --- Чтение .env -----------------------------------------------------------

def test_dotenv_fallback_parses_file(workdir: Path, clean_env: None,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    (workdir / ".env").write_text(
        "# комментарий\n"
        f'{secrets.GOLDEN_KEY_ENV}="{VALID_KEY}"\n'
        "\n"
        f"{secrets.TELEGRAM_TOKEN_ENV}={VALID_TOKEN}\n",
        encoding="utf-8")

    secrets._load_dotenv_fallback(str(workdir / ".env"))

    import os
    assert os.environ[secrets.GOLDEN_KEY_ENV] == VALID_KEY
    assert os.environ[secrets.TELEGRAM_TOKEN_ENV] == VALID_TOKEN


def test_dotenv_does_not_override_existing_env(workdir: Path, clean_env: None,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Значение из systemd или docker compose важнее файла: иначе забытый .env
    на сервере молча подменял бы рабочий ключ.
    """
    monkeypatch.setenv(secrets.GOLDEN_KEY_ENV, "e" * 32)
    (workdir / ".env").write_text(f"{secrets.GOLDEN_KEY_ENV}={VALID_KEY}\n", encoding="utf-8")

    secrets._load_dotenv_fallback(str(workdir / ".env"))

    import os
    assert os.environ[secrets.GOLDEN_KEY_ENV] == "e" * 32


def test_load_dotenv_file_is_noop_when_absent(workdir: Path) -> None:
    secrets.load_dotenv_file(str(workdir / "nonexistent.env"))
