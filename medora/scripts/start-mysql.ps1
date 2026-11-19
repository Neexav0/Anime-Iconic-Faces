# Starts Medora's local MySQL server (user-space, no Windows service, localhost only).
# Data lives outside OneDrive in %USERPROFILE%\medora-mysql so syncing can't corrupt it.
$ErrorActionPreference = "Stop"
$root = Join-Path $env:USERPROFILE "medora-mysql"
$mysqld = Get-ChildItem "C:\Program Files\MySQL" -Recurse -Filter mysqld.exe | Select-Object -First 1 -ExpandProperty FullName
if (-not $mysqld) { throw "mysqld.exe not found - is MySQL installed?" }
$ini = Join-Path $root "my.ini"
if (-not (Test-Path (Join-Path $root "data"))) {
    New-Item -ItemType Directory -Force $root | Out-Null
    @"
[mysqld]
basedir="$((Split-Path (Split-Path $mysqld)) -replace '\\','/')"
datadir="$(($root -replace '\\','/'))/data"
bind-address=127.0.0.1
port=3306
character-set-server=utf8mb4
collation-server=utf8mb4_0900_ai_ci
"@ | Set-Content -Encoding ascii $ini
    & $mysqld --defaults-file="$ini" --initialize-insecure --console
}
Write-Host "Starting MySQL on 127.0.0.1:3306 (Ctrl+C to stop)..."
& $mysqld --defaults-file="$ini" --console
