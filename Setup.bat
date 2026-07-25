@echo off
chcp 65001 >nul
title 4FunPay - установка зависимостей

echo.
echo  4FunPay - установка зависимостей
echo  ================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo  [!] Python не найден в PATH.
    echo.
    echo      Установи Python 3.11 или новее с python.org и при установке
    echo      обязательно отметь галочку "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo  Найден Python %PYVER%
echo.

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo  [!] Нужен Python 3.11 или новее, установлен %PYVER%.
    echo.
    pause
    exit /b 1
)

echo  Создаю виртуальное окружение venv...
if not exist venv\Scripts\python.exe (
    python -m venv venv
    if errorlevel 1 (
        echo  [!] Не удалось создать venv.
        pause
        exit /b 1
    )
)

echo  Обновляю pip...
venv\Scripts\python.exe -m pip install --upgrade pip --quiet

echo  Устанавливаю зависимости...
venv\Scripts\python.exe -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo.
    echo  [!] Не удалось установить зависимости. Смотри вывод pip выше.
    pause
    exit /b 1
)

echo.
echo  Готово. Теперь запусти Start.bat
echo.
pause
