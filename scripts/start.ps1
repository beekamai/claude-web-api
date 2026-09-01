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
# Windows PowerShell turns any stderr from a native command into a terminating
# error under Stop, hiding the traceback; run the probe under Continue and
# judge it by its exit code instead.
$ErrorActionPreference = "Continue"
$probe = & $py -c "from camoufox.pkgman import installed_verstr; print(installed_verstr())" 2>&1
$probeExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($probeExit -ne 0) {
    $probe | ForEach-Object { Write-Host $_ }
    Write-Error "Camoufox browser is missing or broken in $projectRoot (see above). Run setup.cmd here."
    exit 1
}
# stdout arrives as strings, stderr as error records; the version is stdout.
$browserVersion = ($probe | Where-Object { $_ -is [string] } | Select-Object -Last 1)
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
