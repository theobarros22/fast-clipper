@echo off
title Criar Atalho na Area de Trabalho - Fast Clipper Studio
cd /d "%~dp0"

echo Criando atalho do Fast Clipper na sua Area de Trabalho...

powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $desktop = [Environment]::GetFolderPath('Desktop'); $s = $ws.CreateShortcut(\"$desktop\Fast Clipper Studio.lnk\"); $s.TargetPath = (Resolve-Path 'iniciar.bat').Path; $s.WorkingDirectory = (Get-Location).Path; $s.Description = 'Estacao Local de Clipping do Fast Clipper Studio'; $s.Save()"

echo.
echo [SUCESSO] Atalho 'Fast Clipper Studio' criado na sua Area de Trabalho!
echo Agora voce pode iniciar o programa dando 2 cliques direto na Area de Trabalho.
echo.
pause
