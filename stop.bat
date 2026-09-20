@echo off
rem ============================================================
rem  Local Ops Assistant - stop the background server
rem  (ASCII only: safe regardless of console code page)
rem ============================================================
setlocal enabledelayedexpansion
set "PORT=8765"
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

rem ping 代替 timeout：timeout 在输入被重定向时会报错
ping -n 2 127.0.0.1 >nul
exit /b 0
