@echo off
setlocal
title 4FunPay

if not exist venv\Scripts\python.exe goto no_venv

:run
venv\Scripts\python.exe main.py
set EXITCODE=%errorlevel%

echo.
echo  ------------------------------------
echo   Process finished, exit code %EXITCODE%
echo  ------------------------------------
echo.
echo   Press any key to start again, or close this window.
pause >nul
goto run

:no_venv
echo.
echo  [ERROR] Virtual environment not found.
echo          Run Setup.bat first.
echo.
pause
exit /b 1
