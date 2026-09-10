@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\update_wizard.ps1"
echo.
pause
