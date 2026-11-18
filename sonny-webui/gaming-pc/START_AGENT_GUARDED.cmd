@echo off
setlocal EnableExtensions

REM Logon / task wrapper for Gaming PC Open Terminal (Coding & Media Generation ONLY).
REM Fails closed whenever GAMING_MODE.lock exists so reboots never interrupt gaming.

set "AGENT_DIR=C:\SonnyAgent"
set "LOCK_FILE=%AGENT_DIR%\GAMING_MODE.lock"
set "CONFIG_FILE=%AGENT_DIR%\gaming-pc.toml"
set "EXE_FILE=%AGENT_DIR%\venv\Scripts\open-terminal.exe"

if exist "%LOCK_FILE%" (
    echo [LOCKED] GAMING_MODE.lock is present - refusing to start Sonny agent on Gaming PC.
    exit /b 0
)

if not exist "%CONFIG_FILE%" (
    echo [ERROR] Missing %CONFIG_FILE%
    exit /b 1
)
if not exist "%EXE_FILE%" (
    echo [ERROR] Missing %EXE_FILE%
    exit /b 1
)

if not exist "C:\SonnyWork\code" mkdir "C:\SonnyWork\code"
if not exist "C:\SonnyWork\media" mkdir "C:\SonnyWork\media"
if not exist "%AGENT_DIR%\logs\gaming-pc" mkdir "%AGENT_DIR%\logs\gaming-pc"

"%EXE_FILE%" run --isolated --config "%CONFIG_FILE%" --cwd "C:\SonnyWork"
exit /b %ERRORLEVEL%
