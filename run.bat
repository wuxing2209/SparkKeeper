@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   Douyin Spark Web - local server
echo ============================================

set "PYCMD="

call :try_cmd python
if defined PYCMD goto :found

call :try_cmd py -3.11
if defined PYCMD goto :found

call :try_cmd py -3.12
if defined PYCMD goto :found

rem 1) explicit override: set DOUYIN_PY=C:\path\to\python.exe
if defined DOUYIN_PY call :try_exe "%DOUYIN_PY%"
if defined PYCMD goto :found

rem 2) portable runtime shipped next to this folder
if exist "%~dp0portable_python\python.exe" call :try_exe "%~dp0portable_python\python.exe"
if defined PYCMD goto :found

rem 3) bundled runtime of some desktop agents (this machine has one)
for /d %%d in ("C:\Program Files\Tencent\Marvis\MarvisAgent\*") do (
  if not defined PYCMD call :try_exe "%%d\runtime\python311\python.exe"
)
if defined PYCMD goto :found

for %%v in (313 312 311 310 39) do (
  if not defined PYCMD call :try_exe "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
)
if defined PYCMD goto :found

echo.
echo [ERROR] No Python interpreter with playwright was found.
echo.
echo   Open a terminal and run these with the interpreter you want to use:
echo       python -m pip install playwright
echo       python -m playwright install chromium
echo.
echo   Then double-click this file again.
echo.
pause
exit /b 1

:found
echo.
echo Python  : %PYCMD%
echo Profile : %~dp0profile
echo Address : http://127.0.0.1:8765/
echo.
%PYCMD% server.py

echo.
echo [INFO] server stopped.
pause
exit /b 0

:try_cmd
if defined PYCMD goto :eof
%* -c "import playwright" >nul 2>&1
if errorlevel 1 goto :eof
set "PYCMD=%*"
goto :eof

:try_exe
if defined PYCMD goto :eof
if not exist %1 goto :eof
%1 -c "import playwright" >nul 2>&1
if errorlevel 1 goto :eof
set "PYCMD=%1"
goto :eof
