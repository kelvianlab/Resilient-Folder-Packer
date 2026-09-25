@echo off
REM Portable launcher - keep this next to rfpack.py, copy both anywhere.
setlocal
set "HERE=%~dp0"
where py >nul 2>nul && (py -3 "%HERE%rfpack.py" %* & exit /b %errorlevel%)
where python >nul 2>nul && (python "%HERE%rfpack.py" %* & exit /b %errorlevel%)
echo Python 3.8+ was not found on this machine.
echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
exit /b 1
