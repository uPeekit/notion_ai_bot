@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\release_wizard.ps1"
echo.
pause
