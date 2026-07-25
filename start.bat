@echo off
cd /d %~dp0
title Consilium
if not exist .venv\Scripts\python.exe goto missing
echo Starting Consilium...
call .venv\Scripts\python.exe -u run.py
if errorlevel 1 pause
exit /b
:missing
echo Project Python was not found.
echo Open the project in Codex and ask to rebuild the environment.
pause
exit /b 1
