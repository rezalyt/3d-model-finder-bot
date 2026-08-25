$ErrorActionPreference = 'Stop'

Write-Host '=== DICOM local AI setup ===' -ForegroundColor Cyan
python --version
if ($LASTEXITCODE -ne 0) { throw 'Python not found. Install Python 3.10-3.12 and reopen PowerShell.' }

if (-not (Test-Path '.venv')) {
  python -m venv .venv
}
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip

Write-Host 'Installing TotalSegmentator and DICOM dependencies...' -ForegroundColor Yellow
python -m pip install -r requirements-local.txt

Write-Host ''
Write-Host 'Checking CUDA availability...' -ForegroundColor Yellow
python -c "import torch; print('Torch:', torch.__version__); print('CUDA available:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host 'Run:' -ForegroundColor Cyan
Write-Host '.\venv\Scripts\python.exe local_ai_runner.py <PATH_TO_DICOM_ZIP>'
