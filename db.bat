@echo off
REM Database management helper script

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"

if "%1"=="" goto :usage
if "%1"=="start" goto :start
if "%1"=="stop" goto :stop
if "%1"=="logs" goto :logs
if "%1"=="shell" goto :shell
if "%1"=="init" goto :init
if "%1"=="migrate" goto :migrate
if "%1"=="test" goto :test
goto :usage

:start
echo Starting PostgreSQL database...
docker-compose up -d
timeout /t 3 /nobreak >nul
echo Database started on localhost:5433
echo pgAdmin available at http://localhost:5050
goto :eof

:stop
echo Stopping PostgreSQL database...
docker-compose down
echo Database stopped
goto :eof

:logs
echo Showing database logs (Ctrl+C to exit)...
docker-compose logs -f postgres
goto :eof

:shell
echo Opening PostgreSQL shell (psql)...
echo Database: aibot_mt5
echo User: aibot
echo.
docker exec -it ai-bot-mt5-db psql -U aibot -d aibot_mt5
goto :eof

:init
echo Initializing database tables...
"%VENV_PY%" -c "from database.connection import init_db; init_db(); print('✓ Database initialized')"
goto :eof

:migrate
echo Running Alembic migrations...
"%VENV_PY%" -m alembic upgrade head
echo ✓ Migrations complete
goto :eof

:test
echo Testing database connection...
"%VENV_PY%" -c "from database.connection import test_connection; test_connection()"
goto :eof

:usage
echo.
echo Database Management Script
echo.
echo Usage: db.bat [command]
echo.
echo Commands:
echo   start    - Start PostgreSQL container
echo   stop     - Stop PostgreSQL container
echo   logs     - View database logs
echo   shell    - Open psql shell
echo   init     - Initialize database tables
echo   migrate  - Run Alembic migrations
echo   test     - Test database connection
echo.
goto :eof
