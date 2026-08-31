# Start Claude Web API (portable Python + Camoufox supervisor)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".python\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "Portable Python missing. Re-run setup."
    exit 1
}
$browserVersion = & $py -c "from camoufox.pkgman import installed_verstr; print(installed_verstr())" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Camoufox browser missing. Run .\setup.ps1 first."
    exit 1
}
$env:PYTHONPATH = $PSScriptRoot
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
