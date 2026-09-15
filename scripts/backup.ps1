param(
    [string]$Destination = "",
    [string]$ProjectName = "sagasmith-service",
    [string[]]$ComposeFiles = @("compose.yaml"),
    [string]$EnvFile = ""
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "backup-hash.ps1")

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
if (-not $Destination) {
    $Destination = Join-Path $repo ("backups\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
$resolvedDestination = [System.IO.Path]::GetFullPath($Destination)
if ($resolvedDestination -eq [System.IO.Path]::GetFullPath($repo)) {
    throw "Backup destination cannot be the repository root."
}
if (Test-Path -LiteralPath $resolvedDestination -PathType Leaf) {
    throw "Backup destination must be a directory."
}
if (Test-Path -LiteralPath $resolvedDestination -PathType Container) {
    $existingEntries = @(Get-ChildItem -LiteralPath $resolvedDestination -Force)
    if ($existingEntries.Count -gt 0) {
        throw "Backup destination must be empty: $resolvedDestination"
    }
} else {
    New-Item -ItemType Directory -Force -Path $resolvedDestination | Out-Null
}

$writers = @("api", "module-worker", "agent", "dnd-mcp", "coc-mcp", "minio")
$stopped = $false
Push-Location $repo
try {
    $composeArgs = @("compose", "-p", $ProjectName)
    if ($EnvFile) { $composeArgs += @("--env-file", $EnvFile) }
    foreach ($composeFile in $ComposeFiles) {
        $composeArgs += @("-f", $composeFile)
    }
    $enabledServices = @(Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("config", "--services")))
    $writers = @($writers | Where-Object { $_ -in $enabledServices })
    Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("stop") + $writers)
    $stopped = $true
    Invoke-CheckedNative -Executable "docker" -Arguments @(
        $composeArgs + @("exec", "-T", "postgres", "pg_dump", "-U", "sagasmith", "-d", "sagasmith_service", "-Fc", "-f", "/tmp/control.dump")
    )
    Invoke-CheckedNative -Executable "docker" -Arguments @(
        $composeArgs + @("cp", "postgres:/tmp/control.dump", (Join-Path $resolvedDestination "control.dump"))
    )
    Invoke-CheckedNative -Executable "docker" -Arguments @(
        $composeArgs + @("exec", "-T", "postgres", "rm", "-f", "/tmp/control.dump")
    )
    $volumes = @("object-data", "dnd-state", "coc-state", "agent-workspace")
    if ("coc-mcp" -notin $enabledServices) { $volumes = @($volumes | Where-Object { $_ -ne "coc-state" }) }
    foreach ($volume in $volumes) {
        $source = "${ProjectName}_$volume"
        Invoke-CheckedNative -Executable "docker" -Arguments @(
            "run", "--rm", "--mount", "source=$source,target=/source,readonly",
            "--mount", "type=bind,source=$resolvedDestination,target=/backup",
            "alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce",
            "tar", "czf", "/backup/$volume.tgz", "-C", "/source", "."
        )
    }
    $release = (Invoke-CheckedNative -Executable "git" -Arguments @("rev-parse", "HEAD")).Trim()
    $workingTreeDirty = [bool](Invoke-CheckedNative -Executable "git" -Arguments @("status", "--porcelain"))
    $imageJson = Invoke-CheckedNative -Executable "docker" -Arguments @(
        $composeArgs + @("images", "--format", "json")
    )
    $images = ConvertFrom-Json -InputObject (($imageJson -join [Environment]::NewLine).Trim())
    $manifest = @{
        schema_version = 1
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        service_release = $release
        working_tree_dirty = $workingTreeDirty
        consistency = "application-writers-stopped"
        compose_files = @($ComposeFiles)
        images = @($images)
        files = (Get-ChildItem -LiteralPath $resolvedDestination -File | ForEach-Object {
            @{
                name = $_.Name
                size_bytes = $_.Length
                sha256 = Get-BackupSha256 -LiteralPath $_.FullName
            }
        })
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 `
        -LiteralPath (Join-Path $resolvedDestination "manifest.json")
    & (Join-Path $PSScriptRoot "verify-backup.ps1") -BackupDirectory $resolvedDestination
    Write-Host "Backup completed and verified: $resolvedDestination"
} finally {
    if ($stopped) {
        Invoke-CheckedNative -Executable "docker" -Arguments @(
            $composeArgs + @("up", "-d", "--wait") + $writers
        )
    }
    Pop-Location
}
