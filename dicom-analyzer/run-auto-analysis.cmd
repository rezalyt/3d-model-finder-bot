@echo off
setlocal
cd /d %~dp0
if not exist source.zip (
  echo ERROR: source.zip not found in %cd%
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  echo ERROR: .venv not found in %cd%
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python auto_pipeline.py source.zip
if errorlevel 1 (
  echo Pipeline finished with an error code.
) else (
  echo Pipeline finished.
)
pause
