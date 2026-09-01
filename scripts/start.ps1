# Start Claude Web API (portable Python + Camoufox supervisor)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
$py = Join-Path $projectRoot ".python\python.exe"
if (-not (Test-Path $py)) {
    $hint = "Portable Python is missing in $projectRoot. Run setup.cmd here"
    $installed = Join-Path $env:SystemDrive "claude-web-api"
    if (($installed -ne $projectRoot) -and (Test-Path (Join-Path $installed ".python\python.exe"))) {
        # The installer defaults to this directory, so a copy unpacked
        # elsewhere is usually a second, unprovisioned checkout.
        $hint += ", or start the installed copy: $installed\start.cmd"
    }
    Write-Error "$hint."
    exit 1
}
$browserVersion = & $py -c "from camoufox.pkgman import installed_verstr; print(installed_verstr())" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Camoufox browser missing. Run .\scripts\setup.ps1 first."
    exit 1
}
$env:PYTHONPATH = Join-Path $projectRoot "src"
if ([string]::IsNullOrWhiteSpace($env:CLAUDE_HEADLESS)) {
    $env:CLAUDE_HEADLESS = "1"
}
$displayPort = if ([string]::IsNullOrWhiteSpace($env:PORT)) { 8765 } else { $env:PORT }
Write-Host "Starting supervised server on http://127.0.0.1:$displayPort"
Write-Host "Control center: http://127.0.0.1:$displayPort/control/"
Write-Host "Camoufox $browserVersion"
Write-Host "Main Camoufox: $(if ($env:CLAUDE_HEADLESS -eq '1') { 'headless' } else { 'visible' })"
Write-Host "Use the control panel to open a visible login window when needed."
& (Join-Path $PSScriptRoot "supervise.ps1") -PythonPath $py
