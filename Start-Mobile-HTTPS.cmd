@echo off
setlocal
cd /d "%~dp0"
if not defined MINEPATH_PYTHON set "MINEPATH_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%MINEPATH_PYTHON%" (
  echo Python environment missing. Create .venv and install requirements.txt. See README.md.
  pause
  exit /b 1
)
if not exist "%~dp0data\tls\server.pem" "%MINEPATH_PYTHON%" "%~dp0tools\create_local_tls.py"
if not exist "%~dp0data\tls\server.pem" exit /b 1
echo Use a certificate-listed LAN IP on port 8443. Read docs/MOBILE.md first.
echo Trust local-ca.pem on your test device. Never share private keys.
"%MINEPATH_PYTHON%" "%~dp0run.py" --tls --host 0.0.0.0 --port 8443 %*
pause
