param([Parameter(Mandatory = $true)][string]$BackupDirectory)
$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath($BackupDirectory)
$manifestPath = Join-Path $root "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "manifest.json is missing" }
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.schema_version -ne 2) { throw "Unsupported production backup manifest version" }
if ($manifest.consistency -ne "application-writers-stopped") { throw "Backup does not claim a consistent write boundary" }
$required = @("control.dump", "dnd-state.tgz", "coc-state.tgz", "agent-workspace.tgz")
$names = @($manifest.files | ForEach-Object { $_.name })
foreach ($name in $required) { if ($name -notin $names) { throw "Backup manifest is missing $name" } }
if (-not (Test-Path -LiteralPath (Join-Path $root "object-storage") -PathType Container)) { throw "object-storage directory is missing" }
foreach ($entry in $manifest.files) {
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $root ($entry.name -replace "/", [IO.Path]::DirectorySeparatorChar)))
    $prefix = $root.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe backup path: $($entry.name)" }
    $file = Get-Item -LiteralPath $candidate -ErrorAction Stop
    if ($file.Length -ne $entry.size_bytes) { throw "Backup size mismatch: $($entry.name)" }
    $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidate).Hash.ToLower()
    if ($digest -ne $entry.sha256) { throw "Backup checksum mismatch: $($entry.name)" }
}
Write-Host "Production backup verified: $root"
