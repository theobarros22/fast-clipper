@echo off
title Parar Fast Clipper Studio
echo =====================================================================
echo                ENCERRANDO FAST CLIPPER STUDIO
echo =====================================================================
echo [INFO] Encerrando processos do Fast Clipper e liberando a porta 8000...

taskkill /F /IM FastClipper.exe /T 2>nul
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"

echo.
echo [OK] Fast Clipper Studio encerrado com sucesso.
echo =====================================================================
timeout /t 3 >nul
