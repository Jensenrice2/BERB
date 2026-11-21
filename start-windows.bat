@echo off
rem B.E.R.B for Windows: double-click this ONCE. It sets everything up, puts a B.E.R.B icon
rem on your Desktop and Start menu, and opens the app. After that, just use the icon.
rem (Double-click it again any time to repair or update the setup.)
setlocal
cd /d "%~dp0"
title B.E.R.B setup

rem --- find Python (the "py" launcher, or python on PATH; skip the Microsoft Store placeholder)
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY goto :nopython

if exist ".venv\Scripts\python.exe" goto :install
echo.
echo  Setting up B.E.R.B. The first time takes a minute or two...
echo.
%PY% -m venv .venv
if errorlevel 1 goto :fail

:install
echo  Checking that everything B.E.R.B needs is installed...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :fail
if defined TASKRUNNER_SETUP_ONLY exit /b 0

echo  Adding the B.E.R.B icon to your Desktop and Start menu...
".venv\Scripts\python.exe" -m taskrunner.cli shortcuts >nul
if errorlevel 1 echo  (Couldn't make the icons. You can still start B.E.R.B with this file.)

echo  Opening B.E.R.B...
".venv\Scripts\python.exe" -m taskrunner.cli open
if errorlevel 1 goto :fail
echo.
echo  All set! From now on, open B.E.R.B from its icon on your Desktop or in the Start menu.
echo  It runs quietly in the background; use "Turn off" in the app to stop it.
echo  This window closes by itself.
timeout /t 8 >nul
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
echo  Something went wrong. Take a screenshot of this window and send it to Claude.
echo  (Details may also be in the file data\server.log.)
pause
exit /b 1
