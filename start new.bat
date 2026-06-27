@echo off
echo Starting Antigravity SQL Migration Server...
cd /d "%~dp0"

:: Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed or not in system PATH.
    echo Please install Python 3.8+ to run this application.
    pause
    exit /b
)

:: Set up virtual environment if it doesn't exist
if not exist .venv (
    echo Virtual environment not found. Creating virtual environment .venv...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo Error: Failed to create virtual environment.
        echo Please ensure you have permission to write to this directory.
        pause
        exit /b
    )
    echo Installing dependencies from requirements.txt...
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo Error: Failed to install required libraries.
        pause
        exit /b
    )
)

:: Run Flask server in a new command window
echo Launching backend server...
start "Antigravity Migration Server" cmd /k ".venv\Scripts\python src/server.py"

:: Wait for server to boot up
echo Waiting for server to initialize...
ping 127.0.0.1 -n 4 >nul

:: Open browser
echo Opening frontend in web browser...
start http://localhost:5000

echo Application successfully launched. Keep the command prompt window open.
echo To close, close the server terminal window.
ping 127.0.0.1 -n 6 >nul
