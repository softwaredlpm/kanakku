# Read-only export of BUSY BDS tables to tab-separated files, used by bds_sync.py.
#   -Bds        full path of a BUSY .bds file (db.bds or db1YYYY.bds)
#   -QueryFile  JSON: [{"name": "tran1", "sql": ["SELECT ...", "fallback SELECT ..."]}, ...]
#   -OutDir     folder for <name>.tsv (or <name>.err when every variant failed)
# Password: $env:BUSY_BDS_PWD, else the DPAPI-protected file written by set_bds_password.ps1.
# Values: NULL -> \N, dates -> yyyy-MM-dd HH:mm:ss, booleans -> 0/1, tabs/newlines inside text -> space.
param([Parameter(Mandatory)][string]$Bds, [Parameter(Mandatory)][string]$QueryFile, [Parameter(Mandatory)][string]$OutDir)
$ErrorActionPreference = 'Stop'

$pwdPlain = $env:BUSY_BDS_PWD
if (-not $pwdPlain) {
    $pwdFile = Join-Path $env:LOCALAPPDATA 'BusyMobileApp\bds_password.dat'
    if (-not (Test-Path $pwdFile)) { throw "No BDS password: run set_bds_password.ps1 once (or set BUSY_BDS_PWD)." }
    $secure = (Get-Content $pwdFile -Raw).Trim() | ConvertTo-SecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $pwdPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
$pwdEsc = $pwdPlain.Replace('}', '}}')
New-Item -ItemType Directory -Force $OutDir | Out-Null
$inv = [Globalization.CultureInfo]::InvariantCulture
$utf8 = New-Object System.Text.UTF8Encoding($false)

$conn = New-Object System.Data.Odbc.OdbcConnection("Driver={Microsoft Access Driver (*.mdb, *.accdb)};Dbq=$Bds;Pwd=$pwdEsc;ReadOnly=1;")
$conn.Open()
try {
    foreach ($q in (Get-Content $QueryFile -Raw | ConvertFrom-Json)) {
        $done = $false; $lastErr = ''
        foreach ($sql in @($q.sql)) {
            $tmp = Join-Path $OutDir "$($q.name).tsv.part"
            $w = New-Object System.IO.StreamWriter($tmp, $false, $utf8)
            try {
                $cmd = $conn.CreateCommand(); $cmd.CommandText = $sql; $cmd.CommandTimeout = 1800
                $r = $cmd.ExecuteReader()
                $n = $r.FieldCount
                $w.WriteLine(((0..($n - 1)) | ForEach-Object { $r.GetName($_) }) -join "`t")
                $rows = 0
                while ($r.Read()) {
                    $vals = New-Object string[] $n
                    for ($i = 0; $i -lt $n; $i++) {
                        if ($r.IsDBNull($i)) { $vals[$i] = '\N'; continue }
                        $v = $r.GetValue($i)
                        if ($v -is [double] -or $v -is [single] -or $v -is [decimal]) { $vals[$i] = ([double]$v).ToString('R', $inv) }
                        elseif ($v -is [datetime]) { $vals[$i] = $v.ToString('yyyy-MM-dd HH:mm:ss', $inv) }
                        elseif ($v -is [bool]) { $vals[$i] = [string][int]$v }
                        else { $vals[$i] = ([string]$v) -replace "[`t`r`n]", ' ' }
                    }
                    $w.WriteLine($vals -join "`t"); $rows++
                }
                $r.Close(); $w.Close()
                Move-Item -Force $tmp (Join-Path $OutDir "$($q.name).tsv")
                Write-Host "$($q.name): $rows rows"
                $done = $true; break
            } catch {
                $w.Close(); Remove-Item -Force $tmp -ErrorAction SilentlyContinue
                $lastErr = $_.Exception.Message
            }
        }
        if (-not $done) {
            [IO.File]::WriteAllText((Join-Path $OutDir "$($q.name).err"), $lastErr, $utf8)
            Write-Host "$($q.name): ERROR $lastErr"
        }
    }
} finally { $conn.Close() }
