# Bootstrap portable Python + deps + Camoufox browser
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$pyDir = Join-Path $PSScriptRoot ".python"
$py = Join-Path $pyDir "python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Downloading portable Python 3.12..."
    $zip = Join-Path $env:TEMP "python-embed.zip"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip" -OutFile $zip -UseBasicParsing
    New-Item -ItemType Directory -Force -Path $pyDir | Out-Null
    Expand-Archive -Path $zip -DestinationPath $pyDir -Force
    $pth = Join-Path $pyDir "python312._pth"
    (Get-Content $pth) | ForEach-Object { if ($_ -match '^#import site') { 'import site' } else { $_ } } | Set-Content $pth
    Add-Content $pth "`nLib\site-packages"
    $getpip = Join-Path $env:TEMP "get-pip.py"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip -UseBasicParsing
    & $py $getpip
}

& $py -m pip install -U pip
& $py -m pip install -r requirements.txt
& $py -m camoufox fetch

Write-Host ""
Write-Host "OK. Start:  .\start.ps1"
Write-Host "API: http://127.0.0.1:8765"
