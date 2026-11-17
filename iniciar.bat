@echo off
title Fast Clipper Studio - Estacao Local de Clipping (9:16 e 16:9)
cls
echo =====================================================================
echo                FAST CLIPPER STUDIO - ESTACAO LOCAL
echo =====================================================================
echo [INFO] Iniciando servidor local e abrindo o Estudio no navegador...
echo [INFO] Endereco padrao: http://127.0.0.1:8000
echo [DICA] Deixe esta janela aberta enquanto estiver editando seus clipes.
echo =====================================================================
echo.
cd /d "%~dp0"
if exist "dist\FastClipper\FastClipper.exe" (
    call "dist\FastClipper\FastClipper.exe"
) else if exist ".venv\Scripts\python.exe" (
    call ".venv\Scripts\python.exe" -m fastclipper.desktop_entry
) else (
    echo [ERRO] Ambiente .venv ou dist\FastClipper\FastClipper.exe nao encontrado.
    echo Execute: python -m venv .venv ^&^& .venv\Scripts\pip install -e ".[dev,transcribe]"
)
pause
