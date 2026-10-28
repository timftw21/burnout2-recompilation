# Official, fixed downloads. Keep tools local to this installation and reuse
# compatible system tools. No package manager or global PATH edit is required.
$windowsPackages = @{
    cmake = @{
        Name='CMake'; Directory='cmake-4.4.5'; File='cmake-4.4.5-windows-x86_64.zip'; Size=54412232L
        Url='https://github.com/Kitware/CMake/releases/download/v4.4.5/cmake-4.4.5-windows-x86_64.zip'
        Hash='326c826486d46a23cf9d5a1c9ee3a567d427bdd02c4a34071937a2a675cff004'
        Folder='cmake-4.4.5-windows-x86_64'; Program='bin\cmake.exe'; Version='4.4.5'; Companions=@()
    }
    git = @{
        Name='Git'; Directory='mingit-2.56.0.4'; File='MinGit-2.56.0.4-64-bit.zip'; Size=39846926L
        Url='https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/MinGit-2.56.0.4-64-bit.zip'
        Hash='e3bf78351fe777fba47ddee5950082f8a98bb0f02604fdc7ba3ee2cb68ffa877'
        Folder=''; Program='cmd\git.exe'; Version='2.56.0'; Companions=@()
    }
    ninja = @{
        Name='Ninja'; Directory='ninja-1.13.2'; File='ninja-1.13.2.zip'; Size=291570L
        Url='https://github.com/ninja-build/ninja/releases/download/v1.13.2/ninja-win.zip'
        Hash='07fc8261b42b20e71d1720b39068c2e14ffcee6396b76fb7a795fb460b78dc65'
        Folder=''; Program='ninja.exe'; Version='1.13.2'; Companions=@()
    }
    llvm = @{
        Name='LLVM'; Directory='llvm-22.1.8'; File='llvm-22.1.8-windows-x64.tar.xz'; Size=862053924L
        Url='https://github.com/llvm/llvm-project/releases/download/llvmorg-22.1.8/clang%2Bllvm-22.1.8-x86_64-pc-windows-msvc.tar.xz'
        Hash='d96c2cc1736f4eb7fa43cb9bbdf56d93551a9ae0a9aadb9c99c3c3b2b712a234'
        Folder='clang+llvm-22.1.8-x86_64-pc-windows-msvc'; Program='bin\clang-cl.exe'; Version='22.1.8'; Companions=@('lld-link.exe', 'llvm-lib.exe')
    }
    vswhere = @{
        Name='Microsoft build-tool detection'; File='vswhere-3.1.7.exe'; Size=469456L
        Url='https://github.com/microsoft/vswhere/releases/download/3.1.7/vswhere.exe'
        Hash='c54f3b7c9164ea9a0db8641e81ecdda80c2664ef5a47c4191406f848cc07c662'
    }
    visualstudio = @{
        Name='Microsoft C++ Build Tools'; File='vs-buildtools-18.exe'; Size=5689008L
        Url='https://download.visualstudio.microsoft.com/download/pr/9a3f23c2-cc94-49a0-88c0-f9f19f2ac9df/f1d6aa62ea7c6339fde1cfb4fdea891587bd27238d6f7e387301f0fb66f9068a/vs_BuildTools.exe'
        Hash='f1d6aa62ea7c6339fde1cfb4fdea891587bd27238d6f7e387301f0fb66f9068a'
    }
}

function Find-Program([string]$name, [string[]]$candidates, [version]$minimum, [string[]]$companions = @(), [switch]$CandidatesOnly) {
    $paths = $candidates
    if (!$CandidatesOnly) { $paths = @((Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue).Source) + $paths }
    foreach ($candidate in $paths | Select-Object -Unique) {
        if (!$candidate -or !(Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $directory = Split-Path -Parent $candidate
        if (@($companions | Where-Object { !(Test-Path -LiteralPath (Join-Path $directory $_) -PathType Leaf) }).Count) { continue }
        if ($minimum) {
            try { $versionLine = @(& $candidate --version 2>$null)[0] } catch { continue }
            if ($LASTEXITCODE -ne 0 -or $versionLine -notmatch '(\d+\.\d+(?:\.\d+)?)' -or [version]$Matches[1] -lt $minimum) { continue }
        }
        return $candidate
    }
    return $null
}

function Show-DownloadProgress([long]$received, [long]$total) {
    if ($NoGui) { return }
    if ($total) { Show-TaskProgress $received ([Math]::Max($received, $total)) }
    else { Show-TaskProgress 0 0 }
    $taskCount.Text = if ($total) { '{0:N1} of {1:N1} MB downloaded' -f ($received / 1MB), ($total / 1MB) }
        else { '{0:N1} MB downloaded' -f ($received / 1MB) }
    $progress.AccessibleDescription = $taskCount.Text
}

function Get-SetupDownload($package) {
    $cache = Join-Path $toolsRoot 'downloads'
    [IO.Directory]::CreateDirectory($cache) | Out-Null
    $path = Join-Path $cache $package.File
    if ((Test-Path -LiteralPath $path) -and (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -eq $package.Hash) { return $path }
    $partial = "$path.download"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if ($cancelled) { throw 'Installation cancelled.' }
        Show-Status "Downloading $($package.Name)..."
        Show-TaskProgress 0 0
        $client = [Net.WebClient]::new()
        $client.Headers['User-Agent'] = 'Burnout2-Setup'
        $download = $null
        try {
            $download = $client.DownloadFileTaskAsync([Uri]$package.Url, $partial)
            $lastSize = 0L; $lastChange = [DateTime]::UtcNow
            while (!$download.IsCompleted) {
                if (!$NoGui) { [Windows.Forms.Application]::DoEvents() }
                if ($cancelled) { throw 'Installation cancelled.' }
                $size = if (Test-Path -LiteralPath $partial) { (Get-Item -LiteralPath $partial).Length } else { 0L }
                if ($size -ne $lastSize) { $lastSize = $size; $lastChange = [DateTime]::UtcNow }
                if (([DateTime]::UtcNow - $lastChange).TotalSeconds -gt 120) { throw 'The download stopped responding.' }
                Show-DownloadProgress $size $package.Size
                Start-Sleep -Milliseconds 100
            }
            $null = $download.GetAwaiter().GetResult()
            if ($cancelled) { throw 'Installation cancelled.' }
            Show-Status "Verifying $($package.Name)..."
            if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $package.Hash) { throw 'The downloaded file failed its SHA-256 check.' }
            Move-Item -LiteralPath $partial -Destination $path -Force
            return $path
        } catch {
            if ($cancelled) { throw 'Installation cancelled.' }
            if ($attempt -eq 3) { throw "Could not download $($package.Name). Check your connection and run Install.cmd again. $($_.Exception.Message) Source: $($package.Url)" }
            Show-Status "Retrying $($package.Name) download ($attempt of 3)..."
        } finally {
            $client.CancelAsync()
            if ($download) { try { $null = $download.GetAwaiter().GetResult() } catch {} }
            $client.Dispose()
            if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
        }
    }
}

function Remove-SetupDirectory([string]$path) {
    # Only installer-owned unpack/cache directories may be removed recursively.
    $root = [IO.Path]::GetFullPath($toolsRoot).TrimEnd('\') + '\'
    $target = [IO.Path]::GetFullPath($path)
    if (!$target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid tool-cache directory.' }
    if (Test-Path -LiteralPath $target) {
        if ((Get-Item -LiteralPath $target).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'A tool-cache directory cannot be a link.' }
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

function Get-PortableTool([string]$key) {
    $package = $windowsPackages[$key]
    $directory = Join-Path $toolsRoot $package.Directory
    $program = Join-Path $directory $package.Program
    $stamp = Join-Path $directory 'setup.sha256'
    if ((Test-Path -LiteralPath $stamp) -and [IO.File]::ReadAllText($stamp) -eq $package.Hash -and
        (Find-Program '' @($program) ([version]$package.Version) $package.Companions -CandidatesOnly)) { return $program }
    $archive = Get-SetupDownload $package
    $unpack = Join-Path $toolsRoot ('.unpack-' + [Guid]::NewGuid())
    [IO.Directory]::CreateDirectory($unpack) | Out-Null
    try {
        Show-Status "Preparing $($package.Name)..."
        Show-TaskProgress 0 0
        if ($key -eq 'cmake') {
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            [IO.Compression.ZipFile]::ExtractToDirectory($archive, $unpack)
        } else {
            Run-Step "Unpacking $($package.Name)..." $script:cmake @('-E', 'tar', 'xf', $archive) -Directory $unpack
        }
        if ($cancelled) { throw 'Installation cancelled.' }
        $source = if ($package.Folder) { Join-Path $unpack $package.Folder } else { $unpack }
        if (!(Find-Program '' @((Join-Path $source $package.Program)) ([version]$package.Version) $package.Companions -CandidatesOnly)) {
            throw "$($package.Name) archive does not contain a working toolchain. Open the build log for details."
        }
        Remove-SetupDirectory $directory
        # Both paths are confined to this installer-owned cache before moving.
        $prefix = [IO.Path]::GetFullPath($toolsRoot).TrimEnd('\') + '\'
        if (![IO.Path]::GetFullPath($source).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
            ![IO.Path]::GetFullPath($directory).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid tool-cache move.' }
        Move-Item -LiteralPath $source -Destination $directory
        [IO.File]::WriteAllText($stamp, $package.Hash)
    } finally { Remove-SetupDirectory $unpack }
    return $program
}

function Find-WindowsSdk {
    $registry = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots' -ErrorAction SilentlyContinue
    $roots = @($registry.KitsRoot10, "${env:ProgramFiles(x86)}\Windows Kits\10") | Select-Object -Unique
    foreach ($root in $roots) {
        if (!$root) { continue }
        foreach ($folder in Get-ChildItem -LiteralPath (Join-Path $root 'Include') -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending) {
            $version = $null
            if (![version]::TryParse($folder.Name, [ref]$version) -or $version -lt [version]'10.0.26100.0') { continue }
            $required = @("Include\$version\um\Windows.h", "Include\$version\ucrt\stdio.h", "Lib\$version\um\x64\kernel32.lib", "Lib\$version\ucrt\x64\ucrt.lib", "bin\$version\x64\rc.exe", "bin\$version\x64\mt.exe")
            if (!@($required | Where-Object { !(Test-Path -LiteralPath (Join-Path $root $_) -PathType Leaf) }).Count) { return $root }
        }
    }
    return $null
}

function Invoke-MicrosoftSetup([string]$program, [string[]]$arguments) {
    $signature = Get-AuthenticodeSignature -LiteralPath $program
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch '(^|, )O=Microsoft Corporation(,|$)') { throw 'Microsoft setup signature verification failed.' }
    Show-Status 'Installing Microsoft C++ build tools. Approve the Windows prompt, then finish or cancel in the Microsoft installer.'
    Show-TaskProgress 0 0
    $quoted = @($arguments | ForEach-Object { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }) -join ' '
    $process = $null
    try {
        if (!$NoGui) { $cancelButton.Enabled = $false }
        $process = Start-Process -FilePath $program -ArgumentList $quoted -WorkingDirectory $projectRoot -Verb RunAs -WindowStyle Hidden -PassThru
        $null = $process.Handle
        # The system installer owns its workers and rollback. Never kill its
        # MSI/service processes when our window is closed or cancellation occurs.
        while (!$process.WaitForExit(100)) { if (!$NoGui) { [Windows.Forms.Application]::DoEvents() } }
        $process.WaitForExit()
        switch ($process.ExitCode) {
            0 { }
            3010 { throw 'Microsoft tools are installed. Restart Windows, then run Install.cmd again to continue.' }
            1602 { $script:cancelled = $true; throw 'Microsoft setup was cancelled. Run Install.cmd again to continue.' }
            5004 { $script:cancelled = $true; throw 'Microsoft setup was cancelled. Run Install.cmd again to continue.' }
            1001 { throw 'Another Visual Studio installation is running. Let it finish, then run Install.cmd again.' }
            1618 { throw 'Another installation is running. Let it finish, then run Install.cmd again.' }
            default { throw "Microsoft setup failed (code $($process.ExitCode)). Run Install.cmd again after resolving the error shown by Microsoft setup." }
        }
        if ($cancelled) { throw 'Installation cancelled.' }
    } catch [ComponentModel.Win32Exception] {
        if ($_.Exception.NativeErrorCode -eq 1223) { throw 'Windows permission was declined. Run Install.cmd again and approve Microsoft build-tool setup.' }
        throw
    } finally {
        if ($process) { $process.Dispose() }
        if (!$NoGui) { $cancelButton.Enabled = $true }
    }
}

function Initialize-WindowsTools {
    if (![Environment]::Is64BitProcess) { throw 'Run Install.cmd using 64-bit Windows PowerShell.' }
    $script:toolsRoot = Join-Path $buildRoot 'deps\windows'
    $vswhere = Find-Program 'vswhere.exe' @("${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe")
    if (!$vswhere) { $vswhere = Get-SetupDownload $windowsPackages.vswhere }
    $visualStudio = & $vswhere -latest -products '*' -version '[18.0,19.0)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (!$visualStudio -or !(Find-WindowsSdk)) {
        $existing = & $vswhere -latest -products '*' -version '[18.0,19.0)' -property installationPath
        $arguments = @('--passive', '--norestart', '--add', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '--add', 'Microsoft.VisualStudio.Component.Windows11SDK.26100')
        if ($existing) {
            $program = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\setup.exe"
            if (!(Test-Path -LiteralPath $program)) { throw 'The existing Microsoft installer is missing. Repair Visual Studio Installer, then run Install.cmd again.' }
            $arguments = @('modify', '--installPath', $existing) + $arguments
        } else {
            $program = Get-SetupDownload $windowsPackages.visualstudio
            $arguments += @('--wait', '--add', 'Microsoft.VisualStudio.Workload.VCTools')
        }
        Invoke-MicrosoftSetup $program $arguments
        $visualStudio = & $vswhere -latest -products '*' -version '[18.0,19.0)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if (!$visualStudio -or !(Find-WindowsSdk)) { throw 'Microsoft setup did not finish installing C++ and the Windows SDK. Run Install.cmd again to retry.' }
    }
    $developerShell = Join-Path $visualStudio 'Common7\Tools\Launch-VsDevShell.ps1'
    if (!(Test-Path -LiteralPath $developerShell)) { throw 'The Microsoft C++ environment is incomplete. Repair its installation, then run Install.cmd again.' }
    & $developerShell -Arch amd64 -HostArch amd64 -SkipAutomaticLocation -NoLogo
    $script:cmake = Find-Program 'cmake.exe' @("$env:ProgramFiles\CMake\bin\cmake.exe", "$visualStudio\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe") ([version]'4.2.0')
    if (!$script:cmake) { $script:cmake = Get-PortableTool 'cmake' }
    $script:git = Find-Program 'git.exe' @("$env:ProgramFiles\Git\cmd\git.exe", "$visualStudio\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\cmd\git.exe") ([version]'2.20')
    if (!$script:git) { $script:git = Get-PortableTool 'git' }
    $script:clang = Find-Program 'clang-cl.exe' @("$env:ProgramFiles\LLVM\bin\clang-cl.exe", "$visualStudio\VC\Tools\Llvm\x64\bin\clang-cl.exe") ([version]'22.1.0') @('lld-link.exe', 'llvm-lib.exe')
    if (!$script:clang) { $script:clang = Get-PortableTool 'llvm' }
    $script:ninja = Find-Program 'ninja.exe' @("$visualStudio\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe") ([version]'1.10')
    if (!$script:ninja) { $script:ninja = Get-PortableTool 'ninja' }
    $env:PATH = "$(Split-Path -Parent $script:cmake);$(Split-Path -Parent $script:git);$(Split-Path -Parent $script:clang);$(Split-Path -Parent $script:ninja);$env:PATH"
    Show-Status 'Build tools are ready.'
}
