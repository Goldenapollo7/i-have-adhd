@echo off
setlocal EnableExtensions

REM Stops ONLY AI-PC open-terminal.exe and its child commands.
REM Never touches Ollama, Docker, Tailscale, or unrelated Python processes.
schtasks /End /TN "SonnyTerminal_AIPC" >nul 2>&1
taskkill /F /T /IM open-terminal.exe >nul 2>&1
echo [OK] AI-PC Open Terminal stopped (Ollama, Docker, and Open WebUI untouched).
exit /b 0
