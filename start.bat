@echo off
REM ===========================================================================
REM  AAE4203 GNSS Benchmark Challenge - Windows launcher
REM  Double-click this file. Requires Python 3.11 or newer on PATH.
REM ===========================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo.
    echo   Python was not found on PATH.
    echo   Install Python 3.11 or newer from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during the installation.
    echo.
    pause
    exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo.
    echo   Python 3.11 or newer is required - this is:
    %PY% -c "import sys; print(sys.version)"
    echo.
    pause
    exit /b 1
)

if not exist "config.toml" (
    echo   config.toml is missing.
    pause
    exit /b 1
)

echo.
echo   Starting the leaderboard server ...
echo.
%PY% run.py %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo   The server stopped with code %RC%.
)
pause
endlocal
