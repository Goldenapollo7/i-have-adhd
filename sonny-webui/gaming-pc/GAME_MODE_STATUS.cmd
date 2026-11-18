@echo off
setlocal EnableExtensions

set "LOCK_FILE=C:\SonnyAgent\GAMING_MODE.lock"

echo ============================================================
echo  SONNY - GAMING PC STATUS (Read-Only)
echo ============================================================

if exist "%LOCK_FILE%" (
    echo  Mode:           [LOCKED] GAMING MODE ON - Protected for gaming
    for /f "usebackq delims=" %%L in ("%LOCK_FILE%") do echo                  %%L
) else (
    echo  Mode:           [UNLOCKED] Available to Sonny ^(coding ^& media^)
)

tasklist /FI "IMAGENAME eq open-terminal.exe" 2>nul | find /I "open-terminal.exe" >nul
if errorlevel 1 (
    echo  Sonny Agent:    STOPPED ^(open-terminal.exe not running^)
) else (
    echo  Sonny Agent:    RUNNING ^(open-terminal.exe active^)
)

netstat -ano -p tcp | findstr /R /C:":8002 .*LISTENING" >nul 2>&1
if errorlevel 1 (
    echo  Port 8002:      CLOSED
) else (
    echo  Port 8002:      LISTENING ^(Tailscale interface^)
)

where nvidia-smi >nul 2>&1
if not errorlevel 1 (
    echo.
    echo  RTX 5090 Status ^(Name, Temp C, GPU %%, VRAM Used MiB, VRAM Total MiB, Power W^):
    for /f "usebackq delims=" %%G in (`nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw --format=csv,noheader`) do echo    %%G
)
echo ============================================================

if /I "%~1"=="--nopause" exit /b 0
pause
exit /b 0
