"""Тесты подстановки бренда."""

from __future__ import annotations

import branding


def test_apply_substitutes_known_placeholders() -> None:
    result = branding.apply("Добро пожаловать в {{BOT_NAME}}!")
    assert result == f"Добро пожаловать в {branding.BOT_NAME}!"


def test_apply_substitutes_several_placeholders_at_once() -> None:
    result = branding.apply("{{BOT_NAME}} / {{BOT_SHORT_NAME}} / {{SERVICE_NAME}}")
    assert result == f"{branding.BOT_NAME} / {branding.BOT_SHORT_NAME} / {branding.SERVICE_NAME}"


def test_apply_leaves_text_without_placeholders_untouched() -> None:
    text = "Обычная строка без подстановок"
    assert branding.apply(text) is text


def test_apply_keeps_format_slots_intact() -> None:
    """
    Подстановка бренда идёт до str.format(), поэтому она не должна затрагивать
    слоты вида {} - иначе локализованные строки с аргументами сломаются.
    """
    result = branding.apply("{{BOT_NAME}} версии {} на аккаунте {}")
    assert result.count("{}") == 2
    assert result.format("1.0.0", "shop") == f"{branding.BOT_NAME} версии 1.0.0 на аккаунте shop"


def test_apply_ignores_unknown_placeholder() -> None:
    """Незнакомый плейсхолдер остаётся как есть - это видимый признак опечатки."""
    assert branding.apply("{{NO_SUCH_KEY}}") == "{{NO_SUCH_KEY}}"


def test_every_placeholder_in_map_is_substituted() -> None:
    """
    Каждый объявленный плейсхолдер подставляется своим значением.

    Проверяется именно факт подстановки, а не отсутствие фигурных скобок
    в результате: значения контактов по умолчанию сами являются
    незаполненными плейсхолдерами (``OWNER_TG = "@{{OWNER_TG}}"``), и это
    намеренно - так незаполненный бренд виден в интерфейсе.
    """
    for placeholder, value in branding._PLACEHOLDERS.items():
        assert branding.apply(placeholder) == value, placeholder


def test_unfilled_contacts_stay_visible() -> None:
    """
    Пока контакты не заполнены, они остаются заметными в интерфейсе.

    На это опирается tg_bot.keyboards.support_links(): кнопка со ссылкой
    не создаётся, если значение содержит "{{" - незаполненный плейсхолдер
    не является валидным URL и был бы отвергнут Telegram.
    """
    for value in (branding.OWNER_TG, branding.SUPPORT_CHAT, branding.REPO_URL):
        assert isinstance(value, str) and value


def test_service_name_is_filesystem_safe() -> None:
    """
    Имя сервиса попадает в systemd-юнит и в пути /run/<name>, поэтому
    в нём не должно быть пробелов и слэшей.
    """
    assert branding.SERVICE_NAME
    assert " " not in branding.SERVICE_NAME
    assert "/" not in branding.SERVICE_NAME
