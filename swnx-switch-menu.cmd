@echo off
rem Double-click launcher for swnx-switch (interactive menu, Chinese UI).
rem Everything real happens in scripts\menu.ps1; this only picks a PowerShell
rem and keeps the console window open when the menu exits.
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"
set "PWSH="
where pwsh >nul 2>nul && set "PWSH=pwsh"
if not defined PWSH (
  where powershell >nul 2>nul && set "PWSH=powershell"
)
if not defined PWSH (
  echo PowerShell not found. Install Windows PowerShell 5.1 or PowerShell 7.
  pause
  exit /b 2
)
"%PWSH%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\menu.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%SWNX_NO_PAUSE%"=="1" pause
endlocal & exit /b %RC%
