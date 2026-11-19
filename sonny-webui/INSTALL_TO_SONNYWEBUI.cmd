@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================================
REM  Sonny - INSTALL TO C:\SonnyWebUI (one command, safe, verified)
REM
REM  Run this from inside the extracted folder that contains SETUP.md.
REM  It copies the whole kit to C:\SonnyWebUI\sonny-webui and verifies it.
REM
REM  Safe behaviour:
REM    - Never overwrites an existing install without asking.
REM    - Never deletes your generated keys (.env / *.toml / firewall_rule.txt).
REM    - Fails closed if the copy would damage .cmd line endings.
REM ============================================================================

echo ============================================================
echo  SONNY - INSTALL TO C:\SonnyWebUI
echo ============================================================
echo.

set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "DEST_ROOT=C:\SonnyWebUI"
set "DEST=%DEST_ROOT%\sonny-webui"

REM --- Sanity: are we actually in the kit? ---
if not exist "%SRC%\SETUP.md" (
    echo [ERROR] SETUP.md not found here.
    echo         Run this from inside the extracted "sonny-webui" folder.
    echo         Current folder: %SRC%
    goto :fail
)
if not exist "%SRC%\scripts\validate_kit.py" (
    echo [ERROR] scripts\validate_kit.py not found - this is not the kit folder.
    goto :fail
)
echo [OK] Found the kit at: %SRC%
echo.

REM --- Refuse to install onto itself ---
if /i "%SRC%"=="%DEST%" (
    echo [OK] Already installed at %DEST% - nothing to move.
    goto :verify
)

REM --- Existing install? Protect the owner's generated secrets. ---
if exist "%DEST%" (
    echo [WARN] An existing install was found at:
    echo        %DEST%
    echo.
    echo  Your generated keys live there:
    echo     ai-pc\.env, ai-pc\ai-pc.toml, gaming-pc\gaming-pc.toml
    echo.
    if exist "%DEST%\ai-pc\.env" (
        echo  [FOUND] Real keys already generated in the existing install.
        echo          These will be PRESERVED.
    )
    echo.
    choice /C YN /M "Replace the kit files in that folder (keys are kept)"
    if errorlevel 2 (
        echo [CANCELLED] Nothing changed.
        goto :fail
    )
    echo.
    echo [STEP] Backing up existing install to "%DEST_ROOT%\sonny-webui.backup" ...
    if exist "%DEST_ROOT%\sonny-webui.backup" rmdir /S /Q "%DEST_ROOT%\sonny-webui.backup"
    robocopy "%DEST%" "%DEST_ROOT%\sonny-webui.backup" /E /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 (
        echo [ERROR] Backup failed - refusing to continue.
        goto :fail
    )
    echo [OK] Backup created.
)

REM --- Create destination and copy (never deletes; merges over). ---
if not exist "%DEST_ROOT%" mkdir "%DEST_ROOT%"
if errorlevel 1 (
    echo [ERROR] Could not create %DEST_ROOT%. Run cmd.exe as Administrator.
    goto :fail
)

echo [STEP] Copying kit to %DEST% ...
robocopy "%SRC%" "%DEST%" /E /NFL /NDL /NJH /NJS /NP >nul
set "RC=%ERRORLEVEL%"
REM robocopy: 0-7 = success (1 just means files were copied), 8+ = real failure.
if %RC% GEQ 8 (
    echo [ERROR] Copy failed ^(robocopy code %RC%^).
    goto :fail
)
echo [OK] Copied ^(robocopy code %RC%^).
echo.

:verify
echo [STEP] Verifying the installed kit ...
echo.
pushd "%DEST%"
python scripts\validate_kit.py
set "VRC=%ERRORLEVEL%"
popd
echo.
if not "%VRC%"=="0" (
    echo [ERROR] Validation FAILED at %DEST%
    echo         Do not proceed. Paste the output above for help.
    goto :fail
)

echo ============================================================
echo  [DONE] INSTALLED AND VERIFIED
echo ============================================================
echo.
echo  Kit location: %DEST%
echo.
echo  Next step - generate your private keys. Open cmd.exe and run:
echo.
echo      cd /d %DEST%
echo.
echo      tailscale ip -4          ^(first, on the GAMING PC^)
echo.
echo      python scripts\init_config.py --gaming-tailscale-ip ^<GAMING_TS_IP^>
echo.
echo  Then follow %DEST%\SETUP.md from "Step 2".
echo ============================================================
echo.
if /i "%~1"=="--nopause" exit /b 0
pause
exit /b 0

:fail
echo.
if /i "%~1"=="--nopause" exit /b 1
pause
exit /b 1
