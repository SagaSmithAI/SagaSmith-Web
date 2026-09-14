param(
    [string]$Destination = "",
    [string]$ProjectName = "sagasmith-service-beta",
    [string]$EnvFile = ".env.production",
    [string[]]$ComposeFiles = @("compose.yaml", "compose.production.yaml")
)
$ErrorActionPreference = "Stop"

function Invoke-CheckedNative {
    param([Parameter(Mandatory = $true)][string]$Executable, [Parameter(Mandatory = $true)][string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable exited with code $LASTEXITCODE" }
}

$repo = Split-Path -Parent $PSScriptRoot
if (-not $Destination) { $Destination = Join-Path $repo ("backups\production-" + (Get-Date -Format "yyyyMMdd-HHmmss")) }
$resolvedDestination = [System.IO.Path]::GetFullPath($Destination)
if ($resolvedDestination -eq [System.IO.Path]::GetFullPath($repo)) { throw "Backup destination cannot be the repository root." }
if (Test-Path -LiteralPath $resolvedDestination -PathType Leaf) { throw "Backup destination must be a directory." }
if (Test-Path -LiteralPath $resolvedDestination -PathType Container) {
    $existingEntries = @(Get-ChildItem -LiteralPath $resolvedDestination -Force)
    if ($existingEntries.Count -gt 0) { throw "Backup destination must be empty: $resolvedDestination" }
} else {
    New-Item -ItemType Directory -Force -Path $resolvedDestination | Out-Null
}
$objectDirectory = Join-Path $resolvedDestination "object-storage"
New-Item -ItemType Directory -Force -Path $objectDirectory | Out-Null

$objectEndpoint = [Environment]::GetEnvironmentVariable("SAGASMITH_OBJECT_ENDPOINT")
$objectRegion = [Environment]::GetEnvironmentVariable("SAGASMITH_OBJECT_REGION")
$objectBucket = [Environment]::GetEnvironmentVariable("SAGASMITH_OBJECT_BUCKET")
$objectAccessKey = [Environment]::GetEnvironmentVariable("SAGASMITH_OBJECT_ACCESS_KEY")
$objectSecretKey = [Environment]::GetEnvironmentVariable("SAGASMITH_OBJECT_SECRET_KEY")
if (-not $objectEndpoint -or -not $objectRegion -or -not $objectBucket -or -not $objectAccessKey -or -not $objectSecretKey) {
    throw "SAGASMITH_OBJECT_ENDPOINT, REGION, BUCKET, ACCESS_KEY, and SECRET_KEY are required in the process environment."
}
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw "AWS CLI is required for the external S3 backup." }
$endpointUri = $null
if (-not [Uri]::TryCreate($objectEndpoint, [UriKind]::Absolute, [ref]$endpointUri) -or
    $endpointUri.Scheme -ne "https" -or $endpointUri.IsLoopback) {
    throw "Production object endpoint must be an external HTTPS endpoint."
}
$env:AWS_ACCESS_KEY_ID = $objectAccessKey
$env:AWS_SECRET_ACCESS_KEY = $objectSecretKey
$env:AWS_DEFAULT_REGION = $objectRegion

$stopped = $false
Push-Location $repo
try {
    $composeArgs = @("compose", "--env-file", $EnvFile, "-p", $ProjectName)
    foreach ($composeFile in $ComposeFiles) { $composeArgs += @("-f", $composeFile) }
    Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("stop", "api", "module-worker", "agent", "dnd-mcp", "coc-mcp"))
    $stopped = $true
    Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("exec", "-T", "postgres", "pg_dump", "-U", "sagasmith", "-d", "sagasmith_service", "-Fc", "-f", "/tmp/control.dump"))
    Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("cp", "postgres:/tmp/control.dump", (Join-Path $resolvedDestination "control.dump")))
    Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("exec", "-T", "postgres", "rm", "-f", "/tmp/control.dump"))
    foreach ($volume in @("dnd-state", "coc-state", "agent-workspace")) {
        $source = "${ProjectName}_$volume"
        Invoke-CheckedNative -Executable "docker" -Arguments @(
            "run", "--rm", "--mount", "source=$source,target=/source,readonly",
            "--mount", "type=bind,source=$resolvedDestination,target=/backup",
            "alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce",
            "tar", "czf", "/backup/$volume.tgz", "-C", "/source", "."
        )
    }
    Invoke-CheckedNative -Executable "aws" -Arguments @("s3", "sync", "s3://$objectBucket", $objectDirectory, "--endpoint-url", $objectEndpoint, "--no-progress")
    $release = (Invoke-CheckedNative -Executable "git" -Arguments @("rev-parse", "HEAD")).Trim()
    $workingTreeDirty = [bool](Invoke-CheckedNative -Executable "git" -Arguments @("status", "--porcelain"))
    $imageJson = Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("images", "--format", "json"))
    $images = @(
        foreach ($line in @($imageJson)) {
            $jsonLine = ([string]$line).Trim()
            if (-not $jsonLine) { continue }
            ConvertFrom-Json -InputObject $jsonLine
        }
    )
    $files = Get-ChildItem -LiteralPath $resolvedDestination -Recurse -File | Where-Object { $_.Name -ne "manifest.json" } | ForEach-Object {
        $relative = $_.FullName.Substring($resolvedDestination.Length).TrimStart([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) -replace "\\", "/"
        @{ name = $relative; size_bytes = $_.Length; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLower() }
    }
    $manifest = @{
        schema_version = 2
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        service_release = $release
        working_tree_dirty = $workingTreeDirty
        consistency = "application-writers-stopped"
        compose_files = @($ComposeFiles)
        object_storage = @{ backend = "s3"; endpoint = $objectEndpoint; region = $objectRegion; bucket = $objectBucket; credentials = "external-secret-store" }
        images = @($images)
        files = @($files)
    }
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $resolvedDestination "manifest.json")
    & (Join-Path $PSScriptRoot "verify-production-backup.ps1") -BackupDirectory $resolvedDestination
    Write-Host "Production backup completed and verified: $resolvedDestination"
} finally {
    if ($stopped) { Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("up", "-d", "--wait", "dnd-mcp", "coc-mcp", "agent", "module-worker", "api")) }
    Pop-Location
    Remove-Item Env:AWS_ACCESS_KEY_ID, Env:AWS_SECRET_ACCESS_KEY, Env:AWS_DEFAULT_REGION -ErrorAction SilentlyContinue
}
