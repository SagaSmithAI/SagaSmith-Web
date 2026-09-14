param(
    [Parameter(Mandatory = $true)][string]$BackupDirectory,
    [string]$ProjectName = "sagasmith-service-beta-restore",
    [string]$EnvFile = ".env.production",
    [string[]]$ComposeFiles = @("compose.yaml", "compose.production.yaml"),
    [Parameter(Mandatory = $true)][string]$ConfirmRestore,
    [Parameter(Mandatory = $true)][string]$ObjectBucket
)
$ErrorActionPreference = "Stop"

function Invoke-CheckedNative {
    param([Parameter(Mandatory = $true)][string]$Executable, [Parameter(Mandatory = $true)][string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable exited with code $LASTEXITCODE" }
}

$repo = Split-Path -Parent $PSScriptRoot
$backup = [System.IO.Path]::GetFullPath($BackupDirectory)
if ($ProjectName -eq "sagasmith-service-beta") { throw "Live beta project restore is forbidden. Use a distinct isolated ProjectName." }
if ($ConfirmRestore -ne "RESTORE-$ProjectName") { throw "Confirmation mismatch. Pass -ConfirmRestore RESTORE-$ProjectName" }
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw "AWS CLI is required for the external S3 restore." }
$manifest = Get-Content -Raw -LiteralPath (Join-Path $backup "manifest.json") | ConvertFrom-Json
$sourceBucket = [string]$manifest.object_storage.bucket
if (-not $sourceBucket) { throw "Backup manifest does not identify its source object bucket." }
if ($sourceBucket -eq $ObjectBucket) {
    throw "Restore target bucket must differ from the backup source bucket ($sourceBucket)."
}
$endpoint = [Environment]::GetEnvironmentVariable("SAGASMITH_OBJECT_ENDPOINT")
$region = [Environment]::GetEnvironmentVariable("SAGASMITH_OBJECT_REGION")
$accessKey = [Environment]::GetEnvironmentVariable("SAGASMITH_OBJECT_ACCESS_KEY")
$secretKey = [Environment]::GetEnvironmentVariable("SAGASMITH_OBJECT_SECRET_KEY")
if (-not $endpoint -or -not $region -or -not $accessKey -or -not $secretKey) {
    throw "External S3 credentials and endpoint must be supplied in the process environment."
}
$endpointUri = $null
if (-not [Uri]::TryCreate($endpoint, [UriKind]::Absolute, [ref]$endpointUri) -or
    $endpointUri.Scheme -ne "https" -or $endpointUri.IsLoopback) {
    throw "Production object endpoint must be an external HTTPS endpoint."
}
$previousAwsAccessKey = [Environment]::GetEnvironmentVariable("AWS_ACCESS_KEY_ID")
$previousAwsSecretKey = [Environment]::GetEnvironmentVariable("AWS_SECRET_ACCESS_KEY")
$previousAwsRegion = [Environment]::GetEnvironmentVariable("AWS_DEFAULT_REGION")
$previousObjectBucket = [Environment]::GetEnvironmentVariable("SAGASMITH_OBJECT_BUCKET")
$env:AWS_ACCESS_KEY_ID = $accessKey
$env:AWS_SECRET_ACCESS_KEY = $secretKey
$env:AWS_DEFAULT_REGION = $region
$env:SAGASMITH_OBJECT_BUCKET = $ObjectBucket
Push-Location $repo
try {
    $keyCount = (Invoke-CheckedNative -Executable "aws" -Arguments @(
        "s3api", "list-objects-v2", "--bucket", $ObjectBucket, "--max-keys", "1",
        "--query", "KeyCount", "--output", "text", "--endpoint-url", $endpoint
    )).Trim()
    if ($keyCount -ne "0") { throw "Restore target bucket must be empty: s3://$ObjectBucket" }
    & (Join-Path $PSScriptRoot "verify-production-backup.ps1") -BackupDirectory $backup
    $composeArgs = @("compose", "--env-file", $EnvFile, "-p", $ProjectName)
    foreach ($composeFile in $ComposeFiles) { $composeArgs += @("-f", $composeFile) }
    $existingVolumes = @(Invoke-CheckedNative -Executable "docker" -Arguments @("volume", "ls", "--quiet") | Where-Object { $_ -like "${ProjectName}_*" })
    if ($existingVolumes.Count -gt 0) { throw "Restore project already has volumes. Use a fresh isolated ProjectName." }
    Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("up", "-d", "--wait", "postgres"))
    foreach ($volume in @("dnd-state", "coc-state", "agent-workspace")) {
        $target = "${ProjectName}_$volume"
        Invoke-CheckedNative -Executable "docker" -Arguments @("volume", "create", "--label", "com.docker.compose.project=$ProjectName", "--label", "com.docker.compose.volume=$volume", $target) | Out-Null
        Invoke-CheckedNative -Executable "docker" -Arguments @(
            "run", "--rm", "--mount", "source=$target,target=/target",
            "--mount", "type=bind,source=$backup,target=/backup,readonly",
            "alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce",
            "sh", "-c", "tar xzf /backup/$volume.tgz -C /target"
        )
    }
    Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("cp", (Join-Path $backup "control.dump"), "postgres:/tmp/control.dump"))
    Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("exec", "-T", "postgres", "pg_restore", "-U", "sagasmith", "-d", "sagasmith_service", "--clean", "--if-exists", "/tmp/control.dump"))
    Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("run", "--no-deps", "--rm", "api", "alembic", "upgrade", "head"))

    Invoke-CheckedNative -Executable "aws" -Arguments @("s3", "sync", (Join-Path $backup "object-storage"), "s3://$ObjectBucket", "--endpoint-url", $endpoint, "--no-progress")
    Invoke-CheckedNative -Executable "docker" -Arguments @($composeArgs + @("up", "-d", "--wait", "dnd-mcp", "coc-mcp", "agent", "module-worker", "api"))
    Write-Host "Isolated production restore completed for project $ProjectName. Proxy was not started. Object target was s3://$ObjectBucket. Run production_acceptance.py and the authenticated checklist before directing traffic."
} finally {
    Pop-Location
    if ($null -eq $previousAwsAccessKey) { Remove-Item Env:AWS_ACCESS_KEY_ID -ErrorAction SilentlyContinue } else { $env:AWS_ACCESS_KEY_ID = $previousAwsAccessKey }
    if ($null -eq $previousAwsSecretKey) { Remove-Item Env:AWS_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue } else { $env:AWS_SECRET_ACCESS_KEY = $previousAwsSecretKey }
    if ($null -eq $previousAwsRegion) { Remove-Item Env:AWS_DEFAULT_REGION -ErrorAction SilentlyContinue } else { $env:AWS_DEFAULT_REGION = $previousAwsRegion }
    if ($null -eq $previousObjectBucket) { Remove-Item Env:SAGASMITH_OBJECT_BUCKET -ErrorAction SilentlyContinue } else { $env:SAGASMITH_OBJECT_BUCKET = $previousObjectBucket }
}
