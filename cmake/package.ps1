param([string]$Ref = 'HEAD', [string]$Output = 'build/release/Installation.zip')

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$outputPath = if ([IO.Path]::IsPathRooted($Output)) { [IO.Path]::GetFullPath($Output) }
    else { [IO.Path]::GetFullPath((Join-Path $projectRoot $Output)) }
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($outputPath)) | Out-Null
if (Test-Path -LiteralPath $outputPath) { throw 'Package already exists; choose a new output path.' }

# Archive only committed build inputs, instructions and required license notices.
$inputs = @(
    'Build.cmd', 'CMakeLists.txt', 'CMakePresets.json', 'README.md', 'BUILDING.txt',
    'LICENSE', 'NOTICE.md', 'THIRD_PARTY_NOTICES.txt',
    'cmake/build.ps1', 'cmake/target.h.in', 'cmake/kernel_names.h.in',
    'cmake/cpu-seeds.txt.in', 'cmake/imgui-shaders.cmake',
    'knowledge/facts.json', 'knowledge/supported_target.json', 'src', 'resources'
)
$temporary = "$outputPath.$([Guid]::NewGuid()).tmp"
try {
    & git archive --format=zip "--output=$temporary" $Ref -- @inputs
    if ($LASTEXITCODE -ne 0) { throw 'Creating the installation archive failed.' }
    [IO.File]::Move($temporary, $outputPath)
} finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
}
Get-Item -LiteralPath $outputPath | Select-Object Name, Length
