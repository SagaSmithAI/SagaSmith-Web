@echo off
setlocal
cd /d "%~dp0"
if not exist .env (echo Run install.bat and configure .env first.& exit /b 1)
if not exist secrets\agent-config.json (echo Missing secrets\agent-config.json.& exit /b 1)
docker compose up -d --build
docker compose ps
