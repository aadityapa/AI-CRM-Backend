@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM KARNEX backend launcher (API + serves UI from sibling frontend repo).
REM Usage:
REM   start_app.bat                 -> HTTPS, opens browser
REM   start_app.bat --http          -> HTTP mode
REM   start_app.bat --no-browser    -> do not open browser
REM   start_app.bat --http --no-browser

set "MODE=https"
set "OPEN_BROWSER=1"
:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--http" set "MODE=http"
if /I "%~1"=="--https" set "MODE=https"
if /I "%~1"=="--open-browser" set "OPEN_BROWSER=1"
if /I "%~1"=="--no-browser" set "OPEN_BROWSER=0"
shift
goto parse_args
:args_done

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "CERTDIR=%BACKEND%\certs"
set "LOGDIR=%ROOT%logs"

if exist "%ROOT%.env" (
  for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /i "FRONTEND_DIR=" "%ROOT%.env" 2^>nul`) do set "FRONTEND_DIR=%%B"
  for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /i "PUBLIC_BASE_URL=" "%ROOT%.env" 2^>nul`) do set "PUBLIC_BASE_URL=%%B"
)
if not defined FRONTEND_DIR (
  if exist "%ROOT%frontend\index.html" (
    set "FRONTEND_DIR=%ROOT%frontend"
  ) else if exist "%ROOT%..\AI-Interview-Model-F-V2\frontend\index.html" (
    set "FRONTEND_DIR=%ROOT%..\AI-Interview-Model-F-V2\frontend"
  )
)
if defined FRONTEND_DIR (
  echo Frontend directory: %FRONTEND_DIR%
) else (
  echo WARNING: No frontend folder found. API will run; UI may be unavailable.
  echo Set FRONTEND_DIR in .env or build UI from AI-Interview-Model-F-V2.
)

set "PORT=2020"
set "SCHEME=https"
set "LOGFILE=%LOGDIR%\server-https.log"
if /I "%MODE%"=="http" (
  set "SCHEME=http"
  set "LOGFILE=%LOGDIR%\server.log"
)

if not exist "%LOGDIR%" mkdir "%LOGDIR%"
if not exist "%CERTDIR%" mkdir "%CERTDIR%"

echo Checking Python availability...
python --version >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python is not installed or not in PATH.
  pause
  exit /b 1
)

echo Checking required Python packages...
python -c "import fastapi,uvicorn,openai,pypdf,multipart,openpyxl,jwt" >nul 2>&1
if errorlevel 1 (
  echo Installing required dependencies...
  python -m pip install -r "%BACKEND%\requirements.txt"
  if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
  )
)

if /I "%MODE%"=="https" (
  echo Checking Python package cryptography...
  python -c "import cryptography" >nul 2>&1
  if errorlevel 1 (
    echo Installing cryptography ^(one-time^)...
    python -m pip install "cryptography>=42.0.0"
    if errorlevel 1 (
      echo ERROR: Failed to install cryptography.
      pause
      exit /b 1
    )
  )
  if not exist "%CERTDIR%\cert.pem" (
    echo Creating self-signed certificate in %CERTDIR%...
    cd /d "%BACKEND%"
    python generate_https_certs.py
    if errorlevel 1 (
      echo ERROR: Certificate generation failed.
      pause
      exit /b 1
    )
  )
)

echo Configuring Windows Firewall rule for port %PORT% (best effort)...
netsh advfirewall firewall add rule name="karnex-ai-hr-%PORT%" dir=in action=allow protocol=TCP localport=%PORT% >nul 2>&1

echo Stopping old KARNEX backend instances (ports 1010, 8443, %PORT%)...
taskkill /FI "WINDOWTITLE eq karnex AI HR*" /F >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "foreach ($port in @('1010','8443','%PORT%')) { netstat -ano | Select-String 'LISTENING' | Select-String (\":$port\s\") | ForEach-Object { $procId = ($_ -split '\s+')[-1]; if ($procId -match '^[0-9]+$' -and $procId -ne '0') { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue } } }; Start-Sleep -Seconds 2"

if exist "%LOGFILE%" (
  del /f /q "%LOGFILE%" >nul 2>&1
  if exist "%LOGFILE%" ren "%LOGFILE%" server-https.log.bak >nul 2>&1
)
if exist "%LOGFILE%" (
  set "LOGFILE=%LOGDIR%\server-https-!RANDOM!.log"
  echo Log file was locked; using !LOGFILE!
)
if /I "%MODE%"=="http" if exist "%LOGDIR%\server.log" (
  del /f /q "%LOGDIR%\server.log" >nul 2>&1
  if exist "%LOGDIR%\server.log" ren "%LOGDIR%\server.log" server.log.bak >nul 2>&1
)

echo Applying database migrations (alembic upgrade head)...
pushd "%BACKEND%"
python -m alembic upgrade head
if errorlevel 1 (
  echo WARNING: Database migration failed. CRM APIs may return 500 until fixed.
)
popd

echo Starting backend (%SCHEME%) on port %PORT%...
if /I "%MODE%"=="https" (
  start "karnex AI HR Backend" /min cmd /c ""%ROOT%scripts\run_backend.cmd" "%BACKEND%" %PORT% "!LOGFILE!" https "%CERTDIR%""
) else (
  start "karnex AI HR Backend" /min cmd /c ""%ROOT%scripts\run_backend.cmd" "%BACKEND%" %PORT% "!LOGFILE!" http "%CERTDIR%""
)

echo Waiting for server startup (up to 90 seconds)...
set "READY="
for /L %%i in (1,1,180) do (
  netstat -ano | findstr /i "LISTENING" | findstr /i ":%PORT% " >nul && set "READY=1"
  if defined READY goto :server_ready
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Milliseconds 500" >nul
)

if not defined READY goto :server_not_ready
goto :server_ready

:server_not_ready
echo ERROR: Backend did not become ready on %SCHEME%://127.0.0.1:%PORT%
echo Check logs: !LOGFILE!
if exist "!LOGFILE!" (
  echo.
  echo --- Last log lines ---
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -Path '!LOGFILE!' -Tail 25 -ErrorAction SilentlyContinue"
)
pause
exit /b 1

:server_ready

set "LAN_IP="
for /f "usebackq delims=" %%A in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\get_lan_ip.ps1"`) do set "LAN_IP=%%A"

if "%OPEN_BROWSER%"=="1" (
  echo Opening app in browser...
  if defined PUBLIC_BASE_URL (
    if /I not "!PUBLIC_BASE_URL!"=="auto" (
      start "" "!PUBLIC_BASE_URL!"
    ) else if defined LAN_IP (
      start "" "%SCHEME%://%LAN_IP%:%PORT%"
    ) else (
      start "" "%SCHEME%://127.0.0.1:%PORT%"
    )
  ) else if defined LAN_IP (
    start "" "%SCHEME%://%LAN_IP%:%PORT%"
  ) else (
    start "" "%SCHEME%://127.0.0.1:%PORT%"
  )
)

echo.
echo Backend ready.
if defined PUBLIC_BASE_URL (
  if /I not "!PUBLIC_BASE_URL!"=="auto" (
    echo Network URL: !PUBLIC_BASE_URL!
  )
)
echo Local URL:  %SCHEME%://127.0.0.1:%PORT%
if defined LAN_IP echo LAN URL:     %SCHEME%://%LAN_IP%:%PORT%
echo Admin:      %SCHEME%://127.0.0.1:%PORT%/admin
if defined PUBLIC_BASE_URL (
  if /I not "!PUBLIC_BASE_URL!"=="auto" (
    echo Admin LAN: "!PUBLIC_BASE_URL!/admin"
  )
)
echo Logs:       !LOGFILE!
echo.
echo Build UI first from frontend repo:  cd ..\AI-Interview-Model-F-V2 ^& start_frontend.bat

endlocal
