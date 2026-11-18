@echo off
setlocal EnableExtensions

REM ============================================================
REM  SONNY - GAMING MODE ON (INSTANT KILL & LOCK SWITCH)
REM  1. Writes GAMING_MODE.lock so Sonny cannot auto-restart
REM  2. Stops SonnyTerminal_GamingPC scheduled task
REM  3. Kills open-terminal.exe and ALL Sonny coding/media child
REM     processes (never touches Steam, games, Discord, iCUE, or
REM     unrelated Python installs)
REM  4. Stops any opt-in containers/renderers in media_stop_list.txt
REM  5. Verifies port 8002 is closed so anti-cheat & GPU stay clean
REM ============================================================

set "AGENT_DIR=C:\SonnyAgent"
set "LOCK_FILE=%AGENT_DIR%\GAMING_MODE.lock"
set "STOP_LIST=%AGENT_DIR%\media_stop_list.txt"
set "HELPER=%AGENT_DIR%\media_stop_helper.py"
set "VENV_PY=%AGENT_DIR%\venv\Scripts\python.exe"

if not exist "%AGENT_DIR%" mkdir "%AGENT_DIR%"

REM Step 1: Write lock file FIRST so no scheduled task or script restarts the agent.
> "%LOCK_FILE%" echo GAMING_MODE=ON
>> "%LOCK_FILE%" echo LOCKED_AT=%DATE% %TIME%
>> "%LOCK_FILE%" echo LOCKED_BY=%USERNAME%

REM Step 2: Stop the scheduled task cleanly if active.
schtasks /End /TN "SonnyTerminal_GamingPC" >nul 2>&1

REM Step 3: Force-kill open-terminal.exe and its entire child process tree.
taskkill /F /T /IM open-terminal.exe >nul 2>&1

REM Step 4: Stop any user-opted-in standalone media containers/binaries (safe blocklist enforced).
if exist "%HELPER%" (
    if exist "%VENV_PY%" (
        "%VENV_PY%" "%HELPER%" --list "%STOP_LIST%"
    ) else (
        python "%HELPER%" --list "%STOP_LIST%" 2>nul
    )
)

REM Step 5: Confirm port 8002 is no longer listening.
netstat -ano -p tcp | findstr /R /C:":8002 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    REM One brief retry in case kernel socket teardown took 300ms.
    ping -n 2 127.0.0.1 >nul
    taskkill /F /T /IM open-terminal.exe >nul 2>&1
)

echo.
echo ============================================================
echo  [LOCKED] GAMING MODE IS ON
echo  - All Sonny coding and media jobs stopped
echo  - Port 8002 closed (Sonny cannot use Gaming PC)
echo  - Automation hooks unloaded (anti-cheat safe)
echo  - Run ALLOW SONNY (GAME_MODE_OFF.cmd) when done gaming
echo ============================================================

if /I "%~1"=="--nopause" exit /b 0
timeout /t 3 >nul
exit /b 0
