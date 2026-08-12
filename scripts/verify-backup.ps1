param([Parameter(Mandatory = $true)][string]$BackupDirectory)
$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath($BackupDirectory)
$manifestPath = Join-Path $root "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "manifest.json is missing" }
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.schema_version -ne 1) { throw "Unsupported backup manifest version" }
if ($manifest.consistency -ne "application-writers-stopped") {
    throw "Backup does not claim a consistent write boundary"
}
$required = @("control.dump", "object-data.tgz", "dnd-state.tgz", "agent-workspace.tgz")
$names = @($manifest.files | ForEach-Object { $_.name })
foreach ($name in $required) {
    if ($name -notin $names) { throw "Backup manifest is missing $name" }
}
foreach ($entry in $manifest.files) {
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $root $entry.name))
    $prefix = $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe backup path: $($entry.name)"
    }
    $file = Get-Item -LiteralPath $candidate -ErrorAction Stop
    if ($file.Length -ne $entry.size_bytes) { throw "Backup size mismatch: $($entry.name)" }
    $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidate).Hash.ToLower()
    if ($digest -ne $entry.sha256) { throw "Backup checksum mismatch: $($entry.name)" }
}
Write-Host "Backup verified: $root"
