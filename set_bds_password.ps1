# Store a BUSY data password once, encrypted with Windows DPAPI for the current Windows user only.
# bds_sync_export.ps1 reads it so sync can run unattended without the password in any file or command line.
#   -Kind bds  (default)  BUSY BDS (MS Access) data password   -> bds_password.dat
#   -Kind sql             SQL Server login password (BUSY SQL)   -> sql_password.dat
# Usage (interactive):        powershell -File set_bds_password.ps1 [-Kind sql]
#        (non-interactive):   $env:BUSY_BDS_PWD = '...'; powershell -File set_bds_password.ps1
param([ValidateSet('bds', 'sql')][string]$Kind = 'bds')
$dir = Join-Path $env:LOCALAPPDATA 'BusyMobileApp'
New-Item -ItemType Directory -Force $dir | Out-Null
$envName = if ($Kind -eq 'sql') { 'KANAKKU_SQL_PWD' } else { 'BUSY_BDS_PWD' }
$file = if ($Kind -eq 'sql') { 'sql_password.dat' } else { 'bds_password.dat' }
$plain = [Environment]::GetEnvironmentVariable($envName)
if ($plain) {
    $secure = ConvertTo-SecureString $plain -AsPlainText -Force
} else {
    $secure = Read-Host "$(if ($Kind -eq 'sql') { 'SQL Server login password' } else { 'BUSY data password' })" -AsSecureString
}
$secure | ConvertFrom-SecureString | Set-Content -Path (Join-Path $dir $file) -Encoding ascii
Write-Host "Saved (DPAPI, user $env:USERNAME): $(Join-Path $dir $file)"
