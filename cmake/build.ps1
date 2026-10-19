param([string]$DiscImage, [switch]$NoGui)

$ErrorActionPreference = 'Stop'
# Use this shell's tools even when another PowerShell version launched it.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1')
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location -LiteralPath $projectRoot
$buildRoot = Join-Path $projectRoot 'build'
$logRoot = Join-Path $buildRoot 'setup'
$gameDirectory = Join-Path $buildRoot 'windows\Release'
$logPath = Join-Path $logRoot 'build.log'
$buildLock = $null
$workers = [Collections.Generic.Dictionary[int,object]]::new()
$cancelled = $false
$busy = $true
$step = 0
$form = $null
$stage = 1
$stageNames = @('Check requirements', 'Prepare build tools', 'Read game files', 'Configure the game', 'Compile the game', 'Finish installation')
$taskProgress = @{ Completed = 0L; Total = 0L }
$previousNinjaStatus = $env:NINJA_STATUS
$env:NINJA_STATUS = '[%f/%t] '

function Set-Stage([int]$number) {
    $script:stage = $number
    if (!$NoGui) { Update-SetupStage }
}

function Show-TaskProgress([long]$completed, [long]$total) {
    if ($completed -lt 0 -or $total -lt 0 -or $completed -gt $total) { return }
    $taskProgress.Completed = $completed
    $taskProgress.Total = $total
    if (!$NoGui) {
        $progress.Value = if ($total) { [int](1000.0 * $completed / $total) } else { 0 }
        $taskCount.Text = if ($total) { '{0:N0} of {1:N0} tasks complete' -f $completed, $total }
            else { 'Working. Progress updates as each task finishes.' }
        $progress.AccessibleDescription = $taskCount.Text
    }
}

function Read-BuildProgress($tail) {
    # Read only new output, including status lines split across polling reads.
    # Tool reports can contain very long lines; never retain a full report here.
    while (($count = $tail.Reader.Read($tail.Buffer, 0, $tail.Buffer.Length)) -gt 0) {
        $tail.Pending += [string]::new($tail.Buffer, 0, $count)
        while (($end = $tail.Pending.IndexOf("`n")) -ge 0) {
            $line = $tail.Pending.Substring(0, $end).TrimEnd("`r")
            $tail.Pending = $tail.Pending.Substring($end + 1)
            if ($line -match '^\[(\d{1,9})/(\d{1,9})\](?:\s|$)') {
                Show-TaskProgress ([long]$Matches[1]) ([long]$Matches[2])
            }
        }
        if ($tail.Pending.Length -gt 8192) { $tail.Pending = $tail.Pending.Substring($tail.Pending.Length - 8192) }
    }
}

function Show-Status([string]$message) {
    if ($buildLock) { [IO.File]::AppendAllText($logPath, "$message`r`n") }
    if ($NoGui) { Write-Host $message }
    else { $status.Text = $message; [Windows.Forms.Application]::DoEvents() }
}

function Record-Workers($process) {
    $all = Get-CimInstance Win32_Process
    $queue = [Collections.Generic.Queue[object]]::new()
    $queue.Enqueue(@{ Id = $process.Id; Ticks = $process.StartTime.ToUniversalTime().Ticks })
    while ($queue.Count) {
        $parent = $queue.Dequeue()
        foreach ($child in $all | Where-Object {
            $_.ParentProcessId -eq $parent.Id -and $_.CreationDate.ToUniversalTime().Ticks -ge $parent.Ticks
        }) {
            $record = @{ Id = [int]$child.ProcessId; Ticks = $child.CreationDate.ToUniversalTime().Ticks }
            $workers[$record.Id] = $record
            $queue.Enqueue($record)
        }
    }
    $root = $all | Where-Object ProcessId -eq $process.Id
    if ($root) { $workers[$process.Id] = @{ Id = $process.Id; Ticks = $root.CreationDate.ToUniversalTime().Ticks } }
}

function Stop-Workers {
    if (!$workers.Count) { return }
    foreach ($record in $workers.Values) {
        $live = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)"
        if ($live -and $live.CreationDate.ToUniversalTime().Ticks -eq $record.Ticks) {
            Stop-Process -Id $record.Id -Force -ErrorAction SilentlyContinue
        }
    }
    @($workers.Values) | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $logRoot 'workers.json')
    $workers.Clear()
}

function Run-Step([string]$message, [string]$program, [string[]]$arguments, [switch]$Build) {
    Show-Status $message
    Show-TaskProgress 0 0
    if ($cancelled) { throw 'Build cancelled.' }
    $script:step++
    $outputPath = Join-Path $logRoot "$step.out.log"
    $errorPath = Join-Path $logRoot "$step.err.log"
    # Quote for the native Windows argument parser; never pass paths through a shell.
    $quoted = @($arguments | ForEach-Object { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }) -join ' '
    $process = Start-Process -FilePath $program -ArgumentList $quoted -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $outputPath -RedirectStandardError $errorPath
    # Windows PowerShell must retain the process handle to read its exit code.
    $null = $process.Handle
    $tail = $null
    try {
        if ($Build) {
            $stream = [IO.FileStream]::new($outputPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
            $tail = @{ Reader = [IO.StreamReader]::new($stream); Buffer = [char[]]::new(4096); Pending = '' }
        }
        Record-Workers $process
        $lastPoll = [DateTime]::UtcNow
        while (!$process.WaitForExit(100)) {
            if ($tail) { Read-BuildProgress $tail }
            if (!$NoGui) { [Windows.Forms.Application]::DoEvents() }
            if ($cancelled) { throw 'Build cancelled.' }
            if (([DateTime]::UtcNow - $lastPoll).TotalSeconds -ge 1) {
                Record-Workers $process
                $lastPoll = [DateTime]::UtcNow
            }
        }
        $process.WaitForExit()
        if ($tail) { Read-BuildProgress $tail }
        if ($cancelled) { throw 'Build cancelled.' }
        if ($process.ExitCode -ne 0) { throw "$message failed. Open the build log for details." }
        # Ninja can prune work after discovering generated dependencies. A
        # successful process exit is the authoritative completion signal.
        if ($taskProgress.Total) { Show-TaskProgress $taskProgress.Total $taskProgress.Total }
        else { Show-TaskProgress 1 1 }
    } finally {
        if ($tail) { $tail.Reader.Dispose() }
        try {
            Record-Workers $process
            Stop-Workers
            if (!$process.HasExited) { $process.Kill() }
            $process.WaitForExit()
            foreach ($path in @($outputPath, $errorPath)) {
                $source = [IO.File]::OpenRead($path)
                $destination = [IO.File]::Open($logPath, [IO.FileMode]::Append, [IO.FileAccess]::Write)
                try { $source.CopyTo($destination) } finally { $source.Dispose(); $destination.Dispose() }
            }
        } finally { $process.Dispose() }
    }
}

function Find-Program([string]$name, [string[]]$candidates, [version]$minimum) {
    $paths = @((Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue).Source) + $candidates
    foreach ($candidate in $paths | Select-Object -Unique) {
        if (!$candidate -or !(Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        if ($minimum) {
            $versionLine = @(& $candidate --version)[0]
            if ($LASTEXITCODE -ne 0 -or $versionLine -notmatch '\bversion (\d+\.\d+\.\d+)' -or [version]$Matches[1] -lt $minimum) { continue }
        }
        return $candidate
    }
    if ($name -eq 'ninja.exe') { throw 'Ninja (ninja.exe) is missing. Open Visual Studio Installer, choose Modify and install C++ CMake tools for Windows. For a standalone Ninja download, see README.md.' }
    if ($minimum) { throw "$name $minimum or newer is required. Install it from the link in README.md." }
    throw "$name is missing. Install the build requirements listed in README.md."
}

function Configure-Step([string]$message, [string]$directory, [string[]]$arguments) {
    $directory = [IO.Path]::GetFullPath($directory)
    if (!$directory.StartsWith("$buildRoot\", [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid build directory.' }
    $cachePath = Join-Path $directory 'CMakeCache.txt'
    if (Test-Path -LiteralPath $cachePath) {
        $cached = @{}
        foreach ($line in [IO.File]::ReadAllLines($cachePath)) {
            if ($line -match '^(CMAKE_HOME_DIRECTORY|CMAKE_CACHEFILE_DIR|CMAKE_GENERATOR|CMAKE_GENERATOR_PLATFORM|CMAKE_GENERATOR_INSTANCE|CMAKE_C_COMPILER|CMAKE_CXX_COMPILER|CMAKE_LINKER_TYPE|CMAKE_MAKE_PROGRAM):[^=]+=(.*)$') {
                $cached[$Matches[1]] = $Matches[2]
            }
        }
        $expected = @{ CMAKE_HOME_DIRECTORY=$projectRoot; CMAKE_CACHEFILE_DIR=$directory;
            CMAKE_GENERATOR='Ninja Multi-Config'; CMAKE_GENERATOR_PLATFORM=''; CMAKE_GENERATOR_INSTANCE='';
            CMAKE_C_COMPILER=$clang; CMAKE_CXX_COMPILER=$clang; CMAKE_LINKER_TYPE='LLD'; CMAKE_MAKE_PROGRAM=$ninja }
        foreach ($key in $expected.Keys) {
            if (!$cached.ContainsKey($key) -or $cached[$key].Replace('\', '/') -ne $expected[$key].Replace('\', '/')) {
                # CMake clears only its own configuration metadata, preserving game files and saves.
                $arguments = @('--fresh') + $arguments
                break
            }
        }
    }
    Run-Step $message $cmake $arguments
}

function Copy-Changed([string]$source, [string]$destination) {
    if (!(Test-Path -LiteralPath $destination) -or (Get-FileHash -LiteralPath $source).Hash -ne (Get-FileHash -LiteralPath $destination).Hash) {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
}

if (!$NoGui) {
    . (Join-Path $PSScriptRoot 'build-ui.ps1')
    Initialize-SetupWindow
    Set-Stage 1
    Show-TaskProgress 0 0
    $form.Show()
}

$importDirectory = $null
$exitCode = 0
try {
    [IO.Directory]::CreateDirectory($logRoot) | Out-Null
    try {
        $buildLock = [IO.File]::Open((Join-Path $logRoot 'build.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] {
        if (($_.Exception.HResult -band 0xffff) -in 32, 33) { throw 'A build is already running in this folder. Wait for it to finish.' }
        throw
    }
    [IO.File]::WriteAllText($logPath, '', [Text.UTF8Encoding]::new($false))
    Show-Status 'Checking build requirements...'
    if (![Environment]::Is64BitOperatingSystem) { throw 'A 64-bit Windows PC is required.' }
    $vswhere = Find-Program 'vswhere.exe' @("${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe")
    $visualStudio = & $vswhere -latest -products '*' -version '[18.0,19.0)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (!$visualStudio) { throw 'Install Visual Studio 2026 with Desktop development with C++, including the Windows SDK.' }
    $cmake = Find-Program 'cmake.exe' @("$env:ProgramFiles\CMake\bin\cmake.exe", "$visualStudio\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe") ([version]'4.2.0')
    $git = Find-Program 'git.exe' @("$env:ProgramFiles\Git\cmd\git.exe", "$visualStudio\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\cmd\git.exe")
    $clang = Find-Program 'clang-cl.exe' @("$env:ProgramFiles\LLVM\bin\clang-cl.exe", "$visualStudio\VC\Tools\Llvm\x64\bin\clang-cl.exe") ([version]'22.1.0')
    $llvmDirectory = Split-Path -Parent $clang
    foreach ($name in @('lld-link.exe', 'llvm-lib.exe')) {
        if (!(Test-Path -LiteralPath (Join-Path $llvmDirectory $name) -PathType Leaf)) { throw 'Install the complete LLVM toolchain listed in README.md.' }
    }
    $developerShell = Join-Path $visualStudio 'Common7\Tools\Launch-VsDevShell.ps1'
    if (!(Test-Path -LiteralPath $developerShell)) { throw 'Repair Visual Studio 2026 with Desktop development with C++ and the Windows SDK.' }
    & $developerShell -Arch amd64 -HostArch amd64 -SkipAutomaticLocation -NoLogo
    $ninja = Find-Program 'ninja.exe' @("$visualStudio\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe")
    $env:PATH = "$(Split-Path -Parent $cmake);$(Split-Path -Parent $git);$llvmDirectory;$(Split-Path -Parent $ninja);$env:PATH"
    $toolchain = @("-DCMAKE_C_COMPILER=$clang", "-DCMAKE_CXX_COMPILER=$clang", "-DCMAKE_MAKE_PROGRAM=$ninja")
    if ($cancelled) { throw 'Build cancelled.' }
    if (!$DiscImage) {
        if ($NoGui) { throw 'Specify -DiscImage for a build without the file picker.' }
        $picker = [Windows.Forms.OpenFileDialog]::new()
        $picker.Title = 'Select your US Burnout 2 Xbox XISO disc image'
        $picker.Filter = 'Xbox disc images (*.iso;*.xiso)|*.iso;*.xiso'
        try {
            if ($picker.ShowDialog($form) -ne 'OK') { $script:cancelled = $true; throw 'Build cancelled.' }
            $DiscImage = $picker.FileName
        } finally { $picker.Dispose() }
    }
    $DiscImage = (Resolve-Path -LiteralPath $DiscImage).Path
    if (!(Test-Path -LiteralPath $DiscImage -PathType Leaf)) { throw 'Select an Xbox XISO disc image file.' }
    $bootstrap = Join-Path $buildRoot 'bootstrap'
    Set-Stage 2
    Configure-Step 'Preparing the build tools...' $bootstrap (@('--preset', 'windows', '-B', $bootstrap, '-DB2_XBE=') + $toolchain)
    Run-Step 'Building the game-file reader...' $cmake @('--build', $bootstrap, '--config', 'Release', '--target', 'b2-tool', '--parallel', '6') -Build
    $tool = Join-Path $bootstrap 'Release\b2-tool.exe'
    $importDirectory = Join-Path $logRoot ([Guid]::NewGuid().ToString())
    [IO.Directory]::CreateDirectory($importDirectory) | Out-Null
    $importXbe = Join-Path $importDirectory 'default.xbe'
    $importEffects = Join-Path $importDirectory 'b2distfx.bin'
    Set-Stage 3
    Run-Step 'Reading the game executable...' $tool @('extract', $DiscImage, 'default.xbe', $importXbe)
    Run-Step 'Verifying the supported game version...' $tool @('verify', $importXbe)
    Run-Step 'Reading the sound effects...' $tool @('extract', $DiscImage, 'data/b2distfx.bin', $importEffects)
    $localDirectory = Join-Path $projectRoot 'data\local'
    [IO.Directory]::CreateDirectory($localDirectory) | Out-Null
    $localXbe = Join-Path $localDirectory 'default.xbe'
    $localEffects = Join-Path $localDirectory 'b2distfx.bin'
    Copy-Changed $importXbe $localXbe
    Copy-Changed $importEffects $localEffects
    Set-Stage 4
    Configure-Step 'Preparing the game build...' (Join-Path $buildRoot 'windows') (@('--preset', 'windows', "-DB2_XBE=$localXbe", "-DB2_EFFECTS=$localEffects") + $toolchain)
    Set-Stage 5
    Run-Step 'Building the game. The first build can take a while...' $cmake @('--build', '--preset', 'release') -Build
    Set-Stage 6
    Show-Status 'Saving game-file locations and finishing installation...'
    if ($cancelled) { throw 'Build cancelled.' }
    Show-TaskProgress 0 0
    $runtimeLocal = Join-Path $gameDirectory 'data\local'
    [IO.Directory]::CreateDirectory($runtimeLocal) | Out-Null
    Copy-Changed $localXbe (Join-Path $runtimeLocal 'default.xbe')
    $settingsPath = Join-Path $runtimeLocal 'settings.cfg'
    $settings = @()
    if (Test-Path -LiteralPath $settingsPath) { $settings = [IO.File]::ReadAllLines($settingsPath) }
    $settings = @('xbe=data/local/default.xbe', "disc=$DiscImage") + @($settings | Where-Object { $_ -notmatch '^(xbe|disc)=' })
    [IO.File]::WriteAllLines($settingsPath, $settings, [Text.UTF8Encoding]::new($false))
    Show-Status 'Open the game folder and double-click b2.exe.'
} catch {
    $exitCode = 1
    Show-Status $_.Exception.Message
} finally {
    Stop-Workers
    if ($importDirectory) {
        foreach ($name in @('default.xbe', 'b2distfx.bin')) {
            $path = Join-Path $importDirectory $name
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
        }
        Remove-Item -LiteralPath $importDirectory
    }
    if ($buildLock) { $buildLock.Dispose(); $buildLock = $null }
    $busy = $false
    $env:NINJA_STATUS = $previousNinjaStatus
}
if (!$NoGui) {
    Complete-SetupWindow ($exitCode -eq 0)
    if ($cancelled) { $form.Close() }
    else { [Windows.Forms.Application]::Run($form) }
    $form.Dispose()
}
exit $exitCode
