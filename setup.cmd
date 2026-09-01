@echo off
rem Double-click launcher: runs setup.ps1 without touching the execution policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
pause
