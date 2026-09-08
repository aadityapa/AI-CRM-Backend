@echo off
setlocal EnableExtensions
set "BACKEND=%~1"
set "PORT=%~2"
set "LOGFILE=%~3"
set "MODE=%~4"
set "CERTDIR=%~5"

if not exist "%BACKEND%" (
  echo ERROR: Backend path not found: %BACKEND%>>"%LOGFILE%"
  exit /b 1
)

cd /d "%BACKEND%"

REM FRONTEND_DIR may be set by start_app.bat when frontend lives in a separate repo.
if defined FRONTEND_DIR (
  echo Using FRONTEND_DIR=%FRONTEND_DIR%>>"%LOGFILE%"
)

if /I "%MODE%"=="https" (
  python -m uvicorn main:app --host 0.0.0.0 --port %PORT% --ssl-keyfile "%CERTDIR%\key.pem" --ssl-certfile "%CERTDIR%\cert.pem" >"%LOGFILE%" 2>&1
) else (
  python -m uvicorn main:app --host 0.0.0.0 --port %PORT% >"%LOGFILE%" 2>&1
)
endlocal
