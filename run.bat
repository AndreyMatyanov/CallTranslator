@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "HF_HOME=%~dp0models\hf"
if not exist .venv\Scripts\python.exe goto noenv
.venv\Scripts\python.exe -c "import sys" >nul 2>nul || goto noenv
call .venv\Scripts\activate.bat
python main.py
if errorlevel 1 pause
exit /b
:noenv
echo Окружение не найдено или собрано под удалённую версию Python.
echo Удалите папку .venv ^(если она есть^) и запустите install.bat.
pause
