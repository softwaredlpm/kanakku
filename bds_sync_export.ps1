# Read-only export of BUSY tables to tab-separated files, used by bds_sync.py.
# Source, one of:
#   -Bds <path>                          BUSY BDS (MS Access) file: db.bds or db1YYYY.bds
#   -SqlServer <server> -SqlDatabase <db> BUSY SQL edition database (e.g. BusyComp0001_db12025)
#        -SqlAuth windows|sql  -SqlUser <login>  (SQL password: $env:KANAKKU_SQL_PWD or DPAPI file sql_password.dat)
#   -QueryFile  JSON: [{"name": "tran1", "sql": ["SELECT ...", "fallback SELECT ..."]}, ...]
#   -OutDir     folder for <name>.tsv (or <name>.err when every variant failed)
# BDS password: $env:BUSY_BDS_PWD, else the DPAPI-protected bds_password.dat written by set_bds_password.ps1.
# Values: NULL -> \N, dates -> yyyy-MM-dd HH:mm:ss, booleans -> 0/1, tabs/newlines inside text -> space.
param([string]$Bds, [string]$SqlServer, [string]$SqlDatabase, [string]$SqlAuth = 'windows', [string]$SqlUser = '',
      [Parameter(Mandatory)][string]$QueryFile, [Parameter(Mandatory)][string]$OutDir)
$ErrorActionPreference = 'Stop'

function Read-Secret([string]$envName, [string]$fileName, [string]$hint) {
    $val = [Environment]::GetEnvironmentVariable($envName)
    if ($val) { return $val }
    $f = Join-Path $env:LOCALAPPDATA "BusyMobileApp\$fileName"
    if (-not (Test-Path $f)) { throw "No password: $hint" }
    $secure = (Get-Content $f -Raw).Trim() | ConvertTo-SecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Get-SqlDriver {
    $names = (Get-OdbcDriver -Platform '64-bit' -ErrorAction SilentlyContinue).Name
    foreach ($d in 'ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server', 'SQL Server') { if ($names -contains $d) { return $d } }
    throw 'No SQL Server ODBC driver installed (install "ODBC Driver 18 for SQL Server").'
}

if ($Bds) {
    $pwdEsc = (Read-Secret 'BUSY_BDS_PWD' 'bds_password.dat' 'run set_bds_password.ps1 once (or set BUSY_BDS_PWD).').Replace('}', '}}')
    $connStr = "Driver={Microsoft Access Driver (*.mdb, *.accdb)};Dbq=$Bds;Pwd=$pwdEsc;ReadOnly=1;"
} elseif ($SqlServer -and $SqlDatabase) {
    $auth = 'Trusted_Connection=Yes;'
    if ($SqlAuth -eq 'sql') {
        $p = (Read-Secret 'KANAKKU_SQL_PWD' 'sql_password.dat' 'run set_bds_password.ps1 -Kind sql once (or set KANAKKU_SQL_PWD).').Replace('}', '}}')
        $auth = "Uid=$SqlUser;Pwd={$p};"
    }
    $connStr = "Driver={$(Get-SqlDriver)};Server=$SqlServer;Database=$SqlDatabase;${auth}ApplicationIntent=ReadOnly;Encrypt=Optional;TrustServerCertificate=Yes;Connection Timeout=15;"
} else {
    throw 'Give -Bds <file> or -SqlServer <server> -SqlDatabase <db>.'
}

New-Item -ItemType Directory -Force $OutDir | Out-Null
$inv = [Globalization.CultureInfo]::InvariantCulture
$utf8 = New-Object System.Text.UTF8Encoding($false)

$conn = New-Object System.Data.Odbc.OdbcConnection($connStr)
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
