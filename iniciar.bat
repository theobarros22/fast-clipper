@echo off
title Fast Clipper Studio - Estacao Local de Clipping 9:16
cls
echo =====================================================================
echo                FAST CLIPPER STUDIO - ESTACAO LOCAL
echo =====================================================================
echo [INFO] Iniciando servidor local e abrindo o Estudio no navegador...
echo [INFO] Endereco: http://127.0.0.1:8000
echo [DICA] Deixe esta janela aberta enquanto estiver editando seus clipes.
echo =====================================================================
echo.
cd /d "%~dp0"
call .venv\Scripts\python.exe -m fastclipper studio
pause
