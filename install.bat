@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Call Translator: установка ===
rem --- Всё тяжёлое кладём на этот же диск, а не на C: ---
set "PIP_NO_CACHE_DIR=1"
set "TEMP=%~dp0tmp"
set "TMP=%~dp0tmp"
if not exist "%TEMP%" mkdir "%TEMP%"
set "OLLAMA_MODELS=%~d0\Ollama\models"
if not exist "%OLLAMA_MODELS%" mkdir "%OLLAMA_MODELS%"
setx OLLAMA_MODELS "%OLLAMA_MODELS%" >nul
rem Экономия видеопамяти: один запрос за раз, сжатый кэш контекста, одна модель в памяти
setx OLLAMA_NUM_PARALLEL 1 >nul
setx OLLAMA_FLASH_ATTENTION 1 >nul
setx OLLAMA_KV_CACHE_TYPE q8_0 >nul
setx OLLAMA_MAX_LOADED_MODELS 1 >nul
set "OLLAMA_NUM_PARALLEL=1"
set "OLLAMA_FLASH_ATTENTION=1"
set "OLLAMA_KV_CACHE_TYPE=q8_0"
set "OLLAMA_MAX_LOADED_MODELS=1"
echo Модели Ollama будут храниться в %OLLAMA_MODELS%

rem --- Нужен Python 3.11-3.13: под более новые версии ещё нет сборок onnxruntime и других библиотек ---
set "PY="
for %%V in (3.12 3.11 3.13) do (
  if not defined PY ( py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V" )
)
if not defined PY ( python -c "import sys; assert (3,11)<=sys.version_info[:2]<=(3,13)" >nul 2>nul && set "PY=python" )
if not defined PY (
  echo.
  echo ОШИБКА: нужен Python 3.12 ^(подойдут 3.11-3.13^). Более новые версии пока не поддерживаются библиотеками распознавания речи.
  echo Скачайте: https://www.python.org/downloads/release/python-31210/  ^(Windows installer 64-bit, галочка "Add python.exe to PATH"^)
  echo Другие версии Python удалять не нужно.
  pause
  exit /b 1
)
echo Использую: %PY%
rem Окружение, созданное неподходящей версией Python, пересоздаём
if exist .venv\Scripts\python.exe ( .venv\Scripts\python.exe -c "import sys; assert (3,11)<=sys.version_info[:2]<=(3,13)" >nul 2>nul || ( echo Пересоздаю окружение под %PY%... & rmdir /s /q .venv ) )
if not exist .venv %PY% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || (pause & exit /b 1)
where nvidia-smi >nul 2>nul && (echo Найдена видеокарта NVIDIA - ставлю CUDA-библиотеки... & pip install -r requirements-gpu.txt)

rem --- Ollama: ищем, при отсутствии скачиваем и ставим на тот же диск, что и программа ---
set "OLLAMA_EXE="
for /f "delims=" %%I in ('where ollama 2^>nul') do if not defined OLLAMA_EXE set "OLLAMA_EXE=%%I"
if not defined OLLAMA_EXE if exist "%~d0\Ollama\ollama.exe" set "OLLAMA_EXE=%~d0\Ollama\ollama.exe"
if not defined OLLAMA_EXE if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if defined OLLAMA_EXE goto have_ollama

echo.
echo Ollama не найдена - скачиваю установщик ^(около 1 ГБ^)...
curl -L --fail --progress-bar -o "%TEMP%\OllamaSetup.exe" https://ollama.com/download/OllamaSetup.exe || goto ollama_manual
echo Устанавливаю Ollama в %~d0\Ollama ...
"%TEMP%\OllamaSetup.exe" /SILENT /SUPPRESSMSGBOXES /NORESTART /DIR="%~d0\Ollama"
if exist "%~d0\Ollama\ollama.exe" set "OLLAMA_EXE=%~d0\Ollama\ollama.exe"
if not defined OLLAMA_EXE if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if defined OLLAMA_EXE goto have_ollama

:ollama_manual
echo.
echo Не удалось установить Ollama автоматически.
echo Скачайте её вручную: https://ollama.com/download/windows , установите и запустите install.bat ещё раз.
echo ^(Модель перевода оверлей потом скачает сам при запуске.^)
goto done

:have_ollama
echo Ollama: %OLLAMA_EXE%
rem Перезапускаем Ollama, чтобы она подхватила папку моделей на этом диске
taskkill /f /im "ollama app.exe" >nul 2>nul
taskkill /f /im ollama.exe >nul 2>nul
timeout /t 2 /nobreak >nul
start "" /b "%OLLAMA_EXE%" serve >nul 2>nul
timeout /t 4 /nobreak >nul
echo Скачиваю модель перевода ^(около 3 ГБ, один раз^)...
"%OLLAMA_EXE%" pull gemma3:4b
taskkill /f /im ollama.exe >nul 2>nul
for %%P in ("%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" "%~d0\Ollama\ollama app.exe") do if exist %%P start "" %%P

:done
rmdir /s /q "%~dp0tmp" 2>nul
echo.
echo Готово. Запуск: run.bat
pause
