@echo off
setlocal
cd /d "%~dp0"
if not defined MINEPATH_PYTHON set "MINEPATH_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%MINEPATH_PYTHON%" (
  echo Python environment missing. Create .venv and install requirements.txt. See README.md.
  pause
  exit /b 1
)
echo Open http://127.0.0.1:8080 after the server starts. Press Ctrl+C to stop.
"%MINEPATH_PYTHON%" "%~dp0run.py" --demo %*
pause
