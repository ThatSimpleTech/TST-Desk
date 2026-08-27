# Packaged-sidecar smoke on a clean Windows guest (TD-4906).
#
# Run on Windows after `npm run tauri:build` in ui/. Proves the bundled sidecar
# serves with no Python on PATH, then runs the protocol stranger loop and a
# keychain probe. Requires Python 3 on the guest for the smoke client only.
#
# Usage: pwsh -File core/scripts/smoke_windows_bundle.ps1 [path-to-tstd.exe]

param(
    [string]$Sidecar = ""
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$E2ePy = Join-Path $Repo "core/scripts/smoke_linux_e2e.py"
if (-not (Test-Path $E2ePy)) { throw "missing $E2ePy" }

if (-not $Sidecar) {
    $candidates = @(
        Get-ChildItem -Path (Join-Path $Repo "shell/target/release/bundle/msi") -Recurse -Filter tstd.exe -ErrorAction SilentlyContinue
    )
    if (-not $candidates) {
        $fallback = Join-Path $Repo "shell/target/release/tstd.exe"
        if (Test-Path $fallback) { $Sidecar = $fallback }
    } else {
        $Sidecar = $candidates[0].FullName
    }
}
if (-not (Test-Path $Sidecar)) { throw "need a built tstd.exe (run tauri:build first)" }

Write-Host "== sidecar with minimal PATH =="
$Data = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([guid]::NewGuid()))
$DataDir = Join-Path $Data "sidecar"
New-Item -ItemType Directory -Path $DataDir | Out-Null
$savedPath = $env:PATH
$env:PATH = "$env:SystemRoot\System32"
$proc = Start-Process $Sidecar -ArgumentList "--data-dir `"$DataDir`" --log-level INFO" -PassThru
$env:PATH = $savedPath
$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-Path (Join-Path $DataDir "port.json")) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 100
}
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
if (-not (Test-Path (Join-Path $DataDir "port.json"))) { throw "bundled sidecar did not serve" }
Write-Host "sidecar ok: $(Get-Content (Join-Path $DataDir 'port.json') -Raw)"

Write-Host "== protocol E2E (bundled tstd + loopback mock) =="
$Ws = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([guid]::NewGuid()))
$Home = Join-Path $Data "home"
New-Item -ItemType Directory -Path $Home | Out-Null
$env:HOME = $Home
git -C $Ws init -q
git -C $Ws -c user.email=smoke@tst.desk -c user.name=smoke commit -q --allow-empty -m baseline
$DaemonDir = Join-Path $Data "daemon"
New-Item -ItemType Directory -Path $DaemonDir | Out-Null
$PortFile = Join-Path $DaemonDir "port.json"
if (Test-Path $PortFile) { Remove-Item $PortFile }

$mock = Start-Process python -ArgumentList $E2ePy, "--serve", "--workspace", $Ws -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 2
$daemon = Start-Process $Sidecar -ArgumentList "--data-dir `"$DaemonDir`" --log-level INFO" -PassThru
$deadline2 = (Get-Date).AddSeconds(45)
while (-not (Test-Path $PortFile) -and (Get-Date) -lt $deadline2) {
    if (-not $daemon.HasExited) { Start-Sleep -Milliseconds 100 } else { throw "tstd exited before port.json" }
}
if (-not (Test-Path $PortFile)) { throw "port.json missing" }

python $E2ePy --client --workspace $Ws --port-file $PortFile
if ($LASTEXITCODE -ne 0) { throw "client smoke failed" }
python $E2ePy --probe-keychain --workspace $Ws --port-file $PortFile
if ($LASTEXITCODE -ne 0) { throw "keychain probe failed" }

Stop-Process -Id $daemon.Id -Force -ErrorAction SilentlyContinue
Stop-Process -Id $mock.Id -Force -ErrorAction SilentlyContinue
Write-Host "clean Windows guest: sidecar + protocol E2E + keychain probe ok"
