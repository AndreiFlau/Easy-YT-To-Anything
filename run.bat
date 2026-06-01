@echo off
REM Easy YT To Anything — one-click launcher for Windows.
cd /d "%~dp0"
echo Installing/updating dependencies...
python -m pip install -r requirements.txt
echo.
echo Starting server... a browser tab will open at http://127.0.0.1:5000
start "" http://127.0.0.1:5000
python app.py
pause
