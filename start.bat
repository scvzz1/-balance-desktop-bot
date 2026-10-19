@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python
py -3 bot.py
goto end
:python
python bot.py
:end
if errorlevel 1 pause
