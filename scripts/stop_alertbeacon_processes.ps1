param(
    [string]$Root = $env:ALERTBEACON_RESTART_ROOT,

    [string]$PythonPath = $env:ALERTBEACON_RESTART_PYTHON
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Root) -or [string]::IsNullOrWhiteSpace($PythonPath)) {
    throw "Root and PythonPath are required"
}

function Resolve-NormalizedPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
}

function Get-ProcessPath($Process) {
    try {
        return Resolve-NormalizedPath $Process.Path
    }
    catch {
        return $null
    }
}

function Stop-ProcessTree([int]$ProcessId) {
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        return
    }

    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
    }
    finally {
        $ErrorActionPreference = $previousErrorPreference
    }

    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        throw "Failed to stop process tree pid=$ProcessId"
    }
}

$rootPath = Resolve-NormalizedPath $Root
$projectPython = Resolve-NormalizedPath $PythonPath
$projectNgrok = Resolve-NormalizedPath (Join-Path $rootPath "ngrok.exe")
$anchorTimes = [System.Collections.Generic.List[datetime]]::new()

$pythonProcesses = @(
    Get-Process python, pythonw -ErrorAction SilentlyContinue |
        Where-Object { (Get-ProcessPath $_) -ieq $projectPython }
)

$ngrokProcesses = @(
    Get-Process ngrok -ErrorAction SilentlyContinue |
        Where-Object { (Get-ProcessPath $_) -ieq $projectNgrok }
)

foreach ($process in @($pythonProcesses) + @($ngrokProcesses)) {
    try {
        $anchorTimes.Add($process.StartTime)
    }
    catch {
        # StartTime is only used to close legacy cmd /k windows.
    }
}

foreach ($process in $pythonProcesses) {
    Write-Host "[STOP] AlertBeacon Python pid=$($process.Id)"
    Stop-ProcessTree $process.Id
}

foreach ($process in $ngrokProcesses) {
    Write-Host "[STOP] AlertBeacon ngrok pid=$($process.Id)"
    Stop-ProcessTree $process.Id
}

# Older run_all.bat versions launched each service through cmd /k. Once their
# child process exits, close only cmd windows started at the same moment.
if ($anchorTimes.Count -gt 0) {
    $legacyCmdProcesses = @(
        Get-Process cmd -ErrorAction SilentlyContinue | Where-Object {
            $cmd = $_
            foreach ($startedAt in $anchorTimes) {
                try {
                    if ([Math]::Abs(($cmd.StartTime - $startedAt).TotalSeconds) -le 3) {
                        return $true
                    }
                }
                catch {
                    return $false
                }
            }
            return $false
        }
    )
    foreach ($process in $legacyCmdProcesses) {
        Write-Host "[STOP] AlertBeacon console pid=$($process.Id)"
        Stop-ProcessTree $process.Id
    }
}

Start-Sleep -Milliseconds 500

$remainingPython = @(
    Get-Process python, pythonw -ErrorAction SilentlyContinue |
        Where-Object { (Get-ProcessPath $_) -ieq $projectPython }
)
$remainingNgrok = @(
    Get-Process ngrok -ErrorAction SilentlyContinue |
        Where-Object { (Get-ProcessPath $_) -ieq $projectNgrok }
)

if ($remainingPython.Count -gt 0 -or $remainingNgrok.Count -gt 0) {
    throw "AlertBeacon services are still running after the stop attempt"
}

Write-Host "[OK] Existing AlertBeacon service processes stopped"
