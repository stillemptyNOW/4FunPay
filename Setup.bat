@echo off
setlocal
title 4FunPay - Setup

echo.
echo  ====================================
echo   4FunPay - installing dependencies
echo  ====================================
echo.

where python >nul 2>&1
if errorlevel 1 goto no_python

python tools\check_python.py
if errorlevel 1 goto bad_version

echo.
echo  [1/3] Preparing virtual environment ...
set VENV_PY=
if exist venv\Scripts\python.exe set VENV_PY=venv\Scripts\python.exe
if exist .venv\Scripts\python.exe set VENV_PY=.venv\Scripts\python.exe
if not "%VENV_PY%"=="" goto venv_ready
python -m venv .venv
if errorlevel 1 goto venv_failed
set VENV_PY=.venv\Scripts\python.exe
echo        created .venv
goto venv_done
:venv_ready
echo        reusing existing environment: %VENV_PY%
:venv_done
echo.

echo  [2/3] Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip --quiet
echo        done.
echo.

echo  [3/3] Installing packages from requirements.txt ...
"%VENV_PY%" -m pip install --upgrade -r requirements.txt
if errorlevel 1 goto pip_failed
echo.
echo        done.
echo.

echo  ====================================
echo   Setup complete. Now run Start.bat
echo  ====================================
echo.
pause
exit /b 0

:no_python
echo  [ERROR] Python was not found in PATH.
echo.
echo    Install Python 3.11 or newer from https://www.python.org/downloads/
echo    During installation tick "Add python.exe to PATH".
echo.
pause
exit /b 1

:bad_version
echo.
echo  [ERROR] Python 3.11 or newer is required.
echo.
pause
exit /b 1

:venv_failed
echo.
echo  [ERROR] Could not create the virtual environment.
echo.
pause
exit /b 1

:pip_failed
echo.
echo  [ERROR] Could not install dependencies. See the pip output above.
echo          Most common cause: no internet connection or a proxy.
echo.
pause
exit /b 1
