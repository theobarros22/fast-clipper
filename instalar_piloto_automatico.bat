@echo off
rem Cria a tarefa "FastClipper Piloto" no Agendador do Windows: um ciclo a cada 15 minutos.
rem Para remover: schtasks /Delete /TN "FastClipper Piloto" /F
cd /d "%~dp0"
if not exist "data\autopilot" mkdir "data\autopilot"
if not exist "data\autopilot\autopilot.json" (
    copy /Y "autopilot.example.json" "data\autopilot\autopilot.json" >nul
    echo [INFO] Criei data\autopilot\autopilot.json a partir do exemplo. Preencha a watchlist antes de usar.
)
schtasks /Create /TN "FastClipper Piloto" /TR "\"%~dp0autopilot.bat\"" /SC MINUTE /MO 15 /F
if errorlevel 1 (
    echo [ERRO] Nao consegui criar a tarefa. Tente rodar este arquivo como administrador.
) else (
    echo [OK] Piloto agendado a cada 15 minutos.
    echo      Fila de aprovacao: http://127.0.0.1:8000/aprovacao  ^(com o iniciar.bat aberto^)
)
pause
