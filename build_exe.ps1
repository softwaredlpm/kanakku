# Build dist\Kanakku.exe (single file, no console window). Needs: py -m pip install --user pyinstaller
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
py -m py_compile kanakku_app.py server.py bds_sync.py auth.py
$hidden = 'reports_accounts', 'reports_inventory', 'reports_analysis', 'reports_gst', 'reports_batch', 'reports_registers' |
    ForEach-Object { '--hidden-import', $_ }
py -m PyInstaller --noconfirm --clean --onefile --windowed --name Kanakku `
    --add-data 'BusyMobileWeb\index.html;BusyMobileWeb' `
    --add-data 'bds_sync_export.ps1;.' `
    --add-data 'set_bds_password.ps1;.' `
    @hidden kanakku_app.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
Get-Item dist\Kanakku.exe | ForEach-Object { "Built {0} ({1:N1} MB)" -f $_.FullName, ($_.Length / 1MB) }
