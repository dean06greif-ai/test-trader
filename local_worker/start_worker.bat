@echo off
REM Lokaler Worker - Start (Windows)
cd /d "%~dp0"
where python >nul 2>nul || (echo Python nicht gefunden - bitte von https://python.org installieren & pause & exit /b 1)
python -m pip install -r requirements.txt --quiet
python worker.py %*
pause
