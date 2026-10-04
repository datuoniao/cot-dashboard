@echo off
rem ============================================================
rem  CFTC COT Dashboard - one-click weekly update
rem  Steps: fetch positions -> fetch prices -> build data
rem  NOTE: this file is intentionally ASCII-only, because cmd.exe
rem        reads .bat files in the OEM codepage (GBK) and would
rem        garble non-ASCII text.
rem ============================================================
setlocal
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

if not exist "logs" mkdir "logs"
set "LOG=logs\update.log"

echo.
echo === CFTC COT dashboard update ===
echo log file: %LOG%
echo [%date:~0,10% %time:~0,8%] ===== update START =====>>"%LOG%"

echo [1/3] fetching CFTC positions ...
"%PY%" fetch_positions.py >>"%LOG%" 2>&1
if errorlevel 1 goto :err

echo [2/3] fetching prices ...
"%PY%" fetch_prices.py >>"%LOG%" 2>&1
if errorlevel 1 goto :err

echo [3/3] building dashboard data ...
"%PY%" build_data.py >>"%LOG%" 2>&1
if errorlevel 1 goto :err

echo [%date:~0,10% %time:~0,8%] ===== update OK =====>>"%LOG%"
echo.
echo === DONE. Open index.html to view the dashboard ===
endlocal
exit /b 0

:err
echo [%date:~0,10% %time:~0,8%] !!!!! update FAILED !!!!!>>"%LOG%"
echo.
echo === FAILED. Check network, then see %LOG% ===
endlocal
exit /b 1
