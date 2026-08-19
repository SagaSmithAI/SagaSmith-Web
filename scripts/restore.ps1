param(
    [Parameter(Mandatory = $true)][string]$BackupDirectory,
    [string]$ProjectName = "sagasmith-service-restore",
    [string[]]$ComposeFiles = @("compose.yaml"),
    [Parameter(Mandatory = $true)][string]$ConfirmRestore
)
$ErrorActionPreference = "Stop"

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable exited with code $LASTEXITCODE"
    }
}

$repo = Split-Path -Parent $PSScriptRoot
$backup = [System.IO.Path]::GetFullPath($BackupDirectory)
if ($ProjectName -eq "sagasmith-service") {
    throw "Live project restore is forbidden. Use a distinct isolated ProjectName."
}
if ($ConfirmRestore -ne "RESTORE-$ProjectName") {
    throw "Confirmation mismatch. Pass -ConfirmRestore RESTORE-$ProjectName"
}
Push-Location $repo
try {
    $composeArgs = @("compose", "-p", $ProjectName)
    foreach ($composeFile in $ComposeFiles) {
        $composeArgs += @("-f", $composeFile)
    }
    & (Join-Path $PSScriptRoot "verify-backup.ps1") -BackupDirectory $backup
    $existingVolumes = @(
        Invoke-CheckedNative -Executable "docker" -Arguments @("volume", "ls", "--quiet") |
            Where-Object { $_ -like "${ProjectName}_*" }
    )
    if ($existingVolumes.Count -gt 0) {
        throw "Restore project already has volumes. Use a fresh isolated ProjectName."
    }
    Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("down"))
    Invoke-CheckedNative -Executable "docker" -Arguments @(
        $composeArgs + @("up", "-d", "--wait", "postgres")
    )
    $volumes = @("object-data", "dnd-state", "coc-state", "agent-workspace")
    foreach ($volume in $volumes) {
        $target = "${ProjectName}_$volume"
        Invoke-CheckedNative -Executable "docker" -Arguments @(
            "volume", "create",
            "--label", "com.docker.compose.project=$ProjectName",
            "--label", "com.docker.compose.volume=$volume",
            $target
        ) | Out-Null
        Invoke-CheckedNative -Executable "docker" -Arguments @(
            "run", "--rm", "--mount", "source=$target,target=/target",
            "--mount", "type=bind,source=$backup,target=/backup,readonly",
            "alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce",
            "sh", "-c", "tar xzf /backup/$volume.tgz -C /target"
        )
    }
    Invoke-CheckedNative -Executable "docker" -Arguments @(
        $composeArgs + @("cp", (Join-Path $backup "control.dump"), "postgres:/tmp/control.dump")
    )
    Invoke-CheckedNative -Executable "docker" -Arguments @(
        $composeArgs + @("exec", "-T", "postgres", "pg_restore", "-U", "sagasmith", "-d", "sagasmith_service", "--clean", "--if-exists", "/tmp/control.dump")
    )
    Invoke-CheckedNative -Executable "docker" -Arguments @(
        $composeArgs + @("run", "--no-deps", "--rm", "api", "alembic", "upgrade", "head")
    )
    Invoke-CheckedNative -Executable "docker" -Arguments @(
        $composeArgs + @(
            "up", "-d", "--wait", "minio", "dnd-mcp", "coc-mcp", "agent", "module-worker", "api"
        )
    )
    Write-Host "Isolated restore completed for project $ProjectName. Proxy was not started."
    Write-Host "Run the acceptance checks before directing any traffic to this project."
} finally {
    Pop-Location
}
