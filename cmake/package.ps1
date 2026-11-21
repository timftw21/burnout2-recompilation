param([string]$Ref = 'HEAD', [string]$Output = 'build/release', [switch]$SkipBuild)

# Release assets: Burnout2-Setup.exe installs the prebuilt Windows game from a
# player's own disc without downloading anything. Installation.zip carries the
# Linux/macOS/Steam Deck build sources. Neither contains game media.
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1')
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location -LiteralPath $projectRoot
$buildRoot = Join-Path $projectRoot 'build'
$gameBuild = Join-Path $buildRoot 'windows'
$outputRoot = if ([IO.Path]::IsPathRooted($Output)) { [IO.Path]::GetFullPath($Output) }
    else { [IO.Path]::GetFullPath((Join-Path $projectRoot $Output)) }
$setupPath = Join-Path $outputRoot 'Burnout2-Setup.exe'
$archivePath = Join-Path $outputRoot 'Installation.zip'
foreach ($path in @($setupPath, "$setupPath.sha256", $archivePath)) {
    if (Test-Path -LiteralPath $path) { throw "Package already exists; choose a new output folder: $path" }
}

# Reuse the source-build toolchain detection without its setup window.
$NoGui = $true
$cancelled = $false
function Show-Status([string]$message) { Write-Host $message }
function Show-TaskProgress([long]$completed, [long]$total) {}
function Run-Step([string]$message, [string]$program, [string[]]$arguments, [switch]$Build, [string]$Directory = $projectRoot) {
    Write-Host $message
    Push-Location -LiteralPath $Directory
    try { & $program @arguments; if ($LASTEXITCODE -ne 0) { throw "$message failed with exit code $LASTEXITCODE." } }
    finally { Pop-Location }
}
. (Join-Path $PSScriptRoot 'windows-dependencies.ps1')
Initialize-WindowsTools

$cache = Join-Path $gameBuild 'CMakeCache.txt'
if (!(Test-Path -LiteralPath $cache) -or !(Select-String -LiteralPath $cache -Pattern '^B2_XBE:FILEPATH=.+' -Quiet)) {
    throw 'Build the game once with Build.cmd and your disc image before packaging.'
}
if (!$SkipBuild) { Run-Step 'Building the game and diagnostics...' $cmake @('--build', '--preset', 'release', '--target', 'b2', 'b2-tool') }

$package = Join-Path $buildRoot 'package'
$stage = Join-Path $package ('stage-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($stage) | Out-Null
[IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$temporaryArchive = "$archivePath.$([Guid]::NewGuid().ToString('N')).tmp"
try {
    Run-Step 'Staging release files...' $cmake @('--install', $gameBuild, '--config', 'Release', '--prefix', $stage, '--component', 'Runtime')
    $files = @(Get-ChildItem -LiteralPath $stage -File -Recurse | Sort-Object FullName)
    $manifest = @('B2-PAYLOAD-V1')
    $cabinet = Join-Path $package 'payload.cab'
    $directives = @('.OPTION EXPLICIT', '.Set CabinetNameTemplate=payload.cab', ('.Set DiskDirectoryTemplate="' + $package + '"'),
        '.Set Cabinet=on', '.Set Compress=on', '.Set CompressionType=LZX', '.Set CompressionMemory=21',
        '.Set MaxDiskSize=0', '.Set MaxCabinetSize=0', '.Set MaxErrors=1',
        ('.Set RptFileName="' + (Join-Path $package 'payload.rpt') + '"'), ('.Set InfFileName="' + (Join-Path $package 'payload.inf') + '"'))
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($stage.TrimEnd('\').Length + 1)
        if ($relative -match '[^\x20-\x7e]' -or $relative -match '[\r\n\t"]' -or $relative -match '^(data|logs|captures|build|mods)[\\/]') {
            throw "Unexpected release file: $relative"
        }
        $manifest += ($relative.Replace('\', '/') + "`t" + $file.Length + "`t" + (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash)
        $directives += ('"' + $file.FullName + '" "' + $relative + '"')
    }
    foreach ($required in @('b2.exe', 'b2-tool.exe', 'vcruntime140.dll', 'msvcp140.dll')) {
        if (!(Test-Path -LiteralPath (Join-Path $stage $required))) { throw "The release payload is missing $required." }
    }
    $manifestPath = Join-Path $package 'payload.tsv'
    $recipe = Join-Path $package 'payload.ddf'
    [IO.File]::WriteAllText($manifestPath, ($manifest -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllLines($recipe, $directives, [Text.Encoding]::Default)
    if (Test-Path -LiteralPath $cabinet) { Remove-Item -LiteralPath $cabinet }
    Run-Step 'Compressing the release payload...' "$env:SystemRoot\System32\makecab.exe" @('/V0', '/F', $recipe)
    Run-Step 'Preparing Setup...' $cmake @('-S', $projectRoot, '-B', $gameBuild,
        "-DB2_INSTALLER_PAYLOAD=$($cabinet.Replace('\', '/'))", "-DB2_INSTALLER_MANIFEST=$($manifestPath.Replace('\', '/'))")
    Run-Step 'Building Setup...' $cmake @('--build', $gameBuild, '--config', 'Release', '--target', 'b2-setup')
    Copy-Item -LiteralPath (Join-Path $gameBuild 'Release\b2-setup.exe') -Destination $setupPath
    $checksum = (Get-FileHash -LiteralPath $setupPath -Algorithm SHA256).Hash
    [IO.File]::WriteAllText("$setupPath.sha256", "$checksum  Burnout2-Setup.exe`n", [Text.UTF8Encoding]::new($false))

    # Archive only committed Linux/macOS build inputs, instructions and license notices.
    $inputs = @(
        'Build.sh', 'Install.sh', 'Install.command', 'Install.desktop', 'Play.sh',
        'CMakeLists.txt', 'CMakePresets.json', 'README.md', 'BUILDING.txt',
        'LICENSE', 'NOTICE.md', 'THIRD_PARTY_NOTICES.txt', 'third_party/lua-5.4.9.tar.gz',
        'cmake/target.h.in', 'cmake/kernel_names.h.in', 'cmake/cpu-seeds.txt.in', 'cmake/imgui-shaders.cmake',
        'cmake/linux.cmake', 'cmake/posix-dependencies.sh', 'cmake/vkd3d-copy-propagation.patch',
        'cmake/steamdeck.sh', 'cmake/cmake-linux.sh', 'cmake/vkd3d-json.patch',
        'cmake/dxvk-native.cmake', 'cmake/build-dxvk.cmake', 'cmake/dxvk-native.patch', 'cmake/dxvk-pnp.pl',
        'cmake/macos.cmake', 'cmake/macos-dependencies.sh', 'cmake/dxmt.cmake',
        'cmake/portable-cpu.cmake', 'cmake/binary-header.cmake',
        'cmake/install-dialog.applescript',
        'knowledge/facts.json', 'knowledge/supported_target.json', 'src', 'resources'
    )
    & $git archive --format=zip "--output=$temporaryArchive" $Ref -- @inputs
    if ($LASTEXITCODE -ne 0) { throw 'Creating the installation archive failed.' }
    [IO.File]::Move($temporaryArchive, $archivePath)
    [pscustomobject]@{ setup = $setupPath; setup_bytes = (Get-Item -LiteralPath $setupPath).Length; sha256 = $checksum
        payload_files = $files.Count; payload_bytes = ($files | Measure-Object Length -Sum).Sum
        archive = $archivePath; archive_bytes = (Get-Item -LiteralPath $archivePath).Length
        source_commit = (& $git rev-parse HEAD); source_modified = [bool](& $git status --porcelain --untracked-files=no) } | Format-List
} finally {
    if (Test-Path -LiteralPath $temporaryArchive) { Remove-Item -LiteralPath $temporaryArchive }
    # Only remove the exact staging directory created by this invocation.
    if ([IO.Path]::GetDirectoryName($stage) -eq $package -and [IO.Path]::GetFileName($stage) -match '^stage-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
