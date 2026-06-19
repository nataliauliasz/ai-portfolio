[CmdletBinding()]
param(
    [ValidateSet("dynamic", "precomputed")]
    [string]$Mode = "dynamic",

    [string]$TaskName = "Charging Log Ranking Snapshot",

    [string]$TaskTime = "03:00",

    [string]$TaskUser = "$env:USERDOMAIN\$env:USERNAME",

    [bool]$WakeToRun = $true
)

$ErrorActionPreference = "Stop"

function Convert-SecureStringToPlainText {
    param(
        [Parameter(Mandatory = $true)]
        [Security.SecureString]$SecureString
    )

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureString)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

$workspace = Split-Path -Parent $PSScriptRoot
$powershellExe = (Get-Command "powershell.exe" -ErrorAction Stop).Source
$taskTimeSpan = [TimeSpan]::ParseExact($TaskTime, "hh\:mm", [System.Globalization.CultureInfo]::InvariantCulture)
$triggerTime = (Get-Date).Date.Add($taskTimeSpan)
$taskScriptPath = if ($Mode -eq "dynamic") {
    Join-Path $PSScriptRoot "run_ranking_snapshot_dynamic.ps1"
} else {
    Join-Path $workspace "run_ranking_snapshot.ps1"
}

if (-not (Test-Path -LiteralPath $taskScriptPath)) {
    throw "Task script not found: $taskScriptPath"
}

$taskArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$taskScriptPath`""
$action = New-ScheduledTaskAction -Execute $powershellExe -Argument $taskArguments -WorkingDirectory $workspace
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime

$settingsParams = @{
    StartWhenAvailable = $true
    AllowStartIfOnBatteries = $true
    DontStopIfGoingOnBatteries = $true
    MultipleInstances = "IgnoreNew"
    ExecutionTimeLimit = (New-TimeSpan -Hours 72)
}
if ($WakeToRun) {
    $settingsParams.WakeToRun = $true
}
$settings = New-ScheduledTaskSettingsSet @settingsParams

$description = if ($Mode -eq "dynamic") {
    "Nightly charging log ranking snapshot with dynamic session fallback."
} else {
    "Nightly charging log ranking snapshot using precomputed session source."
}

$taskPasswordSecure = Read-Host -Prompt "Password for $TaskUser" -AsSecureString
$taskPassword = Convert-SecureStringToPlainText -SecureString $taskPasswordSecure

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description $description `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -User $TaskUser `
        -Password $taskPassword `
        -RunLevel Limited `
        -Force | Out-Null
} finally {
    $taskPassword = $null
}

Get-ScheduledTask -TaskName $TaskName | Format-List TaskName,TaskPath,State
Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime,LastTaskResult,NextRunTime,NumberOfMissedRuns
