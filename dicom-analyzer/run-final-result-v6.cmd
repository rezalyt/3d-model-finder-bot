@echo off
setlocal
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo ERROR: .venv not found in %cd%
  pause
  exit /b 1
)
if not exist dicom-ai-result\final_screening\final_screening.json (
  echo ERROR: final_screening.json not found
  pause
  exit /b 1
)
if not exist dicom-ai-result\final_visual_review_v5\final_visual_review_v5.json (
  echo ERROR: final_visual_review_v5.json not found
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python final_result_analyzer_v6.py
if errorlevel 1 (
  echo Final result analysis finished with an error code.
) else (
  echo Final result analysis finished.
)
pause
