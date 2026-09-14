param(
    [Parameter(Mandatory = $true)][string]$BackupDirectory,
    [Parameter(Mandatory = $true)][string]$AgeRecipient,
    [string]$OutputFile = "",
    [switch]$RemovePlaintext
)
$ErrorActionPreference = "Stop"
if (-not (Get-Command age -ErrorAction SilentlyContinue)) { throw "age is required for off-host backup encryption." }
$backup = [System.IO.Path]::GetFullPath($BackupDirectory)
if (-not (Test-Path -LiteralPath (Join-Path $backup "manifest.json") -PathType Leaf)) { throw "Backup manifest is missing." }
if (-not $OutputFile) { $OutputFile = "$backup.age" }
$output = [System.IO.Path]::GetFullPath($OutputFile)
$parent = Split-Path -Parent $output
New-Item -ItemType Directory -Force -Path $parent | Out-Null
$archive = Join-Path ([System.IO.Path]::GetTempPath()) (([System.IO.Path]::GetRandomFileName()) + ".tar.gz")
try {
    tar -czf $archive -C (Split-Path -Parent $backup) (Split-Path -Leaf $backup)
    if ($LASTEXITCODE -ne 0) { throw "tar exited with code $LASTEXITCODE" }
    & age --recipient $AgeRecipient --output $output $archive
    if ($LASTEXITCODE -ne 0) { throw "age exited with code $LASTEXITCODE" }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $output).Hash.ToLower()
    Write-Host "Encrypted backup written: $output"
    Write-Host "Encrypted backup SHA256: $hash"
    if ($RemovePlaintext) { Remove-Item -LiteralPath $backup -Recurse -Force }
} finally {
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
}
