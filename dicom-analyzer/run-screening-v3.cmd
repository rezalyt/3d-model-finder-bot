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
python -c "import nibabel, pydicom, dicom2nifti; from PIL import Image; print('v3 dependencies: OK')"
if errorlevel 1 (
  echo Missing v3 dependencies. Run: python -m pip install nibabel scipy dicom2nifti Pillow
  pause
  exit /b 1
)
python screening_v3_pipeline.py source.zip
if errorlevel 1 (
  echo Screening v3 finished with an error code.
) else (
  echo Screening v3 finished.
)
pause
