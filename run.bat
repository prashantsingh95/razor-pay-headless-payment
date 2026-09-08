@echo off
setlocal
echo === Razorpay Headless API - One-Click Isolated Setup ===

:: Detect python
where python >nul 2>&1
if %errorlevel%==0 (
    set PY=python
    goto check_venv
)
where python3 >nul 2>&1
if %errorlevel%==0 (
    set PY=python3
    goto check_venv
)
where py >nul 2>&1
if %errorlevel%==0 (
    set PY=py
    goto check_venv
)
echo Python not found! Install Python 3.9+ from https://python.org
pause
exit /b 1

:check_venv
echo Found:
%PY% --version

:: Create isolated venv if not exists
if not exist venv (
    echo Creating isolated venv...
    %PY% -m venv venv
    echo venv created
) else (
    echo venv already exists
)

:: Activate venv
echo Activating venv...
call venv\Scripts\activate.bat
echo venv activated

:: Upgrade pip
echo Upgrading pip...
python -m pip install --upgrade pip -q

:: Install dependencies in venv
echo Installing dependencies in venv...
if exist requirements.txt (
    pip install -r requirements.txt
) else (
    pip install fastapi "uvicorn[standard]" playwright pydantic python-dotenv httpx
)
echo Dependencies installed

:: Install playwright browser
echo Checking playwright chromium...
python -c "from playwright.sync_api import sync_playwright" >nul 2>&1
if %errorlevel% neq 0 (
    pip install playwright -q
)
echo Installing chromium browser...
python -m playwright install chromium

:: Verify
echo Verifying installation...
python -c "import fastapi, uvicorn, playwright; print('fastapi', fastapi.__version__)"
python -c "from playwright.sync_api import sync_playwright; print('playwright OK')"
python -m py_compile app.py
if %errorlevel% neq 0 (
    echo app.py syntax error!
    pause
    exit /b 1
)
echo app.py syntax OK

:: Run
echo.
echo ==========================================
echo  Starting Razorpay Headless API (Isolated venv)
echo   Local:    http://127.0.0.1:8000
echo   Network:  http://0.0.0.0:8000
echo   Docs:     http://127.0.0.1:8000/docs
echo   Env:      %CD%\venv (isolated)
echo ==========================================
echo.
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
pause
