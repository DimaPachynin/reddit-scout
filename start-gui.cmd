@echo off
rem Starts the reddit-scout desktop app (Windows).
cd /d "%~dp0"
where uv >nul 2>nul || (echo uv not found: https://docs.astral.sh/uv/ & pause & exit /b 1)
uv sync --quiet || (echo uv sync failed & pause & exit /b 1)
start "" ".venv\Scripts\reddit-scout-gui.exe"
