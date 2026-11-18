@echo off
setlocal EnableExtensions

REM ============================================================
REM  SONNY - GAMING MODE OFF (ALLOW CODING & MEDIA GENERATION)
REM  Run locally on the Gaming PC when you finish gaming and want
REM  Sonny able to use the Gaming PC again.
REM ============================================================

set "AGENT_DIR=C:\SonnyAgent"
set "LOCK_FILE=%AGENT_DIR%\GAMING_MODE.lock"
set "CONFIG_FILE=%AGENT_DIR%\gaming-pc.toml"
set "VENV_PY=%AGENT_DIR%\venv\Scripts\python.exe"

if not exist "%CONFIG_FILE%" (
    echo [ERROR] Missing %CONFIG_FILE%. Run INSTALL_GAMING_PC.cmd first.
    exit /b 1
)

REM Step 1: Clear Gaming Mode lock.
if exist "%LOCK_FILE%" del /F /Q "%LOCK_FILE%"
if exist "%LOCK_FILE%" (
    echo [ERROR] Could not remove %LOCK_FILE%.
    exit /b 1
)

REM Step 2: Start the Gaming PC Open Terminal scheduled task.
schtasks /Run /TN "SonnyTerminal_GamingPC" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to start Scheduled Task SonnyTerminal_GamingPC. Run INSTALL_GAMING_PC.cmd first.
    exit /b 1
)

REM Step 3: Verify /health answers on the configured Tailscale IP:8002.
echo [STEP] Starting Gaming PC agent and checking port 8002 ...
"%VENV_PY%" -c "import pathlib,sys,time,tomllib,urllib.request as u; c=tomllib.loads(pathlib.Path(sys.argv[1]).read_text('utf-8')); url=f'http://{c[\"host\"]}:{c[\"port\"]}/health'; exec('for _ in range(12):\n try:\n  if u.urlopen(url,timeout=2).status==200: print(\"[OK] Listening on\",url); sys.exit(0)\n except Exception: time.sleep(1)\nsys.exit(1)')" "%CONFIG_FILE%"
if errorlevel 1 (
    echo [ERROR] Gaming PC agent did not respond. Verify Tailscale is connected and check %AGENT_DIR%\logs\gaming-pc
    exit /b 1
)

echo.
echo ============================================================
echo  [READY] GAMING MODE IS OFF
echo  - Sonny can run coding (C:\SonnyWork\code) and media
echo    generation (C:\SonnyWork\media) on the Gaming PC.
echo  - Hit Ctrl+Alt+G or STOP SONNY anytime to kill and lock.
echo ============================================================

if /I "%~1"=="--nopause" exit /b 0
timeout /t 3 >nul
exit /b 0
