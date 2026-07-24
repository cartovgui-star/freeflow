@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Whisperflow is not installed yet. Running setup...
  call setup.bat
  exit /b
)
start "" ".venv\Scripts\pythonw.exe" "whisperflow.py"
