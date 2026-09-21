@echo off
rem ============================================================
rem  Local Ops Assistant - one-click health check
rem  (ASCII only in this file; the console is switched to UTF-8 so
rem   the Python helpers can print Chinese correctly)
rem
rem  Runs: service reachability + KB index stats + a live API key test
rem ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PORT=8765"

echo ============================================================
echo   Local Ops Assistant  /  doctor
echo ============================================================
echo.

rem ---------- locate a REAL python (skip the WindowsApps alias stub) ----------
set "PYEXE="
for /f "delims=" %%P in ('where python 2^>nul') do (
    echo %%P | findstr /I "WindowsApps" >nul || if not defined PYEXE set "PYEXE=%%P"
)
if not defined PYEXE (
    for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%P"
    )
)
if not defined PYEXE (
    echo [!] Python not found. Please install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

echo [i] Python : %PYEXE%
echo.

rem ---------- service + knowledge base ----------
"%PYEXE%" -X utf8 tools\health.py
echo.

rem ---------- live API key test ----------
"%PYEXE%" -X utf8 tools\check_api_key.py
echo.

echo ============================================================
pause
exit /b 0
