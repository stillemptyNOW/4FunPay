"""
Проверка версии интерпретатора для Setup.bat.

Вынесено из batch-файла намеренно: сравнение вида ``>= (3, 11)`` внутри
``python -c "..."`` ломает разбор batch, потому что ``>`` для cmd.exe -
оператор перенаправления вывода, и кавычки от этого не спасают.

Код возврата: 0 - версия подходит, 1 - слишком старая.
"""

import sys

REQUIRED = (3, 11)
"""Минимальная поддерживаемая версия Python."""


def main() -> int:
    """
    Сравнивает текущую версию Python с требуемой.

    :return: код возврата для Setup.bat.
    """
    if sys.version_info < REQUIRED:
        current = ".".join(str(part) for part in sys.version_info[:3])
        required = ".".join(str(part) for part in REQUIRED)
        print(f"  Python {current} is too old, need {required} or newer.")
        return 1

    print(f"  Python {sys.version.split()[0]} - ok.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
