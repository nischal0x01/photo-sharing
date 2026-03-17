@echo off
setlocal

cd /d "%~dp0"

echo Starting Secure Client Gallery...

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment (.venv)...
  py -3 -m venv .venv
)

call .venv\Scripts\activate

echo Ensuring dependencies are installed...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo Launching app...
python main.py

pause
