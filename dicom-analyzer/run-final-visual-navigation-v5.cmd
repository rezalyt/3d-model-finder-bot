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
python -c "import nibabel, pydicom, dicom2nifti, numpy, PIL; print('V5 dependencies: OK')"
if errorlevel 1 (
  echo Missing V5 dependencies.
  pause
  exit /b 1
)
python final_visual_navigation_v5.py source.zip
if errorlevel 1 (
  echo V5 finished with an error code.
) else (
  echo V5 finished.
)
pause
