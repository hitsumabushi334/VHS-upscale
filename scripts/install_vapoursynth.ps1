[CmdletBinding()]
param(
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'

$scriptRoot = Split-Path -Parent $PSScriptRoot
$requirementsPath = Join-Path $scriptRoot 'requirements-vapoursynth.txt'
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptRoot)
$logDirectory = Join-Path $projectRoot 'work\logs\qtgmc-long-processing'
$pythonPath = 'C:\Python313\python.exe'

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logPath = Join-Path $logDirectory ("install-vapoursynth-{0}.log" -f $timestamp)
$arguments = @('-m', 'pip', 'install')

if ($Apply) {
    $arguments += @('-r', $requirementsPath)
} else {
    $arguments += @('--dry-run', '-r', $requirementsPath)
}

$command = '"{0}" {1}' -f $pythonPath, ($arguments -join ' ')
Write-Output ("command: {0}" -f $command)

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$commandOutput = & $pythonPath @arguments 2>&1
$exitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference

$logLines = @(("command: {0}" -f $command))
foreach ($outputLine in $commandOutput) {
    $logLines += [string]$outputLine
}
$logLines += ("exit_code: {0}" -f $exitCode)
[System.IO.File]::WriteAllLines($logPath, $logLines, (New-Object System.Text.UTF8Encoding($false)))

$commandOutput | Write-Output
Write-Output ("exit_code: {0}" -f $exitCode)
exit $exitCode
