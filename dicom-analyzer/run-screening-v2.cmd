@echo off
setlocal
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo ERROR: .venv not found in %cd%
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python screening_v2_pipeline.py
if errorlevel 1 (
  echo Screening v2 finished with an error code.
) else (
  echo Screening v2 finished.
)
pause
