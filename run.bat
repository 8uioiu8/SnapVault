@echo off
rem SnapVault launcher.
rem Prefers a project venv, then the development interpreter, then whatever
rem pythonw.exe is on PATH. Pass --console to keep a console window for logs.
rem NOTE: keep this file ASCII-only with CRLF line endings; cmd.exe is picky.
setlocal
set HERE=%~dp0
set PY=%HERE%.venv\Scripts\python.exe
if exist "%PY%" goto run
set PY=D:\python\python.exe
if exist "%PY%" goto run
set PY=
for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do if not defined PY set PY=%%i
if not defined PY set PY=pythonw.exe

:run
if /i "%~1"=="--console" (
    shift
    "%PY%" "%HERE%snapvault.pyw" %*
) else (
    start "" "%PY%" "%HERE%snapvault.pyw" %*
)
endlocal
