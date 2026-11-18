@echo off
setlocal EnableExtensions

echo ============================================================
echo  SONNY - PRIVATE TAILSCALE HTTPS (AI PC ONLY)
echo ============================================================
echo  Publishes http://127.0.0.1:3000 ONLY inside your private
echo  Tailscale tailnet (uses `tailscale serve`, NEVER `funnel`).
echo ============================================================
echo.

where tailscale >nul 2>&1
if errorlevel 1 (
    echo [ERROR] tailscale CLI not found on PATH.
    exit /b 1
)

tailscale serve --bg --https=443 http://127.0.0.1:3000
if errorlevel 1 (
    echo [ERROR] `tailscale serve` failed. Make sure Tailscale is logged in and HTTPS certificates are enabled in your Tailscale DNS settings.
    exit /b 1
)

echo.
echo [OK] Private Tailscale HTTPS enabled. Your private Sonny URL:
tailscale serve status
echo.
echo Open the https://*.ts.net link above in iPhone Safari, sign in once,
echo then tap Share -^> Add to Home Screen.
exit /b 0
