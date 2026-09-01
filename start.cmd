@echo off
rem Double-click launcher: runs start.ps1 without touching the execution policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
pause
