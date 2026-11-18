@echo off
setlocal EnableExtensions

echo ============================================================
echo  SONNY - GAMING PC SETUP (Coding ^& Media Worker + Kill-Switch)
echo ============================================================
echo  Role: Coding (C:\SonnyWork\code) and Media Generation
echo        (C:\SonnyWork\media) ONLY.
echo        Open WebUI and primary AI stay on the AI PC.
echo ============================================================
echo.

set "KIT_DIR=%~dp0"
if "%KIT_DIR:~-1%"=="\" set "KIT_DIR=%KIT_DIR:~0,-1%"
set "AGENT_DIR=C:\SonnyAgent"
set "VENV_DIR=%AGENT_DIR%\venv"
set "CONFIG_DST=%AGENT_DIR%\gaming-pc.toml"
set "TASK_NAME=SonnyTerminal_GamingPC"

REM 1. Require generated gaming-pc.toml copied from init_config.py output.
if not exist "%KIT_DIR%\gaming-pc.toml" (
    echo [ERROR] Missing %KIT_DIR%\gaming-pc.toml
    echo         Run init_config.py on AI PC first, then copy the gaming-pc folder here.
    exit /b 1
)

REM 2. Verify Python 3.11+.
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.11+ not found on PATH.
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.11 or newer is required.
    exit /b 1
)
echo [OK] Python 3.11+ verified.

REM 3. Validate gaming-pc.toml binds ONLY to a Tailscale 100.64.0.0/10 IP (never 0.0.0.0) and is assigned to this PC.
python -c "import ipaddress,pathlib,socket,sys,tomllib; c=tomllib.loads(pathlib.Path(sys.argv[1]).read_text('utf-8')); h=c.get('host',''); ip=ipaddress.ip_address(h); assert ip in ipaddress.ip_network('100.64.0.0/10'), f'{h} is not a Tailscale IP'; s=socket.socket(); s.bind((h,0)); s.close(); print(f'[OK] Verified local Tailscale IP {h} for port {c.get(\"port\")}.')" "%KIT_DIR%\gaming-pc.toml"
if errorlevel 1 (
    echo [ERROR] gaming-pc.toml host IP is not an active Tailscale IP on this Gaming PC.
    echo         Confirm Tailscale is connected and check `tailscale ip -4`.
    exit /b 1
)

REM 4. Create directories and isolated Python venv.
if not exist "%AGENT_DIR%\logs\gaming-pc" mkdir "%AGENT_DIR%\logs\gaming-pc"
if not exist "C:\SonnyWork\code" mkdir "C:\SonnyWork\code"
if not exist "C:\SonnyWork\media" mkdir "C:\SonnyWork\media"

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [STEP] Creating isolated Python venv at %VENV_DIR% ...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create venv at %VENV_DIR%.
        exit /b 1
    )
)

echo [STEP] Installing/verifying open-terminal in isolated venv ...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade --disable-pip-version-check open-terminal
if errorlevel 1 (
    echo [ERROR] Failed to install open-terminal.
    exit /b 1
)
if not exist "%VENV_DIR%\Scripts\open-terminal.exe" (
    echo [ERROR] open-terminal.exe missing after install.
    exit /b 1
)
echo [OK] open-terminal installed in isolated venv.

REM 5. Install config (ACL-locked) and Gaming Mode kill-switch scripts FIRST.
copy /Y "%KIT_DIR%\gaming-pc.toml" "%CONFIG_DST%" >nul
icacls "%CONFIG_DST%" /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:(R,W)" >nul
copy /Y "%KIT_DIR%\media_stop_helper.py" "%AGENT_DIR%\media_stop_helper.py" >nul
copy /Y "%KIT_DIR%\START_AGENT_GUARDED.cmd" "%AGENT_DIR%\START_AGENT_GUARDED.cmd" >nul
copy /Y "%KIT_DIR%\GAME_MODE_ON.cmd" "%AGENT_DIR%\GAME_MODE_ON.cmd" >nul
copy /Y "%KIT_DIR%\GAME_MODE_OFF.cmd" "%AGENT_DIR%\GAME_MODE_OFF.cmd" >nul
copy /Y "%KIT_DIR%\GAME_MODE_STATUS.cmd" "%AGENT_DIR%\GAME_MODE_STATUS.cmd" >nul
if not exist "%AGENT_DIR%\media_stop_list.txt" (
    copy /Y "%KIT_DIR%\media_stop_list.txt" "%AGENT_DIR%\media_stop_list.txt" >nul
)
echo [OK] Gaming Mode kill-switch and locked config installed in %AGENT_DIR%.

REM 6. Register logon Scheduled Task using START_AGENT_GUARDED.cmd (honors GAMING_MODE.lock on reboot).
schtasks /Create /F /TN "%TASK_NAME%" /SC ONLOGON /RL LIMITED /TR "\"%AGENT_DIR%\START_AGENT_GUARDED.cmd\"" >nul
if errorlevel 1 (
    echo [ERROR] Failed to create Scheduled Task %TASK_NAME%.
    exit /b 1
)
echo [OK] Registered Scheduled Task %TASK_NAME% ^(guarded by GAMING_MODE.lock^).

REM 7. Apply Windows Firewall rule locking TCP 8002 to AI PC Tailscale IP (if elevated).
set "AI_TS_IP="
if exist "%KIT_DIR%\firewall_rule.txt" set /p AI_TS_IP=<"%KIT_DIR%\firewall_rule.txt"
if defined AI_TS_IP (
    python -c "import ipaddress,sys; ip=ipaddress.ip_address(sys.argv[1].strip()); sys.exit(0 if ip in ipaddress.ip_network('100.64.0.0/10') else 1)" "%AI_TS_IP%"
    if errorlevel 1 (
        echo [WARN] Ignored firewall_rule.txt because value is not a valid Tailscale IP.
    ) else (
        net session >nul 2>&1
        if not errorlevel 1 (
            netsh advfirewall firewall delete rule name="Sonny_GamingPC_OpenTerminal_8002" >nul 2>&1
            netsh advfirewall firewall add rule name="Sonny_GamingPC_OpenTerminal_8002" dir=in action=allow protocol=TCP localport=8002 remoteip="%AI_TS_IP%" >nul
            echo [OK] Windows Firewall locked TCP 8002 to AI PC Tailscale IP %AI_TS_IP% only.
        ) else (
            echo [NOTE] To lock Windows Firewall TCP 8002 strictly to AI PC, run once in an Admin Command Prompt:
            echo        netsh advfirewall firewall add rule name="Sonny_GamingPC_OpenTerminal_8002" dir=in action=allow protocol=TCP localport=8002 remoteip=%AI_TS_IP%
        )
    )
)

REM 8. Create Desktop shortcuts (STOP SONNY gets Ctrl+Alt+G hotkey; drag onto Xeneon Edge 14.5" touchscreen).
set "VBS_TMP=%TEMP%\sonny_shortcuts_%RANDOM%.vbs"
> "%VBS_TMP%" echo Set ws = CreateObject("WScript.Shell")
>> "%VBS_TMP%" echo desk = ws.SpecialFolders("Desktop")
>> "%VBS_TMP%" echo Set s1 = ws.CreateShortcut(desk ^& "\STOP SONNY - GAME MODE.lnk")
>> "%VBS_TMP%" echo s1.TargetPath = "C:\SonnyAgent\GAME_MODE_ON.cmd"
>> "%VBS_TMP%" echo s1.WorkingDirectory = "C:\SonnyAgent"
>> "%VBS_TMP%" echo s1.Hotkey = "Ctrl+Alt+G"
>> "%VBS_TMP%" echo s1.Description = "Instantly stop all Sonny coding/media jobs and lock Gaming PC for gaming"
>> "%VBS_TMP%" echo s1.Save
>> "%VBS_TMP%" echo Set s2 = ws.CreateShortcut(desk ^& "\ALLOW SONNY.lnk")
>> "%VBS_TMP%" echo s2.TargetPath = "C:\SonnyAgent\GAME_MODE_OFF.cmd"
>> "%VBS_TMP%" echo s2.WorkingDirectory = "C:\SonnyAgent"
>> "%VBS_TMP%" echo s2.Description = "Turn Gaming Mode OFF and allow Sonny coding/media jobs"
>> "%VBS_TMP%" echo s2.Save
>> "%VBS_TMP%" echo Set s3 = ws.CreateShortcut(desk ^& "\SONNY GAMING STATUS.lnk")
>> "%VBS_TMP%" echo s3.TargetPath = "C:\SonnyAgent\GAME_MODE_STATUS.cmd"
>> "%VBS_TMP%" echo s3.WorkingDirectory = "C:\SonnyAgent"
>> "%VBS_TMP%" echo s3.Description = "Check Gaming Mode state and RTX 5090 usage"
>> "%VBS_TMP%" echo s3.Save
cscript //nologo "%VBS_TMP%" >nul 2>&1
if exist "%VBS_TMP%" del /F /Q "%VBS_TMP%"
echo [OK] Desktop shortcuts created ^(STOP SONNY hotkey: Ctrl+Alt+G^).

REM 9. Start agent if Gaming Mode is not locked and verify health.
if exist "%AGENT_DIR%\GAMING_MODE.lock" (
    echo [LOCKED] GAMING_MODE.lock is set - leaving agent stopped until you run ALLOW SONNY.
) else (
    call "%AGENT_DIR%\GAME_MODE_OFF.cmd" --nopause
    if errorlevel 1 exit /b 1
)

echo.
echo ============================================================
echo  [DONE] GAMING PC READY
echo  - Instant kill + lock: Desktop "STOP SONNY - GAME MODE" or Ctrl+Alt+G
echo  - Re-enable after gaming: Desktop "ALLOW SONNY"
echo  - Optional: pin shortcuts on your Xeneon Edge 14.5-inch screen
echo ============================================================
exit /b 0
