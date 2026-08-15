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

"%PYTHON%" -c "import fastapi, uvicorn, openai, yaml" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Missing dependencies - installing from requirements.txt ...
    "%PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. See the output above.
        exit /b 1
    )
)

echo [INFO] Starting Blue Lake Agent at http://127.0.0.1:8000
"%PYTHON%" -m uvicorn server.main:app --reload --port 8000
