<#
.SYNOPSIS
  One-off pg_dump of the local karnex_db, in AWS/RDS-ready form.

.DESCRIPTION
  Produces a CUSTOM-format dump (-Fc) with ownership and GRANTs stripped
  (--no-owner --no-acl), because an RDS master user is not a superuser and a
  dump that references the local "postgres" owner fails half-way through the
  restore with permission errors.

  It then VERIFIES the file with "pg_restore --list" before reporting success.
  A dump that has never been parsed is a hope, not a backup.

  Credentials come from .env (DB_HOST / DB_PORT / DB_NAME / DB_USER /
  DB_PASSWORD, or CRM_DATABASE_URL / AUTH_DB_URL if those are set). The
  password is passed to pg_dump via the PGPASSWORD environment variable of the
  child process only. It is never printed, logged, or put on a command line.

  NOTE: this file is deliberately pure ASCII. PowerShell 5.1 reads a BOM-less
  script as ANSI, so a UTF-8 em-dash arrives as bytes that include U+201D,
  which PowerShell honours as a string delimiter. Keep it ASCII.

.EXAMPLE
  cd F:\AI-Interview-Model-B-V2
  powershell -ExecutionPolicy Bypass -File scripts\dump_karnex_for_aws.ps1

.EXAMPLE
  .\scripts\dump_karnex_for_aws.ps1 -OutDir D:\migration -AlsoPlainSql
#>

[CmdletBinding()]
param(
    [string] $OutDir       = "F:\KarnexBackups\aws-migration",
    [string] $Database     = "",       # default: DB_NAME from .env
    [string] $PgBin        = "",       # default: PG_BIN from .env, else PATH, else Program Files
    [switch] $AlsoPlainSql,            # additionally write a readable .sql
    [switch] $SchemaOnly,              # structure only, no rows
    [switch] $DataOnly                 # rows only, no structure
)

# Native tools write progress to stderr; 'Stop' would turn that into an
# exception. Exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'

function Info ($m) { Write-Host "  $m" }
function Ok   ($m) { Write-Host "  $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  $m" -ForegroundColor Yellow }
function Fail ($m) {
    Write-Host ""
    Write-Host "FAILED: $m" -ForegroundColor Red
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    exit 1
}

# ---------------------------------------------------------------- repo root
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $repo 'backend\main.py'))) {
    Fail "scripts\ must sit inside the AI-Interview-Model-B-V2 folder (looked in $repo)."
}

# ---------------------------------------------------------------- .env
$envMap = @{}
$envFile = Join-Path $repo '.env'
if (Test-Path $envFile) {
    foreach ($line in (Get-Content $envFile)) {
        $t = $line.Trim()
        if ($t -and (-not $t.StartsWith('#')) -and $t.Contains('=')) {
            $pair = $t.Split('=', 2)
            $envMap[$pair[0].Trim()] = $pair[1].Trim()
        }
    }
}
function EnvVal ($key, $fallback) {
    if ($envMap.ContainsKey($key) -and $envMap[$key]) { return $envMap[$key] }
    $fromProc = [Environment]::GetEnvironmentVariable($key)
    if ($fromProc) { return $fromProc }
    return $fallback
}

$dbHost = EnvVal 'DB_HOST' 'localhost'
$dbPort = EnvVal 'DB_PORT' '5432'
$dbName = EnvVal 'DB_NAME' 'karnex_db'
$dbUser = EnvVal 'DB_USER' 'postgres'
$dbPass = EnvVal 'DB_PASSWORD' ''

# A full URL wins over the DB_* parts, matching scripts\backup_karnex.py.
$url = EnvVal 'CRM_DATABASE_URL' (EnvVal 'AUTH_DB_URL' '')
if ($url) {
    $clean = $url -replace '^([a-z]+)\+[a-z0-9]+://', '$1://'
    try {
        $u = [Uri] $clean
        if ($u.Host) {
            $dbHost = $u.Host
            if ($u.Port -gt 0) { $dbPort = "$($u.Port)" }
            $dbName = $u.AbsolutePath.TrimStart('/')
            if ($u.UserInfo) {
                $ui = $u.UserInfo.Split(':', 2)
                $dbUser = [Uri]::UnescapeDataString($ui[0])
                if ($ui.Count -gt 1) { $dbPass = [Uri]::UnescapeDataString($ui[1]) }
            }
        }
    } catch {
        Warn "Could not parse the database URL; falling back to the DB_* values."
    }
}
if ($Database) { $dbName = $Database }

# ---------------------------------------------------------------- pg tools
function Find-PgTool ($name) {
    $exe = "$name.exe"
    foreach ($dir in @($PgBin, (EnvVal 'PG_BIN' ''))) {
        if ($dir -and (Test-Path (Join-Path $dir $exe))) { return (Join-Path $dir $exe) }
    }
    $onPath = Get-Command $name -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    foreach ($base in @('C:\Program Files\PostgreSQL', 'C:\Program Files (x86)\PostgreSQL')) {
        if (Test-Path $base) {
            # Highest installed major version first: pg_dump must be >= the server.
            $dirs = Get-ChildItem $base -Directory |
                    Sort-Object { [int]($_.Name -replace '\D', '0') } -Descending
            foreach ($d in $dirs) {
                $cand = Join-Path $d.FullName "bin\$exe"
                if (Test-Path $cand) { return $cand }
            }
        }
    }
    return $null
}

$pgDump    = Find-PgTool 'pg_dump'
$pgRestore = Find-PgTool 'pg_restore'
$psql      = Find-PgTool 'psql'
if (-not $pgDump) {
    Fail "pg_dump.exe not found. Install the PostgreSQL client tools, or pass -PgBin 'C:\Program Files\PostgreSQL\16\bin'."
}
if (-not $pgRestore) {
    Fail "pg_restore.exe not found next to pg_dump; it is needed to verify the dump."
}

Write-Host ""
Write-Host "Karnex -> AWS dump" -ForegroundColor Cyan
Write-Host "------------------"
Info "database : $dbName on ${dbHost}:${dbPort} as $dbUser"
Info "pg_dump  : $pgDump"

if ($dbPass) { $env:PGPASSWORD = $dbPass }
else { Warn "No DB_PASSWORD found; relying on pgpass or trust auth." }

# ------------------------------------------------- version + reachability check
$dumpVer = (& $pgDump --version) -join ' '
$dumpMaj = 0
$m = [regex]::Match($dumpVer, '(\d+)\.')
if (-not $m.Success) { $m = [regex]::Match($dumpVer, '(\d+)') }
if ($m.Success) { $dumpMaj = [int]$m.Groups[1].Value }
Info "client   : $dumpVer"

if ($psql) {
    $srvRaw = & $psql -h $dbHost -p $dbPort -U $dbUser -d $dbName -tAc 'SHOW server_version;' 2>&1
    if ($LASTEXITCODE -ne 0) {
        Fail "Cannot connect to '$dbName'. psql said: $($srvRaw -join ' ')"
    }
    $srv = ($srvRaw | Select-Object -First 1).ToString().Trim()
    $sm  = [regex]::Match($srv, '^(\d+)')
    $srvMaj = 0
    if ($sm.Success) { $srvMaj = [int]$sm.Groups[1].Value }
    Info "server   : PostgreSQL $srv"
    if ($dumpMaj -gt 0 -and $srvMaj -gt 0 -and $dumpMaj -lt $srvMaj) {
        Fail "pg_dump is version $dumpMaj but the server is $srvMaj. pg_dump must be the same major version or newer, or the dump will be incomplete. Install the matching client tools and pass -PgBin."
    }
    $sizeRaw = & $psql -h $dbHost -p $dbPort -U $dbUser -d $dbName -tAc "SELECT pg_size_pretty(pg_database_size('$dbName'));" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Info "size     : $(($sizeRaw | Select-Object -First 1).ToString().Trim()) on disk"
    }
} else {
    Warn "psql not found; skipping the version and size pre-check."
}

# ---------------------------------------------------------------- output path
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$tag = ''
if ($SchemaOnly) { $tag = '-schema' }
if ($DataOnly)   { $tag = '-data' }
$dumpPath = Join-Path $OutDir "$dbName$tag-$stamp.dump"
$logPath  = Join-Path $OutDir "$dbName$tag-$stamp.log"

# ---------------------------------------------------------------- dump
$dumpArgs = @(
    '-h', $dbHost, '-p', $dbPort, '-U', $dbUser, '-d', $dbName,
    '-Fc',                 # custom format: compressed, selectively restorable
    '--no-owner',          # the RDS master user is not a superuser
    '--no-acl',            # ...and cannot re-grant to local roles
    '--verbose',
    '-f', $dumpPath
)
if ($SchemaOnly) { $dumpArgs += '--schema-only' }
if ($DataOnly)   { $dumpArgs += '--data-only' }

Write-Host ""
Info "dumping to $dumpPath ..."
& $pgDump @dumpArgs 2> $logPath
$rc = $LASTEXITCODE
if ($rc -ne 0) {
    Warn "pg_dump log tail:"
    Get-Content $logPath -Tail 15 | ForEach-Object { Write-Host "    $_" }
    Fail "pg_dump exited $rc. Full log: $logPath"
}
if (-not (Test-Path $dumpPath)) { Fail "pg_dump reported success but wrote no file." }

$bytes = (Get-Item $dumpPath).Length
if ($bytes -lt 1024) { Fail "The dump is only $bytes bytes, which is not a real database. See $logPath" }

# ---------------------------------------------------------------- verify
Info "verifying with pg_restore --list ..."
$toc = & $pgRestore --list $dumpPath 2>&1
if ($LASTEXITCODE -ne 0) {
    Fail "pg_restore could not read the dump; the file is corrupt. $($toc -join ' ')"
}
$tables = ($toc | Select-String -Pattern 'TABLE DATA').Count
$total  = ($toc | Select-String -Pattern '^\d+;').Count
if ($total -lt 10) {
    Fail "The dump table of contents has only $total entries. Refusing to call that a backup."
}

$mb = [math]::Round($bytes / 1MB, 2)
Write-Host ""
Ok "Dump verified."
Info "file    : $dumpPath"
Info "size    : $mb MB"
Info "objects : $total (of which $tables tables with data)"
Info "log     : $logPath"

# ---------------------------------------------------------------- optional .sql
if ($AlsoPlainSql) {
    $sqlPath = Join-Path $OutDir "$dbName$tag-$stamp.sql"
    Write-Host ""
    Info "also writing plain SQL to $sqlPath ..."
    & $pgRestore --no-owner --no-acl -f $sqlPath $dumpPath
    if ($LASTEXITCODE -ne 0) {
        Warn "Plain-SQL conversion failed. The .dump above is still good."
    } else {
        Ok "Plain SQL written ($([math]::Round((Get-Item $sqlPath).Length / 1MB, 2)) MB)."
    }
}

Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Next: restore onto AWS RDS" -ForegroundColor Cyan
Write-Host "  1. Create the empty database:"
Write-Host "       psql -h ENDPOINT -U MASTERUSER -d postgres -c ""CREATE DATABASE $dbName;"""
Write-Host "  2. Restore:"
Write-Host "       pg_restore --no-owner --no-acl -j 4 -h ENDPOINT -U MASTERUSER -d $dbName ""$dumpPath"""
Write-Host "  3. Check row counts, then point CRM_DATABASE_URL at RDS."
Write-Host ""
Write-Host "  This dump is the database only. Uploaded files (resumes, attachments,"
Write-Host "  PO documents) live on disk under data\ and must be copied separately;"
Write-Host "  scripts\backup_karnex.py zips them if you want that half too."
Write-Host ""
