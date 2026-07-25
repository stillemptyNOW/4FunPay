@echo off
chcp 65001 >nul
title 4FunPay

if not exist venv\Scripts\python.exe (
    echo.
    echo  [!] Виртуальное окружение не найдено. Сначала запусти Setup.bat
    echo.
    pause
    exit /b 1
)

:run
venv\Scripts\python.exe main.py

echo.
echo  Процесс завершился. Код выхода: %errorlevel%
echo  Нажми любую клавишу, чтобы запустить снова, или закрой окно.
pause >nul
goto run
