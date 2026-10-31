@echo off
REM Easy YT To Anything — one-click launcher for Windows.
cd /d "%~dp0"
echo Installing/updating dependencies...
python -m pip install --upgrade -r requirements.txt
echo.
echo Starting server... a browser tab will open automatically.
python app.py
pause
