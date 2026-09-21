@echo off
rem ============================================================
rem  Local Ops Assistant - stop the background server
rem  (ASCII only: safe regardless of console code page)
rem
rem  Prefers tools\stop_server.ps1, which kills the listener AND
rem  sweeps orphan app.py processes. That second pass matters on
rem  Windows: SO_REUSEADDR lets two instances bind the same port,
rem  so killing the listener alone can leave one behind.
rem  Falls back to a plain netstat + taskkill loop.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PORT=8765"

if exist "%~dp0tools\stop_server.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\stop_server.ps1" -Port %PORT%
    goto done
)

echo [i] tools\stop_server.ps1 not found, using fallback.
set "FOUND=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr /C:"LISTENING"') do (
    if not "%%a"=="0" (
        echo [i] Killing PID %%a on port %PORT% ...
        taskkill /F /PID %%a >nul 2>&1
        set "FOUND=1"
    )
)
if "!FOUND!"=="0" (
    echo [i] No server is listening on port %PORT%.
) else (
    echo [i] Server stopped.
)

:done
ping -n 2 127.0.0.1 >nul
exit /b 0
