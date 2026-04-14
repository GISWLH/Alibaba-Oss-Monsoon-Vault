$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name OssDrivePreview `
  --add-data "oss_drive\\assets;oss_drive\\assets" `
  --add-data "oss_drive_config.json;." `
  run_oss_drive.py

Copy-Item ".\\oss_drive_config.json" ".\\dist\\OssDrivePreview\\oss_drive_config.json" -Force
Copy-Item ".\\README.md" ".\\dist\\OssDrivePreview\\README.md" -Force

Write-Host ""
Write-Host "Build finished."
Write-Host "EXE path: $root\\dist\\OssDrivePreview\\OssDrivePreview.exe"
