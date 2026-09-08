@echo off
REM ==========================================================================
REM  Karnex backup — database + uploaded files, verified and pruned.
REM
REM  RUN IT DAILY with Windows Task Scheduler (one-time setup):
REM
REM    1. Start -> "Task Scheduler" -> Create Task...
REM    2. General : name "Karnex Backup"; tick "Run whether user is logged on
REM                 or not"; tick "Run with highest privileges".
REM    3. Triggers: New -> Daily -> 22:00 (pick a time the PC is on).
REM    4. Actions : New -> Start a program
REM                 Program:   F:\AI-Interview-Model-B-V2\backup_karnex.bat
REM                 Start in:  F:\AI-Interview-Model-B-V2
REM    5. Settings: tick "Run task as soon as possible after a scheduled start
REM                 is missed" — this is what covers a machine that was off.
REM
REM  Then TEST A RESTORE once. A backup you have never restored is a hope,
REM  not a backup:
REM    pg_restore --clean --if-exists -d karnex_test db-YYYYmmdd-HHMMSS.dump
REM ==========================================================================
setlocal
cd /d "%~dp0"

if not exist "backend\main.py" (
  echo ERROR: run this from the AI-Interview-Model-B-V2 folder.
  exit /b 2
)

REM Log every run; keeping the log next to the backups makes failures findable.
set "LOGDIR=%~dp0logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\backup.log"

echo. >> "%LOGFILE%"
echo ===== %DATE% %TIME% ===== >> "%LOGFILE%"

python "scripts\backup_karnex.py" %* >> "%LOGFILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Echo the tail so a manual run shows the outcome on screen too.
powershell -NoProfile -Command "Get-Content '%LOGFILE%' -Tail 12" 2>nul

if not "%RC%"=="0" (
  echo.
  echo BACKUP FAILED ^(exit %RC%^) - see %LOGFILE%
  exit /b %RC%
)
exit /b 0
