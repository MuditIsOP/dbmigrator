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

:: Run Flask server in a new command window
echo Launching backend server...
start "Antigravity Migration Server" cmd /c "python src/server.py"

:: Wait for server to boot up
echo Waiting for server to initialize...
timeout /t 3 /nobreak >nul

:: Open browser
echo Opening frontend in web browser...
start http://localhost:5000

echo Application successfully launched. Keep the command prompt window open.
echo To close, close the server terminal window.
timeout /t 5 >nul
