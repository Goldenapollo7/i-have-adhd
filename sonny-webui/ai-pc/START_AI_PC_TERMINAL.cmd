@echo off
setlocal EnableExtensions

REM Starts ONLY the AI-PC Open Terminal Scheduled Task (127.0.0.1:8001).
schtasks /Run /TN "SonnyTerminal_AIPC" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Could not start Scheduled Task SonnyTerminal_AIPC. Run INSTALL_AI_PC.cmd first.
    exit /b 1
)
python -c "import sys,time,urllib.request as u; exec('for _ in range(int(sys.argv[2])):\n try:\n  if u.urlopen(sys.argv[1],timeout=2).status==200: sys.exit(0)\n except Exception: time.sleep(1)\nsys.exit(1)')" "http://127.0.0.1:8001/health" 12
if errorlevel 1 (
    echo [ERROR] AI-PC Open Terminal did not respond on 127.0.0.1:8001.
    exit /b 1
)
echo [OK] AI-PC Open Terminal running on 127.0.0.1:8001.
exit /b 0
