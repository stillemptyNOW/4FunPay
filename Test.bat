@echo off
setlocal
title 4FunPay - Tests

set VENV_PY=
if exist venv\Scripts\python.exe set VENV_PY=venv\Scripts\python.exe
if exist .venv\Scripts\python.exe set VENV_PY=.venv\Scripts\python.exe
if "%VENV_PY%"=="" goto no_venv

echo.
echo  Running test suite ...
echo.
"%VENV_PY%" -m pytest
echo.
pause
exit /b 0

:no_venv
echo.
echo  [ERROR] Virtual environment not found.
echo          Run Setup.bat first.
echo.
pause
exit /b 1
