param([string]$DiscImage, [switch]$NoGui)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location -LiteralPath $projectRoot
$buildRoot = Join-Path $projectRoot 'build'
$logRoot = Join-Path $buildRoot 'setup'
$gameDirectory = Join-Path $buildRoot 'windows\Release'
[IO.Directory]::CreateDirectory($logRoot) | Out-Null
$logPath = Join-Path $logRoot 'build.log'
[IO.File]::WriteAllText($logPath, '', [Text.UTF8Encoding]::new($false))
$workers = [Collections.Generic.Dictionary[int,object]]::new()
$cancelled = $false
$busy = $true
$step = 0
$form = $null

function Show-Status([string]$message) {
    [IO.File]::AppendAllText($logPath, "$message`r`n")
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
    foreach ($record in $workers.Values) {
        $live = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)"
        if ($live -and $live.CreationDate.ToUniversalTime().Ticks -eq $record.Ticks) {
            Stop-Process -Id $record.Id -Force -ErrorAction SilentlyContinue
        }
    }
    @($workers.Values) | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $logRoot 'workers.json')
}

function Run-Step([string]$message, [string]$program, [string[]]$arguments) {
    Show-Status $message
    if ($cancelled) { throw 'Build cancelled.' }
    $script:step++
    $outputPath = Join-Path $logRoot "$step.out.log"
    $errorPath = Join-Path $logRoot "$step.err.log"
    # Quote for the native Windows argument parser; never pass paths through a shell.
    $quoted = @($arguments | ForEach-Object { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }) -join ' '
    $process = Start-Process -FilePath $program -ArgumentList $quoted -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $outputPath -RedirectStandardError $errorPath
    # Windows PowerShell must retain the process handle to read its exit code.
    $null = $process.Handle
    try {
        Record-Workers $process
        $lastPoll = [DateTime]::UtcNow
        while (!$process.WaitForExit(100)) {
            if (!$NoGui) { [Windows.Forms.Application]::DoEvents() }
            if ($cancelled) { throw 'Build cancelled.' }
            if (([DateTime]::UtcNow - $lastPoll).TotalSeconds -ge 1) {
                Record-Workers $process
                $lastPoll = [DateTime]::UtcNow
            }
        }
        $process.WaitForExit()
        foreach ($path in @($outputPath, $errorPath)) {
            $source = [IO.File]::OpenRead($path)
            $destination = [IO.File]::Open($logPath, [IO.FileMode]::Append, [IO.FileAccess]::Write)
            try { $source.CopyTo($destination) } finally { $source.Dispose(); $destination.Dispose() }
        }
        if ($process.ExitCode -ne 0) { throw "$message failed. Open the build log for details." }
    } finally { Stop-Workers; $process.Dispose() }
}

function Find-Program([string]$name, [string[]]$candidates) {
    $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    foreach ($candidate in $candidates) { if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate } }
    throw "$name is missing. Install the build requirements listed in README.md."
}

function Copy-Changed([string]$source, [string]$destination) {
    if (!(Test-Path -LiteralPath $destination) -or (Get-FileHash -LiteralPath $source).Hash -ne (Get-FileHash -LiteralPath $destination).Hash) {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
}

if (!$NoGui) {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()
    $form = [Windows.Forms.Form]::new()
    $form.Text = 'Build Burnout 2'
    $form.ClientSize = [Drawing.Size]::new(520, 190)
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.Font = [Drawing.Font]::new('Segoe UI', 10)
    $status = [Windows.Forms.Label]::new()
    $status.Location = [Drawing.Point]::new(20, 20)
    $status.Size = [Drawing.Size]::new(480, 70)
    $progress = [Windows.Forms.ProgressBar]::new()
    $progress.Location = [Drawing.Point]::new(20, 95)
    $progress.Size = [Drawing.Size]::new(480, 18)
    $progress.Style = 'Marquee'
    $logButton = [Windows.Forms.Button]::new()
    $logButton.Text = 'Open build log'
    $logButton.Location = [Drawing.Point]::new(20, 135)
    $logButton.Size = [Drawing.Size]::new(150, 32)
    $logButton.Add_Click({ Start-Process -FilePath 'notepad.exe' -ArgumentList ('"' + $logPath + '"') })
    $openButton = [Windows.Forms.Button]::new()
    $openButton.Text = 'Open game folder'
    $openButton.Location = [Drawing.Point]::new(330, 135)
    $openButton.Size = [Drawing.Size]::new(170, 32)
    $openButton.Enabled = $false
    $openButton.Add_Click({ Start-Process -FilePath 'explorer.exe' -ArgumentList ('"' + $gameDirectory + '"') })
    $form.Controls.AddRange(@($status, $progress, $logButton, $openButton))
    $form.Add_FormClosing({ param($sender, $eventArgs) if ($busy) { $script:cancelled = $true; $eventArgs.Cancel = $true } })
    $form.Show()
}

$importDirectory = $null
$exitCode = 0
try {
    Show-Status 'Checking build requirements...'
    if (![Environment]::Is64BitOperatingSystem) { throw 'A 64-bit Windows PC is required.' }
    $vswhere = Find-Program 'vswhere.exe' @("${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe")
    $visualStudio = & $vswhere -latest -products '*' -version '[18.0,19.0)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (!$visualStudio) { throw 'Install Visual Studio 2026 with Desktop development with C++, including the Windows SDK.' }
    $cmake = Find-Program 'cmake.exe' @("$env:ProgramFiles\CMake\bin\cmake.exe", "$visualStudio\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe")
    $versionLine = (& $cmake --version)[0]
    if ($versionLine -notmatch 'cmake version (\d+\.\d+\.\d+)' -or [version]$Matches[1] -lt [version]'4.2.0') {
        throw 'CMake 4.2 or newer is required. Install it from the link in README.md.'
    }
    $git = Find-Program 'git.exe' @("$env:ProgramFiles\Git\cmd\git.exe", "$visualStudio\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\cmd\git.exe")
    $env:PATH = "$(Split-Path -Parent $cmake);$(Split-Path -Parent $git);$env:PATH"
    $env:MSBUILDDISABLENODEREUSE = '1'
    if (!$DiscImage) {
        if ($NoGui) { throw 'Specify -DiscImage for a build without the file picker.' }
        $picker = [Windows.Forms.OpenFileDialog]::new()
        $picker.Title = 'Select your US Burnout 2 Xbox disc image'
        $picker.Filter = 'Xbox disc images (*.iso;*.xiso)|*.iso;*.xiso'
        try {
            if ($picker.ShowDialog($form) -ne 'OK') { $script:cancelled = $true; throw 'Build cancelled.' }
            $DiscImage = $picker.FileName
        } finally { $picker.Dispose() }
    }
    $DiscImage = (Resolve-Path -LiteralPath $DiscImage).Path
    $bootstrap = Join-Path $buildRoot 'bootstrap'
    Run-Step 'Preparing the build tools...' $cmake @('--preset', 'windows', '-B', $bootstrap, '-DB2_XBE=', "-DCMAKE_GENERATOR_INSTANCE=$visualStudio")
    Run-Step 'Building the game-file reader...' $cmake @('--build', $bootstrap, '--config', 'Release', '--target', 'b2-tool', '--parallel', '2', '--', '/nodeReuse:false')
    $tool = Join-Path $bootstrap 'Release\b2-tool.exe'
    $importDirectory = Join-Path $logRoot ([Guid]::NewGuid().ToString())
    [IO.Directory]::CreateDirectory($importDirectory) | Out-Null
    $importXbe = Join-Path $importDirectory 'default.xbe'
    $importEffects = Join-Path $importDirectory 'b2distfx.bin'
    Run-Step 'Reading the game executable...' $tool @('extract', $DiscImage, 'default.xbe', $importXbe)
    Run-Step 'Verifying the supported game version...' $tool @('verify', $importXbe)
    Run-Step 'Reading the sound effects...' $tool @('extract', $DiscImage, 'data/b2distfx.bin', $importEffects)
    $localDirectory = Join-Path $projectRoot 'data\local'
    [IO.Directory]::CreateDirectory($localDirectory) | Out-Null
    $localXbe = Join-Path $localDirectory 'default.xbe'
    $localEffects = Join-Path $localDirectory 'b2distfx.bin'
    Copy-Changed $importXbe $localXbe
    Copy-Changed $importEffects $localEffects
    Run-Step 'Preparing the game build...' $cmake @('--preset', 'windows', "-DB2_XBE=$localXbe", "-DB2_EFFECTS=$localEffects", "-DCMAKE_GENERATOR_INSTANCE=$visualStudio")
    Run-Step 'Building the game. The first build can take a while...' $cmake @('--build', '--preset', 'release')
    $runtimeLocal = Join-Path $gameDirectory 'data\local'
    [IO.Directory]::CreateDirectory($runtimeLocal) | Out-Null
    Copy-Changed $localXbe (Join-Path $runtimeLocal 'default.xbe')
    $settingsPath = Join-Path $runtimeLocal 'settings.cfg'
    if (!(Test-Path -LiteralPath $settingsPath)) {
        [IO.File]::WriteAllLines($settingsPath, @("xbe=$(Join-Path $runtimeLocal 'default.xbe')", "disc=$DiscImage"), [Text.UTF8Encoding]::new($false))
    }
    Show-Status 'Ready. Open the game folder and double-click b2.exe.'
    if (!$NoGui) { $openButton.Enabled = $true }
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
    $busy = $false
}
if (!$NoGui) {
    $progress.Visible = $false
    if ($cancelled) { $form.Close() }
    else { [Windows.Forms.Application]::Run($form) }
    $form.Dispose()
}
exit $exitCode
