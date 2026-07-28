"""
Клиент к OpenAI-совместимому API.

Используется плагинами, которым нужна оценка текста моделью - например,
разбор переписки по заказу на «покупатель забыл подтвердить» и «есть спор».

Настройки хранятся в ``storage/cache/llm.json`` (каталог в git не попадает).
Ключ можно задать переменной окружения ``FOURFP_LLM_API_KEY`` - она имеет
приоритет над файлом, как и остальные секреты проекта.

Модуль намеренно тонкий: без внешних SDK, на ``requests``, который и так
в зависимостях. Совместим с любым сервисом, отдающим ``/chat/completions``
в формате OpenAI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any

import requests

logger = getLogger("4FP.llm")

SETTINGS_FILE = "storage/cache/llm.json"
API_KEY_ENV = "FOURFP_LLM_API_KEY"

DEFAULT_BASE_URL = "http://195.208.3.238:4500/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 90


class LLMError(RuntimeError):
    """Запрос к модели не удался."""


@dataclass
class LLMConfig:
    """Настройки подключения к API модели."""

    base_url: str = DEFAULT_BASE_URL
    """Базовый URL, включая ``/v1``."""

    model: str = DEFAULT_MODEL
    """Идентификатор модели. Список доступных отдаёт ``GET /v1/models``."""

    api_key: str = ""
    """Ключ. Переменная окружения важнее значения из файла."""

    timeout: int = DEFAULT_TIMEOUT
    """Таймаут запроса в секундах."""

    def resolved_key(self) -> str:
        """
        Возвращает актуальный ключ.

        :return: ключ из окружения либо из файла настроек.
        """
        return os.getenv(API_KEY_ENV, "").strip() or self.api_key

    @property
    def is_ready(self) -> bool:
        """Достаточно ли настроек, чтобы отправить запрос."""
        return bool(self.base_url and self.model and self.resolved_key())


def load_config() -> LLMConfig:
    """
    Загружает настройки клиента.

    :return: настройки; при отсутствии файла - значения по умолчанию.
    """
    if not os.path.exists(SETTINGS_FILE):
        return LLMConfig()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return LLMConfig(
            base_url=str(data.get("base_url", DEFAULT_BASE_URL)).rstrip("/"),
            model=str(data.get("model", DEFAULT_MODEL)),
            api_key=str(data.get("api_key", "")),
            timeout=int(data.get("timeout", DEFAULT_TIMEOUT)),
        )
    except (OSError, ValueError, TypeError):
        logger.debug("Настройки LLM повреждены, беру значения по умолчанию", exc_info=True)
        return LLMConfig()


def save_config(config: LLMConfig) -> None:
    """
    Сохраняет настройки клиента.

    :param config: настройки.
    """
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as file:
            json.dump({
                "base_url": config.base_url,
                "model": config.model,
                "api_key": config.api_key,
                "timeout": config.timeout,
            }, file, ensure_ascii=False, indent=2)
        os.chmod(SETTINGS_FILE, 0o600)
    except OSError:
        logger.debug("Не удалось сохранить настройки LLM", exc_info=True)


def mask_key(key: str) -> str:
    """
    Готовит ключ к показу в интерфейсе и логах.

    :param key: ключ.

    :return: строка вида ``sk_live_88...a0e`` или ``<не задан>``.
    """
    if not key:
        return "<не задан>"
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:10]}...{key[-3:]}"


class LLMClient:
    """Минимальный клиент к OpenAI-совместимому API."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        """
        :param config: настройки; при отсутствии загружаются из файла.
        """
        self.config = config or load_config()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.resolved_key()}",
            "Content-Type": "application/json",
        }

    def list_models(self) -> list[str]:
        """
        Запрашивает список доступных моделей.

        :return: список идентификаторов моделей.

        :raises LLMError: запрос не удался.
        """
        url = f"{self.config.base_url.rstrip('/')}/models"
        try:
            response = requests.get(url, headers=self._headers(), timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise LLMError(f"нет связи с {url}: {exc}") from exc

        if response.status_code != 200:
            raise LLMError(f"{url} ответил {response.status_code}: {response.text[:200]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMError("ответ не является JSON") from exc

        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise LLMError("в ответе нет списка моделей")
        return [str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")]

    def complete(self, system_prompt: str, user_prompt: str,
                 max_tokens: int = 2000, temperature: float = 0.0) -> str:
        """
        Отправляет запрос к модели и возвращает текст ответа.

        Температура по умолчанию нулевая: задача классификации требует
        воспроизводимости, а не разнообразия.

        :param system_prompt: системная инструкция.
        :param user_prompt: пользовательский запрос.
        :param max_tokens: ограничение на длину ответа.
        :param temperature: температура генерации.

        :return: текст ответа модели.

        :raises LLMError: запрос не удался либо ответ не разобран.
        """
        if not self.config.is_ready:
            raise LLMError("клиент не настроен: проверь base_url, модель и ключ")

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        try:
            response = requests.post(url, headers=self._headers(),
                                     json=payload, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise LLMError(f"нет связи с {url}: {exc}") from exc

        if response.status_code != 200:
            raise LLMError(f"модель ответила {response.status_code}: {response.text[:300]}")

        try:
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"не удалось разобрать ответ: {response.text[:300]}") from exc


def extract_json(text: str) -> Any:
    """
    Достаёт JSON из ответа модели.

    Модели часто оборачивают JSON в ```-блок или добавляют пояснение до и после,
    поэтому берётся содержимое от первой открывающей скобки до последней
    закрывающей.

    :param text: ответ модели.

    :return: разобранный объект.

    :raises ValueError: JSON не найден или не разобран.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Убираем ограждение вида ```json ... ```
        cleaned = cleaned.split("```", 2)
        cleaned = cleaned[1] if len(cleaned) > 1 else text
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]

    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except ValueError:
                continue
    raise ValueError(f"в ответе нет разбираемого JSON: {text[:200]}")
