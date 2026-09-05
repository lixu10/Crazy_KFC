@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

rem ===== locate Python =====
set "PYCMD=python"
where py >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"

rem ===== create venv on first run =====
if exist ".venv\Scripts\python.exe" goto start_app

echo [First run] Creating virtual environment...
%PYCMD% -m venv .venv
if errorlevel 1 goto fail

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo [OK] Dependencies installed.

:start_app
echo Starting KFC College course helper...
".venv\Scripts\python.exe" main.py
set "rc=%errorlevel%"
if not "%rc%"=="0" goto fail
goto end

:fail
echo.
echo [Error] Startup failed. Check the message above.
echo If this is your first run, make sure Python 3.9+ is installed and on PATH.
pause
goto end

:end
endlocal
