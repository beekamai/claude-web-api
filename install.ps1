# One-file installer: fetch the repository, build the portable runtime, start.
#
#   irm https://raw.githubusercontent.com/beekamai/claude-web-api/main/install.ps1 | iex
#
# Re-running it updates an existing installation in place. Nothing is installed
# system-wide and nothing under the install directory is overwritten except the
# tracked source: the browser profile, control_config.json and the journal stay.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repo = if ($env:CLAUDE_WEB_API_REPO) { $env:CLAUDE_WEB_API_REPO } else { "beekamai/claude-web-api" }
$branch = if ($env:CLAUDE_WEB_API_BRANCH) { $env:CLAUDE_WEB_API_BRANCH } else { "main" }
function Test-AsciiPath($path) { return ($path -notmatch '[^\x00-\x7F]') }
# The portable Python, pip and the Camoufox launcher do not all survive a
# non-ASCII directory (a Cyrillic user name gives a Cyrillic home), so such
# homes fall back to the system drive root.
$defaultDir = if (Test-AsciiPath $HOME) { Join-Path $HOME "claude-web-api" } else { Join-Path $env:SystemDrive "claude-web-api" }
# Run from inside an unpacked copy of the repository, install right there:
# otherwise the user ends up with a working copy in one place and the
# scripts they keep launching in another.
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pyproject.toml")) -and (Test-AsciiPath $PSScriptRoot)) {
    $defaultDir = $PSScriptRoot
}
$target = if ($env:CLAUDE_WEB_API_DIR) { $env:CLAUDE_WEB_API_DIR } else { $defaultDir }
if (-not (Test-AsciiPath $target)) {
    throw "The install path '$target' contains non-ASCII characters; set `$env:CLAUDE_WEB_API_DIR to a plain-ASCII path such as C:\claude-web-api."
}
# Archives and get-pip.py are staged here rather than in %TEMP%: a Cyrillic
# user directory reaches Windows PowerShell as an 8.3 short name that
# Remove-Item then cannot find.
$staging = Join-Path $env:SystemDrive "Temp\claude-web-api-install"
New-Item -ItemType Directory -Force -Path $staging | Out-Null
$env:TEMP = $staging
$env:TMP = $staging
$autostart = $env:CLAUDE_WEB_API_NO_START -ne "1"

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }

if ($PSVersionTable.PSVersion.Major -lt 5) {
    throw "PowerShell 5 or newer is required (found $($PSVersionTable.PSVersion))."
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "The portable Python and Camoufox builds used here are 64-bit only."
}

$git = Get-Command git -ErrorAction SilentlyContinue

Step "Fetching $repo ($branch) into $target"
if (Test-Path (Join-Path $target ".git")) {
    if (-not $git) { throw "$target is a git checkout but git is not on PATH." }
    Push-Location $target
    try {
        # A local edit must not be silently discarded, so an unclean tree stops
        # the update rather than being reset.
        if (& git status --porcelain) {
            Note "Local changes found; skipping the update and keeping them."
        } else {
            & git fetch --depth 1 origin $branch
            & git checkout -q $branch
            & git reset -q --hard "origin/$branch"
        }
    } finally { Pop-Location }
} elseif ($git -and -not (Test-Path $target)) {
    & git clone --depth 1 --branch $branch "https://github.com/$repo" $target
} elseif (Test-Path (Join-Path $target "pyproject.toml")) {
    Note "Using the unpacked copy already in $target (no git metadata, so it is not updated)."
} else {
    # No git, or a directory that is not a checkout: fall back to the archive.
    $archive = Join-Path $staging "claude-web-api-$branch.zip"
    $unpacked = Join-Path $staging "unpack"
    Invoke-WebRequest -Uri "https://codeload.github.com/$repo/zip/refs/heads/$branch" -OutFile $archive -UseBasicParsing
    if (Test-Path $unpacked) { Remove-Item -Recurse -Force $unpacked -ErrorAction SilentlyContinue }
    Expand-Archive -Path $archive -DestinationPath $unpacked -Force
    $source = Get-ChildItem -Directory $unpacked | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    # Copy the tracked tree only; a previous run's profile and config live in
    # the target and must survive an update.
    Copy-Item -Path (Join-Path $source.FullName "*") -Destination $target -Recurse -Force
    Remove-Item -Recurse -Force $unpacked -ErrorAction SilentlyContinue
    Remove-Item -Force $archive -ErrorAction SilentlyContinue
}

Set-Location $target

Step "Building the portable runtime (Python 3.12, dependencies, Camoufox)"
Note "This downloads ~500 MB on a first run."
$global:LASTEXITCODE = 0
& (Join-Path $target "scripts\setup.ps1")
if ($LASTEXITCODE -ne 0) { throw "setup.ps1 failed with exit code $LASTEXITCODE." }

Write-Host ""
Step "Installed in $target"
Write-Host "    Start:         .\scripts\start.ps1"
Write-Host "    Control panel: http://127.0.0.1:8765/control/"
Write-Host "    Connect a client: the panel's Connect tab configures Claude Code or OpenClaude."
Write-Host ""

if ($autostart) {
    Step "Starting the bridge"
    Note "The first run opens a visible browser: log into claude.ai there."
    & (Join-Path $target "scripts\start.ps1")
}
