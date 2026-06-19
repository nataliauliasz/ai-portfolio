$ErrorActionPreference = "Stop"

function Resolve-PythonInvocation {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Workspace
    )

    $override = [Environment]::GetEnvironmentVariable("PYTHON_EXE")
    $pathCandidates = @(
        $override,
        (Join-Path $Workspace ".venv\Scripts\python.exe"),
        (Join-Path $Workspace ".venv\bin\python.exe"),
        (Join-Path $Workspace ".venv\bin\python")
    ) | Where-Object { $_ }

    foreach ($candidate in $pathCandidates) {
        if (Test-Path -LiteralPath $candidate) {
            return @{
                Command = $candidate
                PrefixArgs = @()
                Description = $candidate
            }
        }
    }

    foreach ($commandName in @("python.exe", "python")) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($command) {
            return @{
                Command = $command.Source
                PrefixArgs = @()
                Description = $command.Source
            }
        }
    }

    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        return @{
            Command = $pyLauncher.Source
            PrefixArgs = @("-3")
            Description = "$($pyLauncher.Source) -3"
        }
    }

    throw "Unable to locate a Python interpreter. Set PYTHON_EXE or install python.exe."
}

$workspace = Split-Path -Parent $PSScriptRoot
Set-Location $workspace

$envFiles = @(".env", ".env.local", "db.env", "db.local.env")

foreach ($name in $envFiles) {
    $path = Join-Path $workspace $name
    if (-not (Test-Path -LiteralPath $path)) {
        continue
    }

    Get-Content -LiteralPath $path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }

        $parts = $line.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim()
        if (-not $key) {
            return
        }

        if (
            $value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        if (-not (Test-Path "Env:$key")) {
            Set-Item -Path "Env:$key" -Value $value
        }
    }
}

$required = @("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
$missing = @(
    $required | Where-Object {
        $value = [Environment]::GetEnvironmentVariable($_)
        -not $value -or -not $value.Trim()
    }
)
if ($missing.Count -gt 0) {
    throw "Missing required environment variables: $($missing -join ', '). Add them to .env or db.env in $workspace."
}

$logsDir = Join-Path $workspace "logs"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
$timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$logPath = Join-Path $logsDir "ranking_snapshot_$timestamp.log"

try {
    "[$(Get-Date -Format s)] Starting ranking snapshot refresh (dynamic fallback)" | Tee-Object -FilePath $logPath
    "[$(Get-Date -Format s)] Workspace: $workspace" | Tee-Object -FilePath $logPath -Append

    $pythonInvocation = Resolve-PythonInvocation -Workspace $workspace
    "[$(Get-Date -Format s)] Using Python launcher: $($pythonInvocation.Description)" | Tee-Object -FilePath $logPath -Append

    $commandArgs = @(
        $pythonInvocation.PrefixArgs +
        @(
            "generate_ranking_snapshot.py",
            "--refresh-source-view",
            "--allow-dynamic-session-fallback"
        )
    )

    & $pythonInvocation.Command @commandArgs 2>&1 | Tee-Object -FilePath $logPath -Append

    $exitCode = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }
    if ($exitCode -ne 0) {
        throw "Ranking snapshot refresh failed with exit code $exitCode. See $logPath"
    }

    "[$(Get-Date -Format s)] Ranking snapshot refresh finished successfully" | Tee-Object -FilePath $logPath -Append
} catch {
    "[$(Get-Date -Format s)] ERROR: $($_.Exception.Message)" | Tee-Object -FilePath $logPath -Append
    throw
}
