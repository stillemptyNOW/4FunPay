"""Тесты резервного копирования."""

from __future__ import annotations

import zipfile
from pathlib import Path

from Utils import backup


def test_create_backup_includes_all_data_dirs(workdir: Path) -> None:
    (workdir / "configs/_main.cfg").write_text("[FunPay]\ngolden_key: x\n", encoding="utf-8")
    (workdir / "storage/products/keys.txt").write_text("key-1\nkey-2\n", encoding="utf-8")
    (workdir / "plugins/demo.py").write_text("NAME = 'demo'\n", encoding="utf-8")

    assert backup.create_backup() == 0

    with zipfile.ZipFile(workdir / backup.BACKUP_ARCHIVE) as archive:
        names = archive.namelist()

    assert any(name.endswith("_main.cfg") for name in names)
    assert any(name.endswith("keys.txt") for name in names)
    assert any(name.endswith("demo.py") for name in names)


def test_create_backup_skips_pycache(workdir: Path) -> None:
    """Кэш байткода в архиве бесполезен и только раздувает его."""
    cache_dir = workdir / "plugins/__pycache__"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "demo.cpython-312.pyc").write_bytes(b"\x00\x01")
    (workdir / "plugins/demo.py").write_text("NAME = 'demo'\n", encoding="utf-8")

    assert backup.create_backup() == 0

    with zipfile.ZipFile(workdir / backup.BACKUP_ARCHIVE) as archive:
        assert not any("__pycache__" in name for name in archive.namelist())


def test_create_backup_survives_missing_dirs(workdir: Path) -> None:
    """Отсутствие plugins/ не должно ронять создание бэкапа."""
    import shutil
    shutil.rmtree(workdir / "plugins")

    assert backup.create_backup() == 0
    assert (workdir / backup.BACKUP_ARCHIVE).exists()


def test_restore_cycle_returns_files(workdir: Path) -> None:
    """Полный круг: создать бэкап, потерять файл, восстановить из архива."""
    products = workdir / "storage/products/keys.txt"
    products.write_text("key-1\nkey-2\n", encoding="utf-8")

    assert backup.create_backup() == 0

    # Имитируем присланный в Telegram архив.
    (workdir / backup.UPLOADED_ARCHIVE).write_bytes(
        (workdir / backup.BACKUP_ARCHIVE).read_bytes())

    products.unlink()
    assert not products.exists()

    assert backup.extract_backup_archive() is True
    assert backup.install_backup() is True
    assert products.read_text(encoding="utf-8").strip() == "key-1\nkey-2"


def test_extract_reports_failure_on_broken_archive(workdir: Path) -> None:
    """Присланный вместо архива мусор не должен приводить к исключению."""
    (workdir / backup.UPLOADED_ARCHIVE).write_bytes(b"not a zip archive")
    assert backup.extract_backup_archive() is False


def test_install_backup_reports_failure_without_extracted_data(workdir: Path) -> None:
    import shutil
    shutil.rmtree(workdir / backup.EXTRACT_DIR, ignore_errors=True)
    assert backup.install_backup() is False
