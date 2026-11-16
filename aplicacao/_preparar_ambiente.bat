@echo off
rem ============================================================
rem Usado pelos executaveis 1, 2 e 3. Encontra um Python com as
rem bibliotecas do projeto; se nao houver, cria .venv e baixa tudo.
rem Ao terminar, a variavel PY aponta para o Python a ser usado.
rem ============================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

set PY=
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Python\bin\python.exe" set "PY=%LOCALAPPDATA%\Python\bin\python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo ERRO: Python nao encontrado. Instale em https://www.python.org/downloads/ e rode novamente.
    pause
    exit /b 1
)

"%PY%" -c "import ultralytics, cv2, requests" >nul 2>nul
if errorlevel 1 (
    echo Bibliotecas ausentes. Criando ambiente .venv e baixando dependencias ^(alguns minutos, so na primeira vez^)...
    "%PY%" -m venv .venv || (echo ERRO ao criar .venv & pause & exit /b 1)
    set "PY=.venv\Scripts\python.exe"
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo ERRO ao instalar dependencias & pause & exit /b 1)
)

if not exist "modelos\yolo11n.pt" (
    echo Baixando o detector de pessoas yolo11n.pt...
    "%PY%" -c "from ultralytics import YOLO; import shutil; YOLO('yolo11n.pt'); shutil.move('yolo11n.pt', 'modelos/yolo11n.pt')"
)
exit /b 0
