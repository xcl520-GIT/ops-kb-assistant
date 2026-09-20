@echo off
rem ============================================================
rem  Local Ops Assistant - launcher
rem  (ASCII only: safe regardless of console code page)
rem
rem  Behaviour:
rem    1. if the port is already listening  -> just open the browser
rem    2. otherwise start the server in the background, wait for the
rem       port, then open the browser
rem
rem  NOTE: this script does NOT register any auto-start entry.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORT=8765"
set "URL=http://127.0.0.1:%PORT%/"
if not exist "logs" mkdir "logs"

rem ---------- 1. already running? ----------
netstat -ano | findstr /C:":%PORT% " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [i] Server already running on port %PORT%.
    echo [i] Opening %URL%
    start "" "%URL%"
    exit /b 0
)

rem ---------- 2. locate a REAL python (skip the WindowsApps alias stub) ----------
set "PYEXE="
set "PYWEXE="

rem 跳过 WindowsApps 下的 python.exe —— 那是应用执行别名（0 字节占位），
rem 直接用它启动在部分环境下不会真正拉起解释器。
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

set "PYWEXE=%PYEXE:python.exe=pythonw.exe%"

echo [i] Python  : %PYEXE%

rem ---------- 3. start the server (silent via pythonw when available) ----------
echo [i] Starting Local Ops Assistant ...
if exist "%PYWEXE%" (
    start "ops-assistant" /min "%PYWEXE%" app.py
) else (
    start "ops-assistant" /min cmd /c ""%PYEXE%" app.py 1>>"logs\console.log" 2>&1"
)

rem ---------- 4. wait for the port ----------
set /a N=0
:loop
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr /C:":%PORT% " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 goto open
set /a N+=1
if !N! lss 40 goto loop

echo [!] Failed to start within 40 seconds.
echo     Please check: logs\app.log
pause
exit /b 1

:open
echo [i] Ready. Opening %URL%
start "" "%URL%"
exit /b 0
