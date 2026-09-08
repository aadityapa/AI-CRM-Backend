@echo off
setlocal EnableExtensions
REM ============================================================================
REM  KARNEX - ONE-CLICK REBUILD
REM  Builds the frontend, then restarts the backend (kills old instances,
REM  applies alembic migrations, serves the fresh UI, opens the browser).
REM
REM  Usage:
REM    rebuild_all.bat                 -> HTTPS, opens browser
REM    rebuild_all.bat --http          -> HTTP mode
REM    rebuild_all.bat --no-browser    -> do not open browser
REM
REM  After it finishes, press Ctrl+Shift+R in the browser once to be 100%%
REM  sure the cached index.html is replaced (built assets are hash-named,
REM  so everything else refreshes automatically).
REM ============================================================================

set "ROOT=%~dp0"
set "FRONTEND_BAT=%ROOT%..\AI-Interview-Model-F-V2\frontend\start.bat"

if not exist "%FRONTEND_BAT%" (
  echo ERROR: Frontend builder not found at:
  echo   %FRONTEND_BAT%
  echo Adjust FRONTEND_BAT in this file if the repo lives elsewhere.
  pause
  exit /b 1
)

echo ============================================================
echo  [1/2] Building frontend (npm install + vite build)...
echo ============================================================
call "%FRONTEND_BAT%"
if errorlevel 1 (
  echo.
  echo ############################################################
  echo  FRONTEND BUILD FAILED - backend was NOT restarted.
  echo  Fix the errors printed above, then run rebuild_all.bat again.
  echo ############################################################
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  [2/2] Restarting backend (migrations + server + browser)...
echo ============================================================
call "%ROOT%start_app.bat" %*
