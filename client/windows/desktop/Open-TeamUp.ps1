param(
    [string]$ProfileDirectory = (Join-Path $env:LOCALAPPDATA 'TeamUpWindows'),
    [string]$NodeExecutable = 'node.exe',
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$profile = Get-Content -Raw -LiteralPath (Join-Path $ProfileDirectory 'profile.json') | ConvertFrom-Json
$port = if ($profile.port) { [int]$profile.port } else { 18766 }
if ($port -lt 1024 -or $port -gt 65535) { throw 'Invalid TeamUp loopback port.' }
$urlFile = Join-Path $ProfileDirectory 'ui-url.txt'
$url = if (Test-Path -LiteralPath $urlFile) { (Get-Content -LiteralPath $urlFile -Raw).Trim() }
$pattern = '^http://127\.0\.0\.1:' + $port + '/#([a-f0-9]{64})$'
$running = $false
if ($url -match $pattern) {
    try {
        $running = (Invoke-RestMethod "http://127.0.0.1:$port/health" -Headers @{'x-teamup-local'=$Matches[1]} -TimeoutSec 3).ok
    } catch { $running = $false }
}

if (-not $running) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = (Get-Command $NodeExecutable -CommandType Application -ErrorAction Stop).Source
    $start.WorkingDirectory = $PSScriptRoot
    $start.Arguments = 'bridge.mjs "' + (Resolve-Path -LiteralPath $ProfileDirectory).Path + '"'
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $codex = Get-AppxPackage -Name OpenAI.Codex | Select-Object -First 1
    if ($codex) {
        $candidate = Join-Path $codex.InstallLocation 'app/resources/codex.exe'
        if (Test-Path -LiteralPath $candidate) {
            $start.EnvironmentVariables['TEAMUP_CODEX_DESKTOP_EXE'] = $candidate
        }
    }
    $process = [Diagnostics.Process]::Start($start)
    try {
        $read = $process.StandardOutput.ReadLineAsync()
        if (-not $read.Wait(10000) -or $read.Result -notmatch $pattern) {
            if (-not $process.HasExited) { $process.Kill() }
            throw 'TeamUp could not start. Check the profile and whether the loopback port is in use.'
        }
        $url = $read.Result
        [IO.File]::WriteAllText($urlFile, $url)
    } finally { $process.Dispose() }
}

if (-not $NoOpen) {
    $edge = Join-Path ${env:ProgramFiles(x86)} 'Microsoft/Edge/Application/msedge.exe'
    $desktopUrl = $url -replace '/#', '/desktop#'
    $edgeProfile = Join-Path $ProfileDirectory 'EdgeProfile'
    Start-Process -FilePath $edge -ArgumentList @("--app=$desktopUrl", "--user-data-dir=`"$edgeProfile`"")
}
