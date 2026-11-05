@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
if exist "%~dp0pythonw.exe" set "PY=%~dp0pythonw.exe"
if not defined PY where pythonw >nul 2>nul && set "PY=pythonw"
if not defined PY (
    echo 没找到 Python。
    echo 请安装 Python 3.10+，或到 Releases 下载便携包。
    pause
    exit /b 1
)

start "" "%PY%" "%~dp0token_ledger.py" %*
