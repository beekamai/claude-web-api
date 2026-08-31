[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [int]$Port = 0,
    [ValidateRange(1, 60)]
    [int]$ProbeIntervalSeconds = 5,
    [ValidateRange(1, 30)]
    [int]$ProbeTimeoutSeconds = 3,
    [ValidateRange(1, 20)]
    [int]$FailureThreshold = 3,
    [ValidateRange(10, 600)]
    [int]$StartupGraceSeconds = 360,
    [ValidateRange(1, 300)]
    [int]$BackoffBaseSeconds = 2,
    [ValidateRange(1, 600)]
    [int]$BackoffMaxSeconds = 60,
    [ValidateRange(2, 100)]
    [int]$CircuitRestartLimit = 5,
    [ValidateRange(30, 3600)]
    [int]$CircuitWindowSeconds = 600,
    [ValidateRange(10, 3600)]
    [int]$CircuitOpenSeconds = 120,
    [ValidateRange(30, 3600)]
    [int]$StableResetSeconds = 300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $projectRoot ".python\python.exe"
}
$packageRoot = Join-Path $projectRoot "src"
$serverModulePath = Join-Path $packageRoot "claude_web_api\app.py"
$serverPidPath = Join-Path $projectRoot ".server.pid"
$supervisorPidPath = Join-Path $projectRoot ".supervisor.pid"
$supervisorLockPath = Join-Path $projectRoot ".supervisor.lock"
$stdoutLogPath = Join-Path $projectRoot "server.stdout.log"
$stderrLogPath = Join-Path $projectRoot "server.stderr.log"

if ($Port -le 0) {
    if (-not [string]::IsNullOrWhiteSpace($env:PORT)) {
        $parsedPort = 0
        if (-not [int]::TryParse($env:PORT, [ref]$parsedPort) -or $parsedPort -lt 1 -or $parsedPort -gt 65535) {
            throw "PORT must be an integer between 1 and 65535."
        }
        $Port = $parsedPort
    }
    else {
        $Port = 8765
    }
}

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Portable Python missing at '$PythonPath'. Re-run setup."
}
if (-not (Test-Path -LiteralPath $serverModulePath -PathType Leaf)) {
    throw "bridge package missing at '$serverModulePath'."
}

if (-not ("OpenClaudeProcessJob" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class OpenClaudeProcessJob
{
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x00002000;

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimitInformation
    {
        public BasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(
        IntPtr securityAttributes,
        string name
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        int informationClass,
        ref ExtendedLimitInformation information,
        uint informationLength
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(
        IntPtr job,
        IntPtr process
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    public static IntPtr CreateAndAssign(IntPtr processHandle)
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error());

        var information = new ExtendedLimitInformation();
        information.BasicLimitInformation.LimitFlags =
            JobObjectLimitKillOnJobClose;
        int size = Marshal.SizeOf(typeof(ExtendedLimitInformation));
        if (!SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                ref information,
                (uint)size
            ))
        {
            int error = Marshal.GetLastWin32Error();
            CloseHandle(job);
            throw new Win32Exception(error);
        }

        if (!AssignProcessToJobObject(job, processHandle))
        {
            int error = Marshal.GetLastWin32Error();
            CloseHandle(job);
            throw new Win32Exception(error);
        }
        return job;
    }

    public static void Close(IntPtr job)
    {
        if (job != IntPtr.Zero)
            CloseHandle(job);
    }
}
"@
}

$healthUri = "http://127.0.0.1:$Port/health/live"
$taskkillPath = Join-Path $env:SystemRoot "System32\taskkill.exe"
$supervisorPid = $PID
$currentRuntime = $null
$lockStream = $null
$restartTimes = [System.Collections.Generic.List[datetime]]::new()
$restartAttempt = 0

function Write-SupervisorMessage {
    param([string]$Message)
    $timestamp = [datetime]::Now.ToString("yyyy-MM-dd HH:mm:ss")
    Write-Host "[$timestamp] supervisor: $Message"
}

function Set-PidFile {
    param(
        [string]$Path,
        [int]$ProcessId
    )
    Set-Content -LiteralPath $Path -Value ([string]$ProcessId) -Encoding ascii
}

function Remove-PidFileIfOwned {
    param(
        [string]$Path,
        [int]$ProcessId
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    try {
        $recordedPid = (Get-Content -LiteralPath $Path -Raw).Trim()
        if ($recordedPid -eq [string]$ProcessId) {
            Remove-Item -LiteralPath $Path -Force
        }
    }
    catch {
        Write-SupervisorMessage "Could not clean PID file '$Path': $($_.Exception.Message)"
    }
}

function Start-ServerProcess {
    $stdoutStream = $null
    $stderrStream = $null
    $process = $null
    $jobHandle = [IntPtr]::Zero
    $processStarted = $false

    try {
        $stdoutStream = [System.IO.FileStream]::new(
            $stdoutLogPath,
            [System.IO.FileMode]::Append,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::ReadWrite
        )
        $stderrStream = [System.IO.FileStream]::new(
            $stderrLogPath,
            [System.IO.FileMode]::Append,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::ReadWrite
        )

        $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $PythonPath
        $startInfo.WorkingDirectory = $projectRoot
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $startInfo.EnvironmentVariables["PYTHONPATH"] = $packageRoot
        $startInfo.EnvironmentVariables["PYTHONUNBUFFERED"] = "1"
        $startInfo.EnvironmentVariables["PORT"] = [string]$Port

        if ($null -ne $startInfo.PSObject.Properties["ArgumentList"]) {
            [void]$startInfo.ArgumentList.Add("-m")
            [void]$startInfo.ArgumentList.Add("claude_web_api.app")
        }
        else {
            $startInfo.Arguments = "-m claude_web_api.app"
        }

        $process = [System.Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            throw "Process.Start() returned false."
        }
        $processStarted = $true
        $jobHandle = [OpenClaudeProcessJob]::CreateAndAssign(
            $process.Handle
        )

        $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($stdoutStream)
        $stderrTask = $process.StandardError.BaseStream.CopyToAsync($stderrStream)
        Set-PidFile -Path $serverPidPath -ProcessId $process.Id

        return [pscustomobject]@{
            Process      = $process
            ProcessId    = $process.Id
            JobHandle    = $jobHandle
            StdoutStream = $stdoutStream
            StderrStream = $stderrStream
            StdoutTask   = $stdoutTask
            StderrTask   = $stderrTask
            StartedAtUtc = [datetime]::UtcNow
        }
    }
    catch {
        if ($jobHandle -ne [IntPtr]::Zero) {
            [OpenClaudeProcessJob]::Close($jobHandle)
            $jobHandle = [IntPtr]::Zero
        }
        if ($null -ne $process) {
            if ($processStarted) {
                try {
                    $process.Refresh()
                    if (-not $process.HasExited) {
                        & $taskkillPath /PID $process.Id /T /F *> $null
                    }
                }
                catch {
                    # Preserve the original launch/setup exception.
                }
            }
            $process.Dispose()
        }
        if ($null -ne $stdoutStream) {
            $stdoutStream.Dispose()
        }
        if ($null -ne $stderrStream) {
            $stderrStream.Dispose()
        }
        throw
    }
}

function Complete-ServerRuntime {
    param([object]$Runtime)
    if ($null -eq $Runtime) {
        return
    }

    if (
        $null -ne $Runtime.PSObject.Properties["JobHandle"] -and
        $Runtime.JobHandle -ne [IntPtr]::Zero
    ) {
        [OpenClaudeProcessJob]::Close($Runtime.JobHandle)
        $Runtime.JobHandle = [IntPtr]::Zero
    }

    foreach ($copyTask in @($Runtime.StdoutTask, $Runtime.StderrTask)) {
        if ($null -eq $copyTask) {
            continue
        }
        try {
            [void]$copyTask.Wait(3000)
        }
        catch {
            # A forced process stop may close a redirected pipe mid-copy.
        }
    }

    foreach ($stream in @($Runtime.StdoutStream, $Runtime.StderrStream)) {
        if ($null -eq $stream) {
            continue
        }
        try {
            $stream.Flush()
            $stream.Dispose()
        }
        catch {
            # Cleanup is best-effort after a forced stop.
        }
    }

    try {
        $Runtime.Process.Dispose()
    }
    catch {
        # Process may already be disposed during shutdown.
    }
    Remove-PidFileIfOwned -Path $serverPidPath -ProcessId $Runtime.ProcessId
}

function Test-ProcessExited {
    param([object]$Runtime)
    try {
        $Runtime.Process.Refresh()
        return $Runtime.Process.HasExited
    }
    catch {
        return $true
    }
}

function Stop-ServerTree {
    param(
        [object]$Runtime,
        [string]$Reason
    )
    if ($null -eq $Runtime) {
        return
    }

    $childPid = [int]$Runtime.ProcessId
    if (-not (Test-ProcessExited -Runtime $Runtime)) {
        Write-SupervisorMessage "Stopping exact child tree PID $childPid ($Reason)."
        try {
            & $taskkillPath /PID $childPid /T /F *> $null
            if ($LASTEXITCODE -ne 0) {
                Write-SupervisorMessage "taskkill exited with code $LASTEXITCODE for PID $childPid."
            }
        }
        catch {
            Write-SupervisorMessage "taskkill failed for PID $childPid`: $($_.Exception.Message)"
        }
    }

    try {
        [void]$Runtime.Process.WaitForExit(5000)
    }
    catch {
        # The process may already have exited between checks.
    }
    Complete-ServerRuntime -Runtime $Runtime
}

function Test-HealthEndpoint {
    $response = $null
    try {
        $request = [System.Net.HttpWebRequest]::CreateHttp($healthUri)
        $request.Method = "GET"
        $request.Proxy = $null
        $request.KeepAlive = $false
        $request.Timeout = $ProbeTimeoutSeconds * 1000
        $request.ReadWriteTimeout = $ProbeTimeoutSeconds * 1000
        $response = [System.Net.HttpWebResponse]$request.GetResponse()
        $statusCode = [int]$response.StatusCode
        return $statusCode -ge 200 -and $statusCode -lt 300
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $response) {
            $response.Dispose()
        }
    }
}

function Wait-Backoff {
    param(
        [int]$Seconds,
        [string]$Reason
    )
    if ($Seconds -le 0) {
        return
    }
    Write-SupervisorMessage "$Reason Waiting $Seconds second(s)."
    Start-Sleep -Seconds $Seconds
}

try {
    try {
        $lockStream = [System.IO.FileStream]::new(
            $supervisorLockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        throw "Another supervisor already owns '$supervisorLockPath'."
    }

    Set-PidFile -Path $supervisorPidPath -ProcessId $supervisorPid
    Write-SupervisorMessage "PID $supervisorPid monitoring $healthUri."

    while ($true) {
        $restartReason = ""
        try {
            $currentRuntime = Start-ServerProcess
            Write-SupervisorMessage "Started server PID $($currentRuntime.ProcessId). Startup grace: $StartupGraceSeconds seconds."

            $consecutiveFailures = 0
            $healthySinceUtc = $null

            while ($true) {
                Start-Sleep -Seconds $ProbeIntervalSeconds

                if (Test-ProcessExited -Runtime $currentRuntime) {
                    $exitCode = "unknown"
                    try {
                        $exitCode = [string]$currentRuntime.Process.ExitCode
                    }
                    catch {
                        # Keep "unknown" if the process object cannot expose ExitCode.
                    }
                    $restartReason = "server PID $($currentRuntime.ProcessId) exited with code $exitCode"
                    Complete-ServerRuntime -Runtime $currentRuntime
                    $currentRuntime = $null
                    break
                }

                $isHealthy = Test-HealthEndpoint
                $nowUtc = [datetime]::UtcNow
                if ($isHealthy) {
                    if ($null -eq $healthySinceUtc) {
                        $healthySinceUtc = $nowUtc
                        Write-SupervisorMessage "Server PID $($currentRuntime.ProcessId) is healthy."
                    }
                    $consecutiveFailures = 0

                    if (($nowUtc - $healthySinceUtc).TotalSeconds -ge $StableResetSeconds) {
                        $restartAttempt = 0
                        $restartTimes.Clear()
                        $healthySinceUtc = $nowUtc
                        Write-SupervisorMessage "Stable window reached; restart backoff and circuit history reset."
                    }
                    continue
                }

                $uptimeSeconds = ($nowUtc - $currentRuntime.StartedAtUtc).TotalSeconds
                if ($null -eq $healthySinceUtc -and $uptimeSeconds -lt $StartupGraceSeconds) {
                    continue
                }

                $consecutiveFailures++
                Write-SupervisorMessage "Health probe failed ($consecutiveFailures/$FailureThreshold) for PID $($currentRuntime.ProcessId)."
                if ($consecutiveFailures -lt $FailureThreshold) {
                    continue
                }

                $restartReason = "$FailureThreshold consecutive health failures"
                Stop-ServerTree -Runtime $currentRuntime -Reason $restartReason
                $currentRuntime = $null
                break
            }
        }
        catch {
            $restartReason = "server supervision failed: $($_.Exception.Message)"
            Write-SupervisorMessage $restartReason
            if ($null -ne $currentRuntime) {
                Stop-ServerTree -Runtime $currentRuntime -Reason "supervisor loop error"
            }
            $currentRuntime = $null
        }

        $nowUtc = [datetime]::UtcNow
        for ($index = $restartTimes.Count - 1; $index -ge 0; $index--) {
            if (($nowUtc - $restartTimes[$index]).TotalSeconds -gt $CircuitWindowSeconds) {
                $restartTimes.RemoveAt($index)
            }
        }
        $restartTimes.Add($nowUtc)
        $restartAttempt++

        if ($restartTimes.Count -ge $CircuitRestartLimit) {
            Write-SupervisorMessage "Circuit open after $($restartTimes.Count) restarts within $CircuitWindowSeconds seconds. Last reason: $restartReason."
            Wait-Backoff -Seconds $CircuitOpenSeconds -Reason "Circuit breaker cooldown."
            $restartTimes.Clear()
            $restartAttempt = 0
            continue
        }

        $exponent = [Math]::Min($restartAttempt - 1, 20)
        $backoffSeconds = [int][Math]::Min(
            $BackoffMaxSeconds,
            $BackoffBaseSeconds * [Math]::Pow(2, $exponent)
        )
        Wait-Backoff -Seconds $backoffSeconds -Reason "Restart requested: $restartReason."
    }
}
finally {
    if ($null -ne $currentRuntime) {
        Stop-ServerTree -Runtime $currentRuntime -Reason "supervisor shutdown"
        $currentRuntime = $null
    }
    Remove-PidFileIfOwned -Path $supervisorPidPath -ProcessId $supervisorPid
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
    Write-SupervisorMessage "Stopped."
}
