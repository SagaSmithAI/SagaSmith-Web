@echo off
setlocal
cd /d "%~dp0"
where docker >nul 2>nul || (echo Docker Desktop is required.& exit /b 1)
docker info >nul 2>nul || (echo Docker Desktop must be running.& exit /b 1)
if not exist .env copy /y .env.example .env >nul
if not exist secrets mkdir secrets
if not exist secrets\agent-config.json copy /y config\agent-config.example.json secrets\agent-config.json >nul
echo Configuration templates are ready.
echo Edit .env and secrets\agent-config.json, then run start.bat.
exit /b 0
