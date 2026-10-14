@echo off
rem SnapVault one-click packager (PyInstaller, onedir).
rem
rem ASCII-ONLY, CRLF line endings. This is not stylistic: cmd.exe reads a batch
rem file byte by byte with the *current* code page, and this script switches it
rem (`chcp 65001`) half way through. Non-ASCII text in here can make the parser
rem lose its place. Keep every line below plain 7-bit ASCII.
rem
rem Output: dist\SnapVault\SnapVault.exe
setlocal
chcp 65001 >nul
pushd "%~dp0.."

set PY=.venv\Scripts\python.exe
if exist "%PY%" goto run
set PY=D:\python\python.exe
if exist "%PY%" goto run
set PY=python.exe

:run
echo [1/2] rendering app icon ...
"%PY%" packaging\make_ico.py
if errorlevel 1 goto fail

echo [2/2] packing with PyInstaller ...
rem --clean is mandatory: PyInstaller reuses build\SnapVault\COLLECT-00.toc,
rem so editing the spec's PRUNE list without it changes nothing at all
rem (dist stays byte-for-byte identical and no warning is printed).
"%PY%" -m PyInstaller packaging\SnapVault.spec --noconfirm --clean --distpath dist --workpath build
if errorlevel 1 goto fail

rem PyInstaller 6 changed the onedir layout: everything from `datas` now lands in
rem _internal\ instead of next to the exe, so THIRD_PARTY_LICENSES.md ends up at
rem dist\SnapVault\_internal\THIRD_PARTY_LICENSES.md.
rem
rem That still satisfies the HarmonyOS Sans licence ("retain the copyright notice
rem and this Agreement in any copies"), because the file does travel with the
rem build. But it is effectively invisible to whoever unzips the app, so put a
rem second copy right next to the exe where it can actually be found.
copy /y "THIRD_PARTY_LICENSES.md" "dist\SnapVault\THIRD_PARTY_LICENSES.md" >nul
copy /y "LICENSE" "dist\SnapVault\LICENSE" >nul

echo.
echo Done.
echo   dist\SnapVault\SnapVault.exe
pause
popd
endlocal
exit /b 0

:fail
echo.
echo FAILED -- see the messages above.
pause
popd
endlocal
exit /b 1
