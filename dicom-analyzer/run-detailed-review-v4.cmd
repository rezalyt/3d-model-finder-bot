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
python -c "import nibabel, scipy, dicom2nifti, pydicom; from PIL import Image; print('v4 dependencies: OK')"
if errorlevel 1 (
  echo Missing v4 dependencies. Run: .venv\Scripts\python.exe -m pip install nibabel scipy dicom2nifti pydicom Pillow
  pause
  exit /b 1
)
python detailed_review_v4.py source.zip
if errorlevel 1 (
  echo Detailed review v4 finished with an error code.
) else (
  echo Detailed review v4 finished.
)
pause
