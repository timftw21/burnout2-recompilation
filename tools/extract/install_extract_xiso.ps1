param(
    [string]$Tag = "build-202505152050",
    [ValidateSet("Win64_Release", "Win32_Release")]
    [string]$Asset = "Win64_Release",
    [string]$InstallRoot = (Join-Path $PSScriptRoot "..\..\data\local\tools\extract-xiso")
)

$ErrorActionPreference = "Stop"

$repo = "XboxDev/extract-xiso"
$releaseUri = if ($Tag -eq "latest") {
    "https://api.github.com/repos/$repo/releases/latest"
} else {
    "https://api.github.com/repos/$repo/releases/tags/$Tag"
}

$release = Invoke-RestMethod -Uri $releaseUri
$assetName = "extract-xiso-$Asset.zip"
$releaseAsset = $release.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
if (-not $releaseAsset) {
    throw "Release asset not found: $assetName"
}

$resolvedRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$installDir = Join-Path $resolvedRoot (Join-Path $release.tag_name $Asset)
$downloadDir = Join-Path $resolvedRoot "downloads"
$zipPath = Join-Path $downloadDir $assetName

New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
New-Item -ItemType Directory -Force -Path $installDir | Out-Null

Invoke-WebRequest -Uri $releaseAsset.browser_download_url -OutFile $zipPath
$zipHash = Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath

Expand-Archive -LiteralPath $zipPath -DestinationPath $installDir -Force

$exe = Get-ChildItem -LiteralPath $installDir -Recurse -File |
    Where-Object { $_.Name -ieq "extract-xiso.exe" -or $_.Name -ieq "extract-xiso" } |
    Select-Object -First 1
if (-not $exe) {
    throw "extract-xiso executable was not found after extracting $zipPath"
}

$exeHash = Get-FileHash -Algorithm SHA256 -LiteralPath $exe.FullName
$manifest = [ordered]@{
    repo = $repo
    tag = $release.tag_name
    asset = $releaseAsset.name
    asset_url = $releaseAsset.browser_download_url
    zip_sha256 = $zipHash.Hash
    executable_path = $exe.FullName
    executable_sha256 = $exeHash.Hash
    installed_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
}

$manifestPath = Join-Path $installDir "install-manifest.json"
$manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Host "Installed extract-xiso:"
Write-Host "  $($exe.FullName)"
Write-Host "Manifest:"
Write-Host "  $manifestPath"
