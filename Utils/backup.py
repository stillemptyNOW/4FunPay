"""
Резервное копирование и восстановление данных бота.

В архив попадают каталоги ``storage``, ``configs`` и ``plugins`` - то есть всё,
что нельзя восстановить из репозитория: конфиги, товарные файлы, кэш, плагины.

Архив содержит golden_key и токен Telegram-бота, поэтому наружу его отдавать
нельзя - об этом предупреждает текст ``backup_ready`` в локализациях.

Модуль заменил ``Utils/updater.py`` из исходного проекта: механика самообновления
с чужого GitHub-репозитория убрана, функции бэкапа сохранены без изменений
логики. Обновление выполняется вручную через git - см. README.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from logging import getLogger

logger = getLogger("4FP.backup")

BACKUP_ARCHIVE = "backup.zip"
"""Путь до создаваемого архива с резервной копией."""

UPLOADED_ARCHIVE = "storage/cache/backup.zip"
"""Путь, куда file_uploader сохраняет присланный в Telegram архив."""

EXTRACT_DIR = "storage/cache/backup"
"""Каталог, в который распаковывается присланный архив перед установкой."""

BACKUP_DIRS = ("storage", "configs", "plugins")
"""Каталоги, попадающие в резервную копию."""


def zipdir(path: str, zip_obj: zipfile.ZipFile) -> None:
    """
    Рекурсивно добавляет каталог в открытый zip-архив, пропуская ``__pycache__``.

    :param path: путь до каталога.
    :param zip_obj: открытый объект архива.
    """
    for root, _dirs, files in os.walk(path):
        if os.path.basename(root) == "__pycache__":
            continue
        for file in files:
            full_path = os.path.join(root, file)
            zip_obj.write(full_path, os.path.relpath(full_path, os.path.join(path, "..")))


def create_backup() -> int:
    """
    Создаёт резервную копию каталогов из :data:`BACKUP_DIRS`.

    :return: 0 при успехе, 1 при ошибке (код возврата сохранён для совместимости
        с вызывающим кодом Telegram-ПУ).
    """
    try:
        with zipfile.ZipFile(BACKUP_ARCHIVE, "w") as archive:
            for directory in BACKUP_DIRS:
                if os.path.exists(directory):
                    zipdir(directory, archive)
        return 0
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
        return 1


def extract_backup_archive() -> bool:
    """
    Распаковывает присланный архив из :data:`UPLOADED_ARCHIVE` в :data:`EXTRACT_DIR`.

    :return: True, если распаковано успешно.
    """
    try:
        if os.path.exists(EXTRACT_DIR):
            shutil.rmtree(EXTRACT_DIR, ignore_errors=True)
        os.makedirs(EXTRACT_DIR)

        with zipfile.ZipFile(UPLOADED_ARCHIVE, "r") as archive:
            archive.extractall(EXTRACT_DIR)
        return True
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
        return False


def install_backup() -> bool:
    """
    Копирует содержимое распакованного бэкапа поверх рабочих каталогов.

    :return: True, если восстановление прошло успешно.
    """
    try:
        if not os.path.exists(EXTRACT_DIR):
            return False

        for entry in os.listdir(EXTRACT_DIR):
            source = os.path.join(EXTRACT_DIR, entry)
            if os.path.isfile(source):
                shutil.copy2(source, entry)
            else:
                shutil.copytree(source, os.path.join(".", entry), dirs_exist_ok=True)
        return True
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
        return False
