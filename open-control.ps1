$ErrorActionPreference = "Stop"

$port = if ([string]::IsNullOrWhiteSpace($env:PORT)) { 8765 } else { $env:PORT }
$url = "http://127.0.0.1:$port/control/"

try {
    $health = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$port/health/live" `
        -TimeoutSec 3
    if (-not $health.ok) {
        throw "server is not healthy"
    }
} catch {
    Write-Error "Claude Web API is not running. Start .\start.ps1 first."
    exit 1
}

Start-Process $url
Write-Host "Opened $url"
