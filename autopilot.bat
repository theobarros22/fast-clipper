@echo off
rem Piloto Automatico do Fast Clipper: roda UM ciclo (watchlist + pasta de entrada -> fila de aprovacao).
rem O Studio (iniciar.bat) precisa estar aberto. O Agendador do Windows chama este arquivo.
cd /d "%~dp0"
if not exist "%~dp0data\_tmp" mkdir "%~dp0data\_tmp"
set "FASTCLIPPER_DATA_DIR=%~dp0data"
set "TMP=%~dp0data\_tmp"
set "TEMP=%~dp0data\_tmp"
set "PYTHONIOENCODING=utf-8"
if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] .venv nao encontrado. Rode: python -m venv .venv ^&^& .venv\Scripts\pip install -e ".[transcribe]"
    exit /b 2
)
".venv\Scripts\python.exe" -m fastclipper.autopilot run-once
