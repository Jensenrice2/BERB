@echo off
rem Double-click this file to start Task Runner on Windows.
rem First time: it installs what the app needs (takes a minute or two).
setlocal
cd /d "%~dp0"
title Task Runner

rem --- find Python (the "py" launcher, or python on PATH; skip the Microsoft Store placeholder)
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY goto :nopython

rem --- first run: create the app's private Python environment
if exist ".venv\Scripts\python.exe" goto :install
echo.
echo  First-time setup: installing what Task Runner needs. This takes a minute or two...
echo.
%PY% -m venv .venv
if errorlevel 1 goto :fail

:install
rem --- every run: install anything new the app needs (quick when nothing changed)
echo  Checking that everything Task Runner needs is installed...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :fail

rem --- optional: load the Anthropic API key (for AI agent tasks) from a file named .env
if exist ".env" for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do set "%%A=%%B"
if defined TASKRUNNER_SETUP_ONLY exit /b 0

echo.
echo  Starting Task Runner. Your browser will open in a moment.
echo  KEEP THIS WINDOW OPEN while you use the app. Close it to stop the app.
echo.
".venv\Scripts\python.exe" -m taskrunner.cli serve --open
echo.
echo  Task Runner has stopped.
pause
exit /b 0

:nopython
echo.
echo  Python is not installed. Trying to install it for you...
echo.
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo.
  echo  Could not install Python automatically.
  echo  Your browser will open the Python download page. Install it, and on the first
  echo  screen TICK "Add python.exe to PATH". Then double-click start-windows.bat again.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)
echo.
echo  Python is installed. Close this window, then double-click start-windows.bat again.
pause
exit /b 0

:fail
echo.
echo  Setup failed. Take a screenshot of this window and send it to Claude.
echo  (Fix the problem, delete the ".venv" folder, and double-click start-windows.bat again.)
pause
exit /b 1
