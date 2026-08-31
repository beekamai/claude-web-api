[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet("Check", "Install", "Rollback")]
    [string] $Mode = "Install",

    [string] $SourcePath,

    [string] $BackupRoot = (
        Join-Path ([Environment]::GetFolderPath("UserProfile")) `
            ".openclaude\patch-backups"
    ),

    [string] $BackupId,

    [switch] $KeepSourcePatch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:IntegrationRoot = $PSScriptRoot
$script:ManifestPath = Join-Path $script:IntegrationRoot "MANIFEST.json"
$script:Manifest = Get-Content -LiteralPath $script:ManifestPath -Raw |
    ConvertFrom-Json
$script:PatchPath = Join-Path `
    $script:IntegrationRoot `
    $script:Manifest.patch.file
$script:SourceRoot = $null
$script:GitSafeDirectory = $null

function Assert-Command {
    param([Parameter(Mandatory)][string] $Name)

    if (-not (Get-Command -Name $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found on PATH."
    }
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string] $FilePath,
        [Parameter(Mandatory)][string[]] $Arguments,
        [string] $WorkingDirectory,
        [switch] $Capture
    )

    if ($WorkingDirectory) {
        Push-Location -LiteralPath $WorkingDirectory
    }

    try {
        if ($Capture) {
            $output = @(& $FilePath @Arguments)
            $exitCode = $LASTEXITCODE
            if ($exitCode -ne 0) {
                throw (
                    "Command failed with exit code {0}: {1} {2}" -f
                    $exitCode,
                    $FilePath,
                    ($Arguments -join " ")
                )
            }
            return ($output -join "`n").Trim()
        }

        & $FilePath @Arguments
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            throw (
                "Command failed with exit code {0}: {1} {2}" -f
                $exitCode,
                $FilePath,
                ($Arguments -join " ")
            )
        }
    }
    finally {
        if ($WorkingDirectory) {
            Pop-Location
        }
    }
}

function Invoke-Git {
    param(
        [Parameter(Mandatory)][string[]] $Arguments,
        [switch] $Capture
    )

    $gitArguments = @(
        "-c",
        "safe.directory=$script:GitSafeDirectory",
        "-C",
        $script:SourceRoot
    ) + $Arguments

    return Invoke-Native `
        -FilePath "git" `
        -Arguments $gitArguments `
        -Capture:$Capture
}

function Test-Git {
    param([Parameter(Mandatory)][string[]] $Arguments)

    $gitArguments = @(
        "-c",
        "safe.directory=$script:GitSafeDirectory",
        "-C",
        $script:SourceRoot
    ) + $Arguments

    & git @gitArguments *> $null
    return ($LASTEXITCODE -eq 0)
}

function Get-Sha256 {
    param([Parameter(Mandatory)][string] $Path)

    return (
        Get-FileHash -LiteralPath $Path -Algorithm SHA256
    ).Hash.ToLowerInvariant()
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)] $Value
    )

    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    $temporaryPath = "{0}.tmp.{1}" -f $Path, [guid]::NewGuid().ToString("N")
    $json = $Value | ConvertTo-Json -Depth 30
    $utf8 = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText(
        $temporaryPath,
        $json + [Environment]::NewLine,
        $utf8
    )
    Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
}

function Set-ObjectProperty {
    param(
        [Parameter(Mandatory)] $Object,
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)] $Value
    )

    if ($Object.PSObject.Properties.Name -contains $Name) {
        $Object.$Name = $Value
    }
    else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Get-PropertySnapshot {
    param(
        [Parameter(Mandatory)] $Object,
        [Parameter(Mandatory)][string] $Name
    )

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return [ordered]@{
            existed = $false
            value = $null
        }
    }

    return [ordered]@{
        existed = $true
        value = $property.Value
    }
}

function Initialize-Source {
    param([Parameter(Mandatory)][string] $Path)

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    $script:SourceRoot = $resolved.Path
    $script:GitSafeDirectory = $resolved.Path.Replace("\", "/")
}

function Get-WorkingBlobHash {
    param([Parameter(Mandatory)][string] $RelativePath)

    return Invoke-Git `
        -Arguments @(
            "hash-object",
            "--path=$RelativePath",
            $RelativePath
        ) `
        -Capture
}

function Get-TargetState {
    $baseCount = 0
    $patchedCount = 0
    $invalid = [System.Collections.Generic.List[string]]::new()

    foreach ($target in $script:Manifest.targetFiles) {
        $fullPath = Join-Path $script:SourceRoot $target.path
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            $invalid.Add("$($target.path) (missing)")
            continue
        }

        $actual = Get-WorkingBlobHash -RelativePath $target.path
        if ($actual -eq $target.baseGitBlobSha1) {
            $baseCount++
        }
        elseif ($actual -eq $target.patchedGitBlobSha1) {
            $patchedCount++
        }
        else {
            $invalid.Add("$($target.path) ($actual)")
        }
    }

    if ($invalid.Count -gt 0) {
        throw (
            "Target files do not match the recorded preimage or patched " +
            "state:`n  " + ($invalid -join "`n  ")
        )
    }

    $targetCount = @($script:Manifest.targetFiles).Count
    if ($baseCount -eq $targetCount) {
        return "Base"
    }
    if ($patchedCount -eq $targetCount) {
        return "Patched"
    }

    throw (
        "The checkout has a mixed patch state " +
        "($baseCount base, $patchedCount patched)."
    )
}

function Assert-NoUnrelatedTrackedChanges {
    $changedText = Invoke-Git `
        -Arguments @("diff", "--name-only", "HEAD", "--") `
        -Capture
    $changed = @(
        $changedText -split "`n" |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    )
    $targets = @($script:Manifest.targetFiles | ForEach-Object { $_.path })
    $unexpected = @($changed | Where-Object { $_ -notin $targets })

    if ($unexpected.Count -gt 0) {
        throw (
            "Refusing to package unrelated tracked changes:`n  " +
            ($unexpected -join "`n  ")
        )
    }

    $untrackedText = Invoke-Git `
        -Arguments @("ls-files", "--others", "--exclude-standard") `
        -Capture
    $untracked = @(
        $untrackedText -split "`n" |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    )
    if ($untracked.Count -gt 0) {
        throw (
            "Refusing to package untracked source files:`n  " +
            ($untracked -join "`n  ")
        )
    }
}

function Assert-PatchPackage {
    if (-not (Test-Path -LiteralPath $script:PatchPath -PathType Leaf)) {
        throw "Patch file is missing: $script:PatchPath"
    }

    $patchHash = Get-Sha256 -Path $script:PatchPath
    if ($patchHash -ne $script:Manifest.patch.sha256) {
        throw (
            "Patch SHA-256 mismatch. Expected " +
            "$($script:Manifest.patch.sha256), got $patchHash."
        )
    }

    $targetCount = @($script:Manifest.targetFiles).Count
    if ($targetCount -ne [int]$script:Manifest.patch.targetCount) {
        throw "Manifest target count is internally inconsistent."
    }
}

function Assert-Source {
    Assert-Command -Name "git"
    Assert-Command -Name "node"
    Assert-Command -Name "npm"
    Assert-Command -Name "bunx"
    Assert-PatchPackage

    if (-not $SourcePath) {
        throw "-SourcePath is required for mode '$Mode'."
    }

    Initialize-Source -Path $SourcePath

    $isWorkTree = Invoke-Git `
        -Arguments @("rev-parse", "--is-inside-work-tree") `
        -Capture
    if ($isWorkTree -ne "true") {
        throw "SourcePath is not a Git working tree."
    }

    $head = Invoke-Git -Arguments @("rev-parse", "HEAD") -Capture
    if ($head -ne $script:Manifest.upstream.commit) {
        throw (
            "Wrong OpenClaude commit. Expected " +
            "$($script:Manifest.upstream.commit), got $head."
        )
    }

    $packagePath = Join-Path $script:SourceRoot "package.json"
    $package = Get-Content -LiteralPath $packagePath -Raw | ConvertFrom-Json
    if ($package.name -ne $script:Manifest.upstream.packageName) {
        throw (
            "Wrong npm package. Expected '$($script:Manifest.upstream.packageName)', " +
            "got '$($package.name)'."
        )
    }
    if ($package.version -ne $script:Manifest.upstream.declaredVersion) {
        throw (
            "Wrong source package version. Expected " +
            "'$($script:Manifest.upstream.declaredVersion)', " +
            "got '$($package.version)'."
        )
    }

    $nodeVersionText = (
        Invoke-Native -FilePath "node" -Arguments @("--version") -Capture
    ).TrimStart("v")
    $nodeVersion = [version]$nodeVersionText
    $minimumNode = [version]$script:Manifest.toolchain.nodeMinimum
    if ($nodeVersion -lt $minimumNode) {
        throw (
            "Node.js $minimumNode or newer is required; found $nodeVersion."
        )
    }

    Assert-NoUnrelatedTrackedChanges
    $state = Get-TargetState

    if ($state -eq "Base") {
        if (-not (
            Test-Git -Arguments @(
                "apply",
                "--check",
                "--whitespace=error-all",
                $script:PatchPath
            )
        )) {
            throw "git apply --check rejected the patch."
        }
    }
    else {
        if (-not (
            Test-Git -Arguments @(
                "apply",
                "--reverse",
                "--check",
                "--whitespace=error-all",
                $script:PatchPath
            )
        )) {
            throw "The target hashes are patched, but reverse apply check failed."
        }
    }

    return [ordered]@{
        sourcePath = $script:SourceRoot
        head = $head
        state = $state
        distributionVersion = $script:Manifest.distributionVersion
    }
}

function Invoke-Bun {
    param([Parameter(Mandatory)][string[]] $Arguments)

    $bunArguments = @(
        "bun@$($script:Manifest.toolchain.bun)"
    ) + $Arguments
    Invoke-Native `
        -FilePath "bunx" `
        -Arguments $bunArguments `
        -WorkingDirectory $script:SourceRoot
}

function Invoke-VerificationGates {
    Write-Host "Installing dependencies with Bun $($script:Manifest.toolchain.bun)..."
    Invoke-Bun -Arguments @("install", "--frozen-lockfile")

    Write-Host "Running targeted tests..."
    $testArguments = @(
        "test",
        "--feature=UNATTENDED_RETRY",
        "--max-concurrency=1",
        "--timeout=20000"
    ) + @($script:Manifest.testFiles)
    Invoke-Bun -Arguments $testArguments
    foreach ($filtered in @($script:Manifest.filteredTests)) {
        Invoke-Bun -Arguments @(
            "test",
            "--feature=UNATTENDED_RETRY",
            "--max-concurrency=1",
            "--timeout=20000",
            "--test-name-pattern=$($filtered.namePattern)",
            $filtered.file
        )
    }

    Write-Host "Running typecheck..."
    Invoke-Bun -Arguments @("run", "typecheck")

    Write-Host "Building OpenClaude..."
    Invoke-Bun -Arguments @("run", "build")
}

function Invoke-NpmPack {
    param(
        [Parameter(Mandatory)][string] $PackageSpec,
        [Parameter(Mandatory)][string] $Destination
    )

    if (-not (Test-Path -LiteralPath $Destination -PathType Container)) {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    }

    $json = Invoke-Native `
        -FilePath "npm" `
        -Arguments @(
            "pack",
            "--ignore-scripts",
            "--json",
            "--pack-destination",
            $Destination,
            $PackageSpec
        ) `
        -Capture
    $result = @($json | ConvertFrom-Json)
    if ($result.Count -ne 1 -or -not $result[0].filename) {
        throw "npm pack did not return exactly one archive."
    }

    $filename = [IO.Path]::GetFileName([string]$result[0].filename)
    if ($filename -ne [string]$result[0].filename) {
        throw "npm pack returned an unsafe archive name."
    }

    $archive = Join-Path $Destination $filename
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
        throw "npm pack archive was not created: $archive"
    }
    return $archive
}

function Get-GlobalPackageInfo {
    $root = Invoke-Native `
        -FilePath "npm" `
        -Arguments @("root", "--global") `
        -Capture
    $packagePath = Join-Path $root "@gitlawb\openclaude"
    return [ordered]@{
        root = $root
        path = $packagePath
        exists = (Test-Path -LiteralPath $packagePath -PathType Container)
    }
}

function Get-SettingsPath {
    $profile = [Environment]::GetFolderPath("UserProfile")
    if (-not $profile) {
        throw "Could not resolve the current user profile."
    }
    return Join-Path $profile ".openclaude\settings.json"
}

function Get-SettingsPreimage {
    param(
        [Parameter(Mandatory)][string] $SettingsPath,
        [Parameter(Mandatory)][string] $BackupDirectory
    )

    $existed = Test-Path -LiteralPath $SettingsPath -PathType Leaf
    if ($existed) {
        $settings = Get-Content -LiteralPath $SettingsPath -Raw |
            ConvertFrom-Json
        $backupFile = "settings.json.preimage"
        Copy-Item `
            -LiteralPath $SettingsPath `
            -Destination (Join-Path $BackupDirectory $backupFile)
        $backupHash = Get-Sha256 -Path (Join-Path $BackupDirectory $backupFile)
    }
    else {
        $settings = [pscustomobject]@{}
        $backupFile = $null
        $backupHash = $null
    }

    $envNode = $settings.PSObject.Properties["env"]
    if ($null -eq $envNode -or $null -eq $envNode.Value) {
        $envObject = [pscustomobject]@{}
    }
    else {
        $envObject = $envNode.Value
    }

    $envSnapshot = [ordered]@{}
    foreach ($property in $script:Manifest.settingsOverlay.env.PSObject.Properties) {
        $envSnapshot[$property.Name] = Get-PropertySnapshot `
            -Object $envObject `
            -Name $property.Name
    }

    $rootSnapshot = [ordered]@{}
    foreach ($property in $script:Manifest.settingsOverlay.root.PSObject.Properties) {
        $rootSnapshot[$property.Name] = Get-PropertySnapshot `
            -Object $settings `
            -Name $property.Name
    }

    return [ordered]@{
        path = [IO.Path]::GetFullPath($SettingsPath)
        existed = $existed
        backupFile = $backupFile
        sha256 = $backupHash
        overlayPreimage = [ordered]@{
            env = $envSnapshot
            root = $rootSnapshot
        }
    }
}

function New-BackupDirectory {
    $root = [IO.Path]::GetFullPath($BackupRoot)
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        New-Item -ItemType Directory -Path $root -Force | Out-Null
    }

    $timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $leaf = "$timestamp-$($script:Manifest.distributionVersion)"
    $candidate = Join-Path $root $leaf
    if (Test-Path -LiteralPath $candidate) {
        $leaf = "$leaf-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
        $candidate = Join-Path $root $leaf
    }
    New-Item -ItemType Directory -Path $candidate | Out-Null
    return [IO.Path]::GetFullPath($candidate)
}

function New-GlobalPackagePreimage {
    param(
        [Parameter(Mandatory)] $GlobalInfo,
        [Parameter(Mandatory)][string] $BackupDirectory
    )

    if (-not $GlobalInfo.exists) {
        return [ordered]@{
            existed = $false
            packagePath = $GlobalInfo.path
            version = $null
            archiveFile = $null
            archiveSha256 = $null
            cliBackupFile = $null
            cliSha256 = $null
        }
    }

    $packageJsonPath = Join-Path $GlobalInfo.path "package.json"
    $packageJson = Get-Content -LiteralPath $packageJsonPath -Raw |
        ConvertFrom-Json
    if ($packageJson.name -ne $script:Manifest.upstream.packageName) {
        throw "Unexpected package occupies the global OpenClaude path."
    }

    $archiveDirectory = Join-Path $BackupDirectory "global-package"
    $archive = Invoke-NpmPack `
        -PackageSpec $GlobalInfo.path `
        -Destination $archiveDirectory
    $archiveRelative = "global-package\$([IO.Path]::GetFileName($archive))"

    $cliPath = Join-Path $GlobalInfo.path "dist\cli.mjs"
    if (-not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
        throw "Installed OpenClaude does not contain dist/cli.mjs."
    }
    $cliBackupFile = "global-cli.mjs.preimage"
    $cliBackupPath = Join-Path $BackupDirectory $cliBackupFile
    Copy-Item -LiteralPath $cliPath -Destination $cliBackupPath

    return [ordered]@{
        existed = $true
        packagePath = [IO.Path]::GetFullPath($GlobalInfo.path)
        version = [string]$packageJson.version
        archiveFile = $archiveRelative
        archiveSha256 = Get-Sha256 -Path $archive
        cliBackupFile = $cliBackupFile
        cliSha256 = Get-Sha256 -Path $cliBackupPath
    }
}

function New-IntegrationTarball {
    param(
        [Parameter(Mandatory)][string] $Destination
    )

    $packagePath = Join-Path $script:SourceRoot "package.json"
    [byte[]]$preimage = [IO.File]::ReadAllBytes($packagePath)
    try {
        $package = Get-Content -LiteralPath $packagePath -Raw |
            ConvertFrom-Json
        $package.version = $script:Manifest.distributionVersion
        $utf8 = [Text.UTF8Encoding]::new($false)
        $json = $package | ConvertTo-Json -Depth 30
        [IO.File]::WriteAllText(
            $packagePath,
            $json + [Environment]::NewLine,
            $utf8
        )

        # Rebuild once with the integration version in package.json so the
        # bundled CLI reports the same version as the npm package metadata.
        Invoke-Bun -Arguments @("run", "build") | Out-Host

        return Invoke-NpmPack `
            -PackageSpec $script:SourceRoot `
            -Destination $Destination
    }
    finally {
        [IO.File]::WriteAllBytes($packagePath, $preimage)
    }
}

function Set-SettingsOverlay {
    param([Parameter(Mandatory)][string] $SettingsPath)

    if (Test-Path -LiteralPath $SettingsPath -PathType Leaf) {
        $settings = Get-Content -LiteralPath $SettingsPath -Raw |
            ConvertFrom-Json
    }
    else {
        $settings = [pscustomobject]@{}
    }

    $envProperty = $settings.PSObject.Properties["env"]
    if ($null -eq $envProperty -or $null -eq $envProperty.Value) {
        $envObject = [pscustomobject]@{}
        Set-ObjectProperty -Object $settings -Name "env" -Value $envObject
    }
    else {
        $envObject = $envProperty.Value
    }

    foreach ($property in $script:Manifest.settingsOverlay.env.PSObject.Properties) {
        Set-ObjectProperty `
            -Object $envObject `
            -Name $property.Name `
            -Value $property.Value
    }
    foreach ($property in $script:Manifest.settingsOverlay.root.PSObject.Properties) {
        Set-ObjectProperty `
            -Object $settings `
            -Name $property.Name `
            -Value $property.Value
    }

    Write-JsonFile -Path $SettingsPath -Value $settings
}

function Assert-InstalledIntegration {
    param([Parameter(Mandatory)][string] $ExpectedCliSha256)

    $globalInfo = Get-GlobalPackageInfo
    if (-not $globalInfo.exists) {
        throw "The global OpenClaude package is missing after npm install."
    }

    $packagePath = Join-Path $globalInfo.path "package.json"
    $package = Get-Content -LiteralPath $packagePath -Raw | ConvertFrom-Json
    if ($package.version -ne $script:Manifest.distributionVersion) {
        throw (
            "Installed npm version mismatch. Expected " +
            "'$($script:Manifest.distributionVersion)', got " +
            "'$($package.version)'."
        )
    }

    $cliPath = Join-Path $globalInfo.path "dist\cli.mjs"
    if ((Get-Sha256 -Path $cliPath) -ne $ExpectedCliSha256) {
        throw "Installed cli.mjs does not match the verified build."
    }

    $reportedVersion = Invoke-Native `
        -FilePath "node" `
        -Arguments @($cliPath, "--version") `
        -Capture
    $expectedVersion = "$($script:Manifest.distributionVersion) (OpenClaude)"
    if ($reportedVersion -ne $expectedVersion) {
        throw (
            "Installed CLI version mismatch. Expected '$expectedVersion', " +
            "got '$reportedVersion'."
        )
    }
}

function Assert-SettingsOverlay {
    param([Parameter(Mandatory)][string] $SettingsPath)

    $settings = Get-Content -LiteralPath $SettingsPath -Raw |
        ConvertFrom-Json
    foreach ($property in $script:Manifest.settingsOverlay.env.PSObject.Properties) {
        $actual = $settings.env.PSObject.Properties[$property.Name]
        if ($null -eq $actual -or $actual.Value -ne $property.Value) {
            throw "Settings overlay verification failed for env.$($property.Name)."
        }
    }
    foreach ($property in $script:Manifest.settingsOverlay.root.PSObject.Properties) {
        $actual = $settings.PSObject.Properties[$property.Name]
        if ($null -eq $actual -or $actual.Value -ne $property.Value) {
            throw "Settings overlay verification failed for $($property.Name)."
        }
    }
}

function Resolve-BackupChild {
    param(
        [Parameter(Mandatory)][string] $BackupDirectory,
        [Parameter(Mandatory)][string] $RelativePath
    )

    if ([IO.Path]::IsPathRooted($RelativePath)) {
        throw "Backup manifest contains a rooted child path."
    }

    $root = [IO.Path]::GetFullPath($BackupDirectory)
    $candidate = [IO.Path]::GetFullPath((Join-Path $root $RelativePath))
    $prefix = $root.TrimEnd("\", "/") + [IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith(
        $prefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Backup manifest child path escapes the backup directory."
    }
    return $candidate
}

function Restore-BackupState {
    param(
        [Parameter(Mandatory)][string] $BackupDirectory,
        [Parameter(Mandatory)] $BackupManifest
    )

    $globalInfo = Get-GlobalPackageInfo
    $previousGlobal = $BackupManifest.globalPackage
    if ([bool]$previousGlobal.existed) {
        $archive = Resolve-BackupChild `
            -BackupDirectory $BackupDirectory `
            -RelativePath $previousGlobal.archiveFile
        if ((Get-Sha256 -Path $archive) -ne $previousGlobal.archiveSha256) {
            throw "Saved global npm package archive failed SHA-256 validation."
        }

        Invoke-Native `
            -FilePath "npm" `
            -Arguments @(
                "install",
                "--global",
                "--no-audit",
                "--no-fund",
                $archive
            )

        $globalInfo = Get-GlobalPackageInfo
        $savedCli = Resolve-BackupChild `
            -BackupDirectory $BackupDirectory `
            -RelativePath $previousGlobal.cliBackupFile
        if ((Get-Sha256 -Path $savedCli) -ne $previousGlobal.cliSha256) {
            throw "Saved cli.mjs failed SHA-256 validation."
        }
        $restoredCli = Join-Path $globalInfo.path "dist\cli.mjs"
        Copy-Item -LiteralPath $savedCli -Destination $restoredCli -Force
        if ((Get-Sha256 -Path $restoredCli) -ne $previousGlobal.cliSha256) {
            throw "Restored cli.mjs does not match its preimage."
        }
    }
    elseif ($globalInfo.exists) {
        Invoke-Native `
            -FilePath "npm" `
            -Arguments @(
                "uninstall",
                "--global",
                $script:Manifest.upstream.packageName
            )
    }

    $settingsPath = Get-SettingsPath
    $recordedSettingsPath = [IO.Path]::GetFullPath(
        [string]$BackupManifest.settings.path
    )
    if (-not $settingsPath.Equals(
        $recordedSettingsPath,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw (
            "Backup settings path belongs to another user/profile: " +
            $recordedSettingsPath
        )
    }

    if ([bool]$BackupManifest.settings.existed) {
        $settingsBackup = Resolve-BackupChild `
            -BackupDirectory $BackupDirectory `
            -RelativePath $BackupManifest.settings.backupFile
        if ((Get-Sha256 -Path $settingsBackup) -ne $BackupManifest.settings.sha256) {
            throw "Saved settings file failed SHA-256 validation."
        }
        $settingsParent = Split-Path -Parent $settingsPath
        if (-not (Test-Path -LiteralPath $settingsParent -PathType Container)) {
            New-Item -ItemType Directory -Path $settingsParent -Force |
                Out-Null
        }
        Copy-Item `
            -LiteralPath $settingsBackup `
            -Destination $settingsPath `
            -Force
    }
    elseif (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
        Remove-Item -LiteralPath $settingsPath -Force
    }
}

function Undo-SourcePatch {
    param([Parameter(Mandatory)][string] $Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Write-Warning "Source checkout no longer exists; patch was not reversed."
        return
    }

    Initialize-Source -Path $Path
    $head = Invoke-Git -Arguments @("rev-parse", "HEAD") -Capture
    if ($head -ne $script:Manifest.upstream.commit) {
        Write-Warning "Source HEAD changed; patch was not reversed."
        return
    }

    $state = Get-TargetState
    if ($state -eq "Base") {
        return
    }
    if (-not (
        Test-Git -Arguments @(
            "apply",
            "--reverse",
            "--check",
            "--whitespace=error-all",
            $script:PatchPath
        )
    )) {
        Write-Warning "Source no longer reverse-applies cleanly; it was not changed."
        return
    }

    Invoke-Git -Arguments @(
        "apply",
        "--reverse",
        "--whitespace=error-all",
        $script:PatchPath
    )
    if ((Get-TargetState) -ne "Base") {
        throw "Source patch reversal did not restore the recorded preimage."
    }
}

function Get-BackupDirectory {
    $root = [IO.Path]::GetFullPath($BackupRoot)
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "Backup root does not exist: $root"
    }

    if ($BackupId) {
        if (
            $BackupId -in @(".", "..") -or
            $BackupId.IndexOfAny([char[]]@("\", "/")) -ge 0
        ) {
            throw "BackupId must be a single backup directory name."
        }
        $candidate = Join-Path $root $BackupId
        if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
            throw "Backup does not exist: $candidate"
        }
        return [IO.Path]::GetFullPath($candidate)
    }

    $eligible = @(
        Get-ChildItem -LiteralPath $root -Directory |
            Sort-Object LastWriteTimeUtc -Descending |
            Where-Object {
                $manifestPath = Join-Path $_.FullName "backup-manifest.json"
                if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
                    return $false
                }
                try {
                    $record = Get-Content -LiteralPath $manifestPath -Raw |
                        ConvertFrom-Json
                    return $record.status -eq "installed"
                }
                catch {
                    return $false
                }
            }
    )
    if ($eligible.Count -eq 0) {
        throw "No completed integration backup was found under $root."
    }
    return $eligible[0].FullName
}

function Invoke-Install {
    $sourceCheck = Assert-Source
    Write-Host (
        "OpenClaude source verified: {0} ({1})" -f
        $sourceCheck.head,
        $sourceCheck.state
    )

    if (-not $PSCmdlet.ShouldProcess(
        $script:SourceRoot,
        "apply, verify, package, and globally install the OpenClaude integration"
    )) {
        return
    }

    $sourcePatchApplied = $false
    $backupDirectory = $null
    $backupRecord = $null
    $backupManifestPath = $null
    $globalMutationAttempted = $false
    $settingsMutationAttempted = $false

    try {
        if ($sourceCheck.state -eq "Base") {
            Write-Host "Applying integration patch..."
            Invoke-Git -Arguments @(
                "apply",
                "--whitespace=error-all",
                $script:PatchPath
            )
            $sourcePatchApplied = $true
        }
        else {
            Write-Host "Integration patch is already applied."
        }

        if ((Get-TargetState) -ne "Patched") {
            throw "Patched target hashes do not match the manifest."
        }

        Invoke-VerificationGates

        $backupDirectory = New-BackupDirectory
        $backupManifestPath = Join-Path `
            $backupDirectory `
            "backup-manifest.json"
        $globalInfo = Get-GlobalPackageInfo
        $globalPreimage = New-GlobalPackagePreimage `
            -GlobalInfo $globalInfo `
            -BackupDirectory $backupDirectory
        $settingsPath = Get-SettingsPath
        $settingsPreimage = Get-SettingsPreimage `
            -SettingsPath $settingsPath `
            -BackupDirectory $backupDirectory

        $packageDirectory = Join-Path $backupDirectory "integration-package"
        $integrationArchive = New-IntegrationTarball `
            -Destination $packageDirectory
        $integrationCliHash = Get-Sha256 -Path (
            Join-Path $script:SourceRoot "dist\cli.mjs"
        )
        $integrationRelative = (
            "integration-package\{0}" -f
            [IO.Path]::GetFileName($integrationArchive)
        )

        $backupRecord = [ordered]@{
            schemaVersion = 1
            status = "prepared"
            createdAtUtc = [DateTime]::UtcNow.ToString("o")
            distributionVersion = $script:Manifest.distributionVersion
            upstreamCommit = $script:Manifest.upstream.commit
            source = [ordered]@{
                path = $script:SourceRoot
                patchAppliedByInstaller = $sourcePatchApplied
            }
            globalPackage = $globalPreimage
            settings = $settingsPreimage
            integrationPackage = [ordered]@{
                archiveFile = $integrationRelative
                sha256 = Get-Sha256 -Path $integrationArchive
                cliSha256 = $integrationCliHash
            }
        }
        Write-JsonFile -Path $backupManifestPath -Value $backupRecord

        Write-Host "Installing custom npm package..."
        $globalMutationAttempted = $true
        Invoke-Native `
            -FilePath "npm" `
            -Arguments @(
                "install",
                "--global",
                "--no-audit",
                "--no-fund",
                $integrationArchive
            )
        Assert-InstalledIntegration -ExpectedCliSha256 $integrationCliHash

        Write-Host "Applying OpenClaude settings overlay..."
        $settingsMutationAttempted = $true
        Set-SettingsOverlay -SettingsPath $settingsPath
        Assert-SettingsOverlay -SettingsPath $settingsPath

        $backupRecord.status = "installed"
        $backupRecord["installedAtUtc"] = [DateTime]::UtcNow.ToString("o")
        Write-JsonFile -Path $backupManifestPath -Value $backupRecord

        Write-Host ""
        Write-Host "Installed $($script:Manifest.distributionVersion)."
        Write-Host "Backup: $backupDirectory"
    }
    catch {
        $originalError = $_
        if ($backupRecord) {
            try {
                if ($globalMutationAttempted -or $settingsMutationAttempted) {
                    Write-Warning "Install failed; restoring package/settings preimages."
                    Restore-BackupState `
                        -BackupDirectory $backupDirectory `
                        -BackupManifest $backupRecord
                }
                $backupRecord.status = "failed-rolled-back"
                $backupRecord["failedAtUtc"] = [DateTime]::UtcNow.ToString("o")
                Write-JsonFile -Path $backupManifestPath -Value $backupRecord
            }
            catch {
                Write-Warning (
                    "Automatic package/settings rollback also failed: " +
                    $_.Exception.Message
                )
            }
        }

        if ($sourcePatchApplied) {
            try {
                Undo-SourcePatch -Path $script:SourceRoot
            }
            catch {
                Write-Warning (
                    "Automatic source rollback also failed: " +
                    $_.Exception.Message
                )
            }
        }
        throw $originalError
    }
}

function Invoke-Rollback {
    Assert-Command -Name "git"
    Assert-Command -Name "npm"
    Assert-PatchPackage

    $backupDirectory = Get-BackupDirectory
    $backupManifestPath = Join-Path $backupDirectory "backup-manifest.json"
    $backupManifest = Get-Content -LiteralPath $backupManifestPath -Raw |
        ConvertFrom-Json
    if ([int]$backupManifest.schemaVersion -ne 1) {
        throw "Unsupported backup manifest schema."
    }

    if (-not $PSCmdlet.ShouldProcess(
        $backupDirectory,
        "restore the saved global OpenClaude package and settings"
    )) {
        return
    }

    Restore-BackupState `
        -BackupDirectory $backupDirectory `
        -BackupManifest $backupManifest

    if (
        [bool]$backupManifest.source.patchAppliedByInstaller -and
        -not $KeepSourcePatch
    ) {
        $rollbackSource = if ($SourcePath) {
            $SourcePath
        }
        else {
            [string]$backupManifest.source.path
        }
        Undo-SourcePatch -Path $rollbackSource
    }

    Set-ObjectProperty `
        -Object $backupManifest `
        -Name "status" `
        -Value "rolled-back"
    Set-ObjectProperty `
        -Object $backupManifest `
        -Name "rolledBackAtUtc" `
        -Value ([DateTime]::UtcNow.ToString("o"))
    Write-JsonFile -Path $backupManifestPath -Value $backupManifest

    Write-Host "Rollback complete: $backupDirectory"
}

switch ($Mode) {
    "Check" {
        $result = Assert-Source
        $result | ConvertTo-Json -Depth 5
    }
    "Install" {
        Invoke-Install
    }
    "Rollback" {
        Invoke-Rollback
    }
}
