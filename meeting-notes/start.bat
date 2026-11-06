@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First-time setup...
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -r requirements.txt || goto :err
)
set /p TITLE=Meeting name (Enter for "meeting"):
if "%TITLE%"=="" set TITLE=meeting
.venv\Scripts\python meeting_notes.py --title "%TITLE%" %*
echo.
pause
exit /b
:err
echo Setup failed.
pause
