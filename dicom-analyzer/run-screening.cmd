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
python -c "import nibabel, scipy, dicom2nifti; print('Screening dependencies: OK')"
if errorlevel 1 (
  echo Missing screening dependencies. Run: python -m pip install -r requirements-local.txt
  pause
  exit /b 1
)
python screening_pipeline.py source.zip
if errorlevel 1 (
  echo Screening finished with an error code.
) else (
  echo Screening finished.
)
pause
