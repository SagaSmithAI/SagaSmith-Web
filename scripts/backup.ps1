param([string]$Destination = "")
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Destination) {
    $Destination = Join-Path $repo ("backups\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
$resolvedParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $Destination))
$resolvedDestination = [System.IO.Path]::GetFullPath($Destination)
New-Item -ItemType Directory -Force -Path $resolvedDestination | Out-Null

Push-Location $repo
try {
    docker compose exec -T postgres pg_dump -U sagasmith -d sagasmith_service -Fc -f /tmp/control.dump
    docker compose cp postgres:/tmp/control.dump (Join-Path $resolvedDestination "control.dump")
    docker compose exec -T postgres rm -f /tmp/control.dump
    $volumes = @("object-data", "dnd-state", "agent-workspace")
    foreach ($volume in $volumes) {
        $source = "sagasmith-service_$volume"
        docker run --rm --mount "source=$source,target=/source,readonly" `
            --mount "type=bind,source=$resolvedDestination,target=/backup" `
            alpine:3.22 tar czf "/backup/$volume.tgz" -C /source .
    }
    $manifest = @{
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        files = (Get-ChildItem -LiteralPath $resolvedDestination -File | ForEach-Object {
            @{ name = $_.Name; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLower() }
        })
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 `
        -LiteralPath (Join-Path $resolvedDestination "manifest.json")
    Write-Host "Backup completed: $resolvedDestination"
} finally {
    Pop-Location
}
