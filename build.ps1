# Gera os dois executaveis na pasta dist\
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

pyinstaller --onefile --clean --noconfirm --name TunelServidor tunel_servidor.py
pyinstaller --onefile --clean --noconfirm --name TunelCliente tunel_cliente.py

Copy-Item config.json dist\ -Force

Write-Host ""
Write-Host "Executaveis prontos em $PSScriptRoot\dist" -ForegroundColor Green
Write-Host "Lembre-se: o config.json precisa ficar na MESMA pasta do .exe" -ForegroundColor Yellow
