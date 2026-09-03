@echo off
REM Launch WhisperType. Lives in the repo root; %~dp0 is that folder, so the
REM paths below survive the repo being moved or cloned somewhere else.
REM
REM   WhisperType.bat          - silent launch, no console (what startup uses)
REM   WhisperType.bat debug    - console window kept open, logging visible
REM
REM Labels rather than parenthesised if-blocks: cmd expands %VAR% for a whole
REM block at parse time, so a variable set and used inside one reads stale.
setlocal
set "ROOT=%~dp0"
if /i "%~1"=="debug" goto debug

REM pythonw.exe is the GUI build of the interpreter: no console window, and
REM nothing left in the taskbar once the tray icon appears.
set "PY=%ROOT%.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=pythonw.exe"
start "" "%PY%" "%ROOT%src\main.py"
exit /b 0

:debug
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%ROOT%src\main.py"
set "CODE=%ERRORLEVEL%"
echo.
echo WhisperType exited with code %CODE%.
pause
exit /b %CODE%
