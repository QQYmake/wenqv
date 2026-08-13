@echo off
rem Dev launcher for Blue Lake Agent (Windows).
rem Always uses the project virtualenv python explicitly, so a missing
rem dependency (e.g. openai) fails here with a clear message instead of
rem producing "The 'openai' package is required" at request time.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ERROR] Virtualenv not found at .venv\Scripts\python.exe
    echo Create and install it first:
    echo   python -m venv .venv
    echo   .venv\Scripts\Activate.ps1
    echo   python -m pip install -r requirements-dev.txt
    exit /b 1
)

"%PYTHON%" -c "import fastapi, uvicorn, openai, cryptography, yaml, aiosqlite" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Missing dependencies - installing from requirements.txt ...
    "%PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. See the output above.
        exit /b 1
    )
)

rem AGENT_SECRET_KEY: reuse .agent_secret_key if present, otherwise generate
rem one and persist it so user API keys survive restarts. The file is written
rem by python to avoid cmd escaping issues.
if "%AGENT_SECRET_KEY%"=="" (
    if exist ".agent_secret_key" (
        set /p AGENT_SECRET_KEY=<.agent_secret_key
    ) else (
        "%PYTHON%" -c "from cryptography.fernet import Fernet; open('.agent_secret_key', 'w', encoding='utf-8').write(Fernet.generate_key().decode())"
        set /p AGENT_SECRET_KEY=<.agent_secret_key
    )
)

echo [INFO] Starting Blue Lake Agent at http://127.0.0.1:8000
rem AGENT_COOKIE_SECURE=false: dev runs over plain http on loopback; a
rem "Secure" cookie would be dropped by stricter browsers there.
set "AGENT_COOKIE_SECURE=false"
"%PYTHON%" -m uvicorn server.main:app --reload --port 8000
