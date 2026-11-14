# Store the BUSY BDS (Access) password once, encrypted with Windows DPAPI for the current Windows user only.
# bds_sync_export.ps1 reads it so the server can auto-sync without the password in any file or command line.
# Usage (interactive):        powershell -File set_bds_password.ps1
#        (non-interactive):   $env:BUSY_BDS_PWD = '...'; powershell -File set_bds_password.ps1
$dir = Join-Path $env:LOCALAPPDATA 'BusyMobileApp'
New-Item -ItemType Directory -Force $dir | Out-Null
if ($env:BUSY_BDS_PWD) {
    $secure = ConvertTo-SecureString $env:BUSY_BDS_PWD -AsPlainText -Force
} else {
    $secure = Read-Host 'BUSY data password' -AsSecureString
}
$secure | ConvertFrom-SecureString | Set-Content -Path (Join-Path $dir 'bds_password.dat') -Encoding ascii
Write-Host "Saved (DPAPI, user $env:USERNAME): $(Join-Path $dir 'bds_password.dat')"
