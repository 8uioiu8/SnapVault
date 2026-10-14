@echo off
rem SnapVault one-click health check.  ASCII-only, CRLF, no smart quotes.
chcp 65001 >nul
setlocal
pushd "%~dp0"
set PY=%~dp0.venv\Scripts\python.exe
if exist "%PY%" goto run
set PY=D:\python\python.exe
if exist "%PY%" goto run
set PY=python.exe

:run
"%PY%" -m snapvault --doctor
echo.
pause
popd
endlocal
