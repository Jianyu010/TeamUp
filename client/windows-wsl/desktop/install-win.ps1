[CmdletBinding()]
param(
    [string]$SourceDirectory = $PSScriptRoot,
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'Programs\TeamUp'),
    [string]$ShortcutPath = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'TeamUp.lnk'),
    [switch]$Start
)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $SourceDirectory).Path
$destination = [IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\', '/')
if ([IO.Path]::GetFileName($destination) -ne 'TeamUp') {
    throw 'The installation directory must be named TeamUp.'
}
$separator = [IO.Path]::DirectorySeparatorChar
if ($source -eq $destination -or
    $source.StartsWith($destination + $separator, [StringComparison]::OrdinalIgnoreCase) -or
    $destination.StartsWith($source.TrimEnd('\', '/') + $separator, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Extract the portable package outside the installed TeamUp directory.'
}
foreach ($file in @('TeamUp.exe', 'resources\app.asar', 'resources\launch.py', 'resources\launch-wsl.py')) {
    if (!(Test-Path -LiteralPath (Join-Path $source $file) -PathType Leaf)) {
        throw "The portable package is incomplete: $file"
    }
}
if (Test-Path -LiteralPath $destination) {
    $item = Get-Item -LiteralPath $destination -Force
    if (!$item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The installed TeamUp directory must be a regular directory.'
    }
}
$parent = [IO.Path]::GetDirectoryName($destination)
$null = New-Item -ItemType Directory -Path $parent -Force
$suffix = [Guid]::NewGuid().ToString('N')
$staging = [IO.Path]::GetFullPath((Join-Path $parent "TeamUp.installing-$suffix"))
$backup = [IO.Path]::GetFullPath((Join-Path $parent "TeamUp.previous-$suffix"))
foreach ($path in @($staging, $backup)) {
    if ([IO.Path]::GetDirectoryName($path) -ne $parent -or $path -eq $destination) {
        throw 'Invalid installation staging path.'
    }
}

try {
    $null = New-Item -ItemType Directory -Path $staging
    Get-ChildItem -LiteralPath $source -Force | Copy-Item -Destination $staging -Recurse -Force
    $installedExe = Join-Path $destination 'TeamUp.exe'
    $owned = @(Get-Process -Name TeamUp -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and [string]::Equals($_.Path, $installedExe, [StringComparison]::OrdinalIgnoreCase)
    })
    foreach ($process in $owned) {
        if ($process.MainWindowHandle -ne 0) { $null = $process.CloseMainWindow() }
    }
    foreach ($process in $owned) { $null = $process.WaitForExit(2000) }
    Get-Process -Name TeamUp -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and [string]::Equals($_.Path, $installedExe, [StringComparison]::OrdinalIgnoreCase)
    } | Stop-Process

    $hadPrevious = Test-Path -LiteralPath $destination
    if ($hadPrevious) { Move-Item -LiteralPath $destination -Destination $backup }
    try {
        Move-Item -LiteralPath $staging -Destination $destination
    } catch {
        if ($hadPrevious -and !(Test-Path -LiteralPath $destination)) {
            Move-Item -LiteralPath $backup -Destination $destination
        }
        throw
    }

    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut([IO.Path]::GetFullPath($ShortcutPath))
    $shortcut.TargetPath = $installedExe
    $shortcut.WorkingDirectory = $destination
    $shortcut.IconLocation = "$installedExe,0"
    $shortcut.Save()
    if ($Start) { Start-Process -FilePath $installedExe -WorkingDirectory $destination -WindowStyle Normal }
    [PSCustomObject]@{
        installed = $destination
        version = (Get-Item -LiteralPath $installedExe).VersionInfo.ProductVersion
        previous = $(if ($hadPrevious) { $backup } else { $null })
        shortcut = [IO.Path]::GetFullPath($ShortcutPath)
    } | ConvertTo-Json -Compress
} finally {
    if (Test-Path -LiteralPath $staging) {
        $remaining = Get-Item -LiteralPath $staging -Force
        if ([IO.Path]::GetDirectoryName($staging) -ne $parent -or
            [IO.Path]::GetFileName($staging) -ne "TeamUp.installing-$suffix" -or
            ($remaining.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Refusing to remove an unexpected installation staging directory.'
        }
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
}
