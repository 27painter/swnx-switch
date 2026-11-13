@echo off
rem swnx-switch launcher for this checkout (no pip install needed).
rem Usage: swnx-switch.cmd probe | read <part> | convert <src> <dst> | mcp
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%src;%PYTHONPATH%"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul
python -m swnx_switch.cli %*
exit /b %ERRORLEVEL%
