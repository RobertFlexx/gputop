[CmdletBinding()]
param(
    [ValidateSet('', 'Install', 'Update', 'Uninstall')]
    [string] $Action = 'Install',
    [switch] $Yes,
    [switch] $DryRun,
    [string] $Dir,
    [string] $BinDir,
    [string] $Python,
    [ValidateSet('', 'pip', 'uv')]
    [string] $Method = '',
    [string] $Ref,
    [string] $Repo,
    [string] $Source,
    [switch] $NoPath,
    [switch] $Help
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version Latest

$AppName = 'gputop'
$ScriptVersion = '1.0.0'
$MinPython = '3.10'
$ManagedPython = '3.12'
$IsWindowsHost = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
$script:Repository = ''
$script:GitRef = ''
$script:LocalSource = ''
$script:RequestedPython = ''
$script:InstallMethod = ''
$script:SourceMode = 'auto'
$script:DataDir = ''
$script:BinDir = ''
$script:VenvDir = ''
$script:SrcDir = ''
$script:VenvBin = ''
$script:VenvPython = ''
$script:LauncherPath = ''
$script:ToolsDir = ''
$script:TempDir = ''
$script:Interpreter = $null
$script:Candidates = @()
$script:UvPath = ''
$script:HasEnvironment = $false
$script:InstalledVersion = ''
$script:OldTls = $null

function Write-Step { param([string] $Message) Write-Host "-> $Message" -ForegroundColor Cyan }
function Write-Detail { param([string] $Message) Write-Host "   $Message" -ForegroundColor DarkGray }
function Write-Field { param([string] $Label, [string] $Value) Write-Host ("   {0,-12} {1}" -f $Label, $Value) -ForegroundColor Gray }
function Write-Success { param([string] $Message) Write-Host "$AppName $Message" -ForegroundColor Green }
function Write-Notice { param([string] $Message) Write-Host "warning: $Message" -ForegroundColor Yellow }
function Write-Fail { param([string] $Message) Write-Host "error: $Message" -ForegroundColor Red }
function Write-Plain { param([string] $Message = '') Write-Host $Message }

function Get-PreferredSetting {
    param($Value, [string] $Name, $Default)
    if ($Value) { return $Value }
    if ($Name) {
        $fromEnvironment = [System.Environment]::GetEnvironmentVariable($Name)
        if ($fromEnvironment) { return $fromEnvironment }
    }
    return $Default
}

function Test-Interactive {
    if ($Yes) { return $false }
    if ($DryRun) { return $false }
    if ($env:GPUTOP_YES) { return $false }
    try {
        return (-not [Console]::IsInputRedirected)
    } catch {
        return $false
    }
}

function Test-Command {
    param([string] $Name)
    if ($Name -match '[/\\]') { return (Test-Path -LiteralPath $Name) }
    return [bool] (Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Get-CommandPath {
    param([string] $Name)
    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    return $null
}

function Get-HomeDirectory {
    if ($IsWindowsHost) { return [System.Environment]::GetFolderPath('UserProfile') }
    return $env:HOME
}

function Get-DefaultDataDir {
    if (-not $IsWindowsHost -and $env:XDG_DATA_HOME) { return (Join-Path $env:XDG_DATA_HOME $AppName) }
    if ($IsWindowsHost) {
        $local = [System.Environment]::GetFolderPath('LocalApplicationData')
        if ($local) { return (Join-Path $local (Join-Path 'Programs' $AppName)) }
    }
    return (Join-Path (Get-HomeDirectory) (Join-Path '.local' (Join-Path 'share' $AppName)))
}

function Get-DefaultBinDir {
    if ($IsWindowsHost) { return (Get-DefaultDataDir) }
    return (Join-Path (Get-HomeDirectory) (Join-Path '.local' 'bin'))
}

function Get-CommandName {
    if ($IsWindowsHost) { return "$AppName.cmd" }
    return $AppName
}

function Get-CommandTarget {
    $exe = if ($IsWindowsHost) { "$AppName.exe" } else { $AppName }
    return (Join-Path $script:VenvBin $exe)
}

function Set-Layout {
    param([string] $DataDirectory = $script:DataDir, [string] $BinDirectory = $script:BinDir)
    $script:DataDir = $DataDirectory
    $script:BinDir = $BinDirectory
    $script:VenvDir = Join-Path $script:DataDir 'venv'
    $script:SrcDir = Join-Path $script:DataDir 'src'
    $script:VenvBin = Join-Path $script:VenvDir 'Scripts'
    $script:VenvPython = Join-Path $script:VenvBin 'python.exe'
    $script:LauncherPath = Join-Path $script:BinDir (Get-CommandName)
    $script:ToolsDir = Join-Path $script:DataDir (Join-Path 'tools' 'bin')
    $script:HasEnvironment = $false
    if (Test-Path -LiteralPath $script:VenvDir) {
        try {
            $layout = Get-VenvLayout
            $script:VenvBin = $layout.Folder
            $script:VenvPython = $layout.Python
            $script:HasEnvironment = $true
        } catch {
            $script:HasEnvironment = $false
        }
    }
}

function Get-VenvLayout {
    foreach ($name in @('Scripts', 'bin')) {
        $folder = Join-Path $script:VenvDir $name
        foreach ($exe in @('python.exe', 'python3.exe', 'python')) {
            $candidate = Join-Path $folder $exe
            if (Test-Path -LiteralPath $candidate) {
                return [PSCustomObject]@{ Folder = $folder; Python = $candidate }
            }
        }
    }
    throw "the environment has no interpreter under $($script:VenvDir)"
}

function Read-State {
    $path = Join-Path $script:DataDir 'install.state'
    $state = @{}
    if (-not (Test-Path -LiteralPath $path)) { return $state }
    foreach ($line in [System.IO.File]::ReadAllLines($path)) {
        $index = $line.IndexOf('=')
        if ($index -lt 1) { continue }
        $state[$line.Substring(0, $index)] = $line.Substring($index + 1)
    }
    return $state
}

function Write-State {
    $version = if ($script:InstalledVersion) { $script:InstalledVersion } else { Get-InstalledVersion }
    if (-not $version) { $version = 'unknown' }
    $lines = @(
        'version=1',
        "data_dir=$($script:DataDir)",
        "venv_dir=$($script:VenvDir)",
        "src_dir=$($script:SrcDir)",
        "bin_dir=$($script:BinDir)",
        "launcher=$($script:LauncherPath)",
        "method=$($script:InstallMethod)",
        "ref=$($script:GitRef)",
        "repo=$($script:Repository)",
        "installed_version=$version"
    )
    New-Item -ItemType Directory -Force -Path $script:DataDir | Out-Null
    [System.IO.File]::WriteAllLines((Join-Path $script:DataDir 'install.state'), $lines)
    $script:InstalledVersion = $version
}

function Get-InstalledVersion {
    if (-not (Test-Path -LiteralPath $script:VenvPython)) { return $null }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $value = & $script:VenvPython -c 'import importlib.metadata as m; print(m.version("gputop"))' 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
    } finally {
        $ErrorActionPreference = $previous
    }
    if (-not $value) { return $null }
    return "$value".Trim()
}

function Get-SourceVersion {
    $initFile = Join-Path $script:SrcDir (Join-Path $AppName '__init__.py')
    if (-not (Test-Path -LiteralPath $initFile)) { return $null }
    $match = Select-String -LiteralPath $initFile -Pattern '^__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $match) { return $null }
    return $match.Matches[0].Groups[1].Value
}

function Test-VersionAtLeast {
    param([string] $Left, [string] $Right)
    $leftParts = @($Left.TrimStart('v').Split('.'))
    $rightParts = @($Right.TrimStart('v').Split('.'))
    for ($index = 0; $index -lt 3; $index++) {
        $a = 0
        $b = 0
        if ($index -lt $leftParts.Count -and $leftParts[$index] -match '^\d+$') { $a = [int]$leftParts[$index] }
        if ($index -lt $rightParts.Count -and $rightParts[$index] -match '^\d+$') { $b = [int]$rightParts[$index] }
        if ($a -gt $b) { return $true }
        if ($a -lt $b) { return $false }
    }
    return $true
}

function New-Interpreter {
    param([string] $Path, [string[]] $Arguments = @(), [string] $Label)
    return [PSCustomObject]@{
        Path    = $Path
        Args    = $Arguments
        Label   = if ($Label) { $Label } else { $Path }
        Version = ''
    }
}

function Invoke-Python {
    param($Interpreter, [string[]] $Code, [switch] $Quiet)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $value = & $Interpreter.Path @($Interpreter.Args) @Code 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
    } finally {
        $ErrorActionPreference = $previous
    }
    if (-not $value) { return $null }
    return ("$value").Trim()
}

function Get-PythonVersion {
    param($Interpreter)
    return (Invoke-Python $Interpreter @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])'))
}

function Get-PythonFullVersion {
    param($Interpreter)
    $value = Invoke-Python $Interpreter @('-c', 'import platform; print(platform.python_version())')
    if ($value) { return $value }
    return '?'
}

function Get-PythonCandidates {
    if ($script:RequestedPython) {
        if (-not (Test-Command $script:RequestedPython)) {
            throw "python not found: $($script:RequestedPython)"
        }
        $single = New-Interpreter -Path $script:RequestedPython
        $version = Get-PythonVersion $single
        if (-not $version) { throw "could not run $($script:RequestedPython)" }
        if (-not (Test-VersionAtLeast $version $MinPython)) {
            throw "$($script:RequestedPython) runs Python $version, $MinPython or newer is required"
        }
        $single.Version = $version
        return @($single)
    }
    $probe = New-Object System.Collections.Generic.List[object]
    $wanted = @('3.14', '3.13', '3.12', '3.11', '3.10')
    if (Test-Command 'py') {
        foreach ($version in $wanted) { $probe.Add((New-Interpreter -Path 'py' -Arguments @("-$version") -Label "py -$version")) }
        $probe.Add((New-Interpreter -Path 'py' -Arguments @('-3') -Label 'py -3'))
        $probe.Add((New-Interpreter -Path 'py' -Label 'py'))
    }
    foreach ($name in @('python3', 'python')) {
        if (Test-Command $name) { $probe.Add((New-Interpreter -Path $name -Label $name)) }
    }
    $usable = New-Object System.Collections.Generic.List[object]
    $seen = @{}
    foreach ($candidate in $probe) {
        $version = Get-PythonVersion $candidate
        if (-not $version) { continue }
        if (-not (Test-VersionAtLeast $version $MinPython)) { continue }
        $key = Invoke-Python $candidate @('-c', 'import sys; print(sys.executable)')
        if (-not $key) { $key = "$($candidate.Path) $($candidate.Args -join ' ')" }
        $key = $key.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $candidate.Version = $version
        $usable.Add($candidate)
    }
    return $usable.ToArray()
}

function Get-UvPath {
    $onPath = Get-CommandPath 'uv'
    if ($onPath) { return $onPath }
    $bundled = Join-Path $script:ToolsDir (Get-ExecutableName)
    if (Test-Path -LiteralPath $bundled) { return $bundled }
    return $null
}

function Test-MacHost {
    return (Test-Path -LiteralPath '/System/Library/CoreServices/SystemVersion.plist')
}

function Get-ExecutableName {
    if ($IsWindowsHost) { return 'uv.exe' }
    return 'uv'
}

function Get-PowerShellCommand {
    if ($PSVersionTable.PSEdition -eq 'Core') {
        $core = Get-CommandPath 'pwsh'
        if ($core) { return $core }
    }
    $windowsShell = Get-CommandPath 'powershell.exe'
    if ($windowsShell) { return $windowsShell }
    return 'powershell'
}

function Get-UvAssetName {
    if ($IsWindowsHost) { return '' }
    $architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
    if ($architecture -eq 'x64') { $architecture = 'x86_64' }
    if ($architecture -eq 'arm64') { $architecture = 'aarch64' }
    if ($architecture -eq 'x86') { $architecture = 'i686' }
    if (Test-MacHost) { return "uv-$architecture-apple-darwin.tar.gz" }
    return "uv-$architecture-unknown-linux-gnu.tar.gz"
}

function Install-UvBinary {
    $asset = Get-UvAssetName
    if (-not $asset) { return '' }
    $url = "https://github.com/astral-sh/uv/releases/latest/download/$asset"
    $archive = Join-Path $script:TempDir $asset
    Write-Detail "downloading $url"
    $arguments = @{
        Uri             = $url
        OutFile         = $archive
        UseBasicParsing = $true
        ErrorAction     = 'Stop'
    }
    if ($script:OldTls -and $PSVersionTable.PSVersion.Major -ge 6) { $arguments.TlsVersion = $script:OldTls }
    Invoke-WebRequest @arguments | Out-Null
    $unpack = Join-Path $script:TempDir 'uv-unpack'
    Remove-Item -LiteralPath $unpack -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $unpack | Out-Null
    & tar -xzf $archive -C $unpack
    if ($LASTEXITCODE -ne 0) { throw "tar could not unpack $asset" }
    $binary = Get-ChildItem -LiteralPath $unpack -Filter 'uv*' -File -Recurse | Select-Object -First 1
    if (-not $binary) { throw "the $asset archive did not contain uv" }
    $destination = Join-Path $script:ToolsDir (Get-ExecutableName)
    Copy-Item -LiteralPath $binary.FullName -Destination $destination -Force
    & chmod +x $destination
    return $destination
}

function Install-Uv {
    $existing = Get-UvPath
    if ($existing) { return $existing }
    Write-Step 'Bootstrapping uv'
    New-Item -ItemType Directory -Force -Path $script:ToolsDir | Out-Null
    if (-not $IsWindowsHost) {
        $binary = Install-UvBinary
        if (-not (Test-Path -LiteralPath $binary)) { throw "could not place uv in $($script:ToolsDir)" }
        Write-Success "uv is ready in $($script:ToolsDir)"
        return $binary
    }
    $installerUrl = 'https://astral.sh/uv/install.ps1'
    $installerPath = Join-Path $script:TempDir 'uv-install.ps1'
    Write-Detail "downloading $installerUrl"
    $arguments = @{
        Uri             = $installerUrl
        OutFile         = $installerPath
        UseBasicParsing = $true
        ErrorAction     = 'Stop'
    }
    if ($script:OldTls -and $PSVersionTable.PSVersion.Major -ge 6) { $arguments.TlsVersion = $script:OldTls }
    Invoke-WebRequest @arguments | Out-Null
    $env:UV_INSTALL_DIR = $script:ToolsDir
    $env:UV_NO_MODIFY_PATH = '1'
    try {
        & (Get-PowerShellCommand) -NoProfile -ExecutionPolicy Bypass -File $installerPath 2>&1 |
            ForEach-Object { Write-Detail "$_" }
        if ($LASTEXITCODE -ne 0) { throw "the uv installer exited with $LASTEXITCODE" }
    } finally {
        Remove-Item -LiteralPath 'env:UV_INSTALL_DIR' -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath 'env:UV_NO_MODIFY_PATH' -ErrorAction SilentlyContinue
    }
    $bundled = Join-Path $script:ToolsDir (Get-ExecutableName)
    if (-not (Test-Path -LiteralPath $bundled)) { throw "the uv installer did not place uv in $($script:ToolsDir)" }
    Write-Success "uv is ready in $($script:ToolsDir)"
    return $bundled
}

function Install-ManagedPython {
    $uv = Install-Uv
    Write-Step "Installing a managed Python $ManagedPython with uv"
    & $uv python install $ManagedPython
    if ($LASTEXITCODE -ne 0) { throw "uv could not install Python $ManagedPython" }
    $found = & $uv python find $ManagedPython
    if (-not $found) { throw "uv installed Python $ManagedPython but did not report its path" }
    return "$found".Trim()
}

function Resolve-Method {
    if (-not $script:InstallMethod) {
        $script:InstallMethod = if (Get-CommandPath 'uv') { 'uv' } else { 'pip' }
    }
    if ($script:InstallMethod -eq 'uv') { $script:UvPath = Install-Uv }
}

function Resolve-Python {
    $candidates = @(Get-PythonCandidates)
    if ($candidates.Count -gt 0) {
        $script:Candidates = $candidates
        return $candidates[0]
    }
    if ((Test-Interactive) -and (Confirm-Question "No Python $MinPython or newer found here. Install one with uv?" $true)) {
        $managed = New-Interpreter -Path (Install-ManagedPython)
        $script:Candidates = @($managed)
        return $managed
    }
    if ($script:InstallMethod -eq 'uv') {
        $managed = New-Interpreter -Path (Install-ManagedPython)
        $script:Candidates = @($managed)
        return $managed
    }
    throw "Python $MinPython or newer is required, install it or rerun with -Method uv"
}

function Confirm-Question {
    param([string] $Question, [bool] $Default = $true)
    if (-not (Test-Interactive)) { return $Default }
    $hint = if ($Default) { 'Y/n' } else { 'y/N' }
    while ($true) {
        $answer = Read-Host "$Question [$hint]"
        if (-not $answer) { return $Default }
        switch ($answer.Trim().ToLowerInvariant()) {
            'y' { return $true }
            'yes' { return $true }
            'n' { return $false }
            'no' { return $false }
            default { Write-Host 'Please answer y or n.' }
        }
    }
}

function Read-Answer {
    param([string] $Question, [string] $Default = '')
    $answer = Read-Host "$Question [$Default]"
    if (-not $answer) { return $Default }
    return $answer.Trim()
}

function Read-Choice {
    param([string] $Question, [string[]] $Options)
    Write-Host $Question
    for ($index = 0; $index -lt $Options.Count; $index++) {
        Write-Host ("   {0}) {1}" -f ($index + 1), $Options[$index])
    }
    $answer = Read-Answer 'Choice' '1'
    if ($answer -match '^\d+$') {
        $index = [int]$answer
        if ($index -ge 1 -and $index -le $Options.Count) { return $index }
    }
    return 1
}

function Select-Interpreter {
    param($Candidates)
    Write-Host '   Python interpreters found:' -ForegroundColor DarkGray
    for ($index = 0; $index -lt $Candidates.Count; $index++) {
        $candidate = $Candidates[$index]
        Write-Host ("   {0}) {1} ({2})" -f ($index + 1), $candidate.Label, $candidate.Version)
    }
    $answer = Read-Answer 'Interpreter' '1'
    if ($answer -match '^\d+$') {
        $index = [int]$answer
        if ($index -ge 1 -and $index -le $Candidates.Count) { return $Candidates[$index - 1] }
    }
    return $Candidates[0]
}

function Test-Commit {
    param([string] $Value)
    if (-not $Value) { return $false }
    if ($Value.Length -lt 7) { return $false }
    return ($Value -match '^[0-9a-fA-F]+$')
}

function Get-ArchiveUrl {
    return "https://codeload.github.com/$($script:Repository)/legacy.zip/$($script:GitRef)"
}

function Get-GitUrl {
    return "https://github.com/$($script:Repository).git"
}

function Install-SourceTree {
    param([string] $Destination, [switch] $Refresh)
    if ($script:LocalSource) {
        Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue
        Copy-Item -LiteralPath $script:LocalSource -Destination $Destination -Recurse -Force
        Write-Detail "copied the checkout at $($script:LocalSource)"
        return
    }
    if ($Refresh -and (Test-Path -LiteralPath (Join-Path $Destination '.git'))) {
        if (Update-GitTree $Destination) { return }
        Write-Notice 'git could not update the checkout, using the source archive'
    }
    if ($script:SourceMode -eq 'git') {
        if (-not (New-GitTree $Destination)) { throw "git could not clone $($script:Repository) at $($script:GitRef)" }
        Write-Detail "cloned $($script:Repository) at $($script:GitRef)"
        return
    }
    if ($script:SourceMode -eq 'auto' -and (Test-Command 'git')) {
        if (New-GitTree $Destination) {
            Write-Detail "cloned $($script:Repository) at $($script:GitRef)"
            return
        }
        Write-Notice 'git could not reach the repository, using the source archive'
    }
    Install-Archive $Destination
    Write-Detail "downloaded $($script:Repository) at $($script:GitRef)"
}

function New-GitTree {
    param([string] $Destination)
    Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Commit $script:GitRef) {
        & git init -q $Destination *> $null
        if ($LASTEXITCODE -ne 0) { return $false }
        & git -C $Destination remote add origin (Get-GitUrl) *> $null
        & git -C $Destination fetch -q --depth 1 origin $script:GitRef *> $null
        if ($LASTEXITCODE -ne 0) { return $false }
        & git -C $Destination checkout -q --detach FETCH_HEAD *> $null
        return ($LASTEXITCODE -eq 0)
    }
    & git clone -q --depth 1 --single-branch --branch $script:GitRef (Get-GitUrl) $Destination *> $null
    return ($LASTEXITCODE -eq 0)
}

function Update-GitTree {
    param([string] $Destination)
    & git -C $Destination fetch -q --depth 1 origin $script:GitRef *> $null
    if ($LASTEXITCODE -ne 0) {
        & git -C $Destination fetch -q --depth 1 --tags --force origin *> $null
        if ($LASTEXITCODE -ne 0) { return $false }
    }
    & git -C $Destination checkout -q --detach FETCH_HEAD *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    Write-Detail "moved the checkout to $(& git -C $Destination rev-parse --short HEAD)"
    return $true
}

function Install-Archive {
    param([string] $Destination)
    $zipPath = Join-Path $script:TempDir 'source.zip'
    $extractDir = Join-Path $script:TempDir 'extract'
    Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    $url = Get-ArchiveUrl
    Write-Detail "downloading $url"
    $arguments = @{
        Uri             = $url
        OutFile         = $zipPath
        UseBasicParsing = $true
        ErrorAction     = 'Stop'
    }
    if ($script:OldTls -and $PSVersionTable.PSVersion.Major -ge 6) { $arguments.TlsVersion = $script:OldTls }
    try {
        Invoke-WebRequest @arguments | Out-Null
    } catch {
        Write-Fail "could not download the source archive for $($script:Repository) at $($script:GitRef)"
        if (Test-Commit $script:GitRef) {
            Write-Fail 'a commit has to be reachable from a branch or tag, try -Ref main or a release tag'
        }
        throw
    }
    New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force
    $roots = @(Get-ChildItem -LiteralPath $extractDir -Directory)
    if ($roots.Count -ne 1) { throw 'the source archive was empty' }
    Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    Move-Item -LiteralPath $roots[0].FullName -Destination $Destination
}

function New-PythonEnvironment {
    Write-Step 'Preparing the Python environment'
    if ($script:HasEnvironment) {
        Write-Detail "reusing $($script:VenvDir)"
        return
    }
    $interpreter = $script:Interpreter
    if (-not $interpreter) { $interpreter = Resolve-Python }
    if ($script:InstallMethod -eq 'uv') {
        & $script:UvPath venv --quiet --python $interpreter.Path $script:VenvDir
        if ($LASTEXITCODE -ne 0) { throw 'uv could not create the environment' }
    } else {
        & $interpreter.Path @($interpreter.Args) -m venv $script:VenvDir
        if ($LASTEXITCODE -ne 0) { throw 'python could not create the environment, install the venv module first' }
    }
    $script:HasEnvironment = $true
}

function Install-GputopPackage {
    param([string] $Destination = $script:SrcDir)
    Write-Step "Installing $AppName"
    $target = "$Destination[windows]"
    $env:PIP_ROOT_USER_ACTION = 'ignore'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    try {
        if ($script:InstallMethod -eq 'uv') {
            & $script:UvPath pip install --quiet --python $script:VenvPython --upgrade $target
        } else {
            & $script:VenvPython -m pip install --quiet --upgrade $target
        }
        if ($LASTEXITCODE -ne 0) { throw "the installer could not install $AppName" }
    } finally {
        Remove-Item -LiteralPath 'env:PIP_ROOT_USER_ACTION' -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath 'env:PIP_DISABLE_PIP_VERSION_CHECK' -ErrorAction SilentlyContinue
    }
    $script:InstalledVersion = Get-InstalledVersion
    if (-not $script:InstalledVersion) { throw "$AppName is missing from the environment at $($script:VenvDir)" }
}

function Write-Command {
    New-Item -ItemType Directory -Force -Path $script:BinDir | Out-Null
    $target = Get-CommandTarget
    if ($IsWindowsHost) {
        [System.IO.File]::WriteAllLines($script:LauncherPath, @('@echo off', "`"$target`" %*"))
    } else {
        [System.IO.File]::WriteAllLines($script:LauncherPath, @('#!/bin/sh', "exec `"$target`" `"`$@`""))
    }
    Write-Detail "$($script:LauncherPath) starts $target"
}

function Test-UserPath {
    if (-not $IsWindowsHost) { return $false }
    $current = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $current) { return $false }
    return (($current -split ';') -contains $script:BinDir)
}

function Get-UserPathWithBinDir {
    param([string] $Current)
    if (-not $Current) { return $script:BinDir }
    $parts = @($Current -split ';' | Where-Object { $_ })
    if ($parts -contains $script:BinDir) { return ($parts -join ';') }
    return (@($parts) + $script:BinDir) -join ';'
}

function Add-BinDirToPath {
    if ($NoPath) { return }
    if (-not $IsWindowsHost) {
        Write-Detail 'skipped the user PATH, add the directory to your shell profile instead'
        return
    }
    if (Test-UserPath) { return }
    if (-not (Confirm-Question "Add $($script:BinDir) to your user PATH?" $true)) {
        Write-Detail "skipped the user PATH, run $($script:LauncherPath) by path instead"
        return
    }
    $current = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $updated = Get-UserPathWithBinDir $current
    [System.Environment]::SetEnvironmentVariable('Path', $updated, 'User')
    $env:Path = "$($script:BinDir)$([System.IO.Path]::PathSeparator)$env:Path"
    Write-Detail "added $($script:BinDir) to your user PATH"
    Write-Plain ''
    Write-Plain 'Open a new terminal so the change takes effect.'
}

function Remove-BinDirFromPath {
    if (-not $IsWindowsHost) { return }
    $current = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $current) { return }
    $parts = @($current -split ';' | Where-Object { $_ -and $_ -ne $script:BinDir })
    [System.Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')
    Write-Detail "removed $($script:BinDir) from your user PATH"
}

function Get-UninstallRegistryPath {
    return 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\gputop'
}

function Write-Uninstaller {
    param([string] $Path)
    $body = @'
$ErrorActionPreference = 'SilentlyContinue'
$dataDir = '__DATA__'
$binDir = '__BIN__'
$shim = Join-Path $binDir '__SHIM__'
Write-Host "-> Removing gputop from $binDir" -ForegroundColor Cyan
if (Test-Path -LiteralPath $shim) { Remove-Item -LiteralPath $shim -Force }
$userPath = [System.Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath) {
    $parts = @($userPath -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ne $binDir.TrimEnd('\') })
    [System.Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')
}
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\gputop'
if (Test-Path -LiteralPath $key) { Remove-Item -LiteralPath $key -Recurse -Force }
if (Test-Path -LiteralPath $dataDir) { Remove-Item -LiteralPath $dataDir -Recurse -Force }
Write-Host 'gputop is uninstalled' -ForegroundColor Green
'@
    $body = $body.Replace('__DATA__', (Escape-SingleQuotes $script:DataDir))
    $body = $body.Replace('__BIN__', (Escape-SingleQuotes $script:BinDir))
    $body = $body.Replace('__SHIM__', (Get-CommandName))
    [System.IO.File]::WriteAllText($Path, $body + [System.Environment]::NewLine)
}

function Escape-SingleQuotes {
    param([string] $Text)
    if (-not $Text) { return '' }
    return $Text.Replace("'", "''")
}

function Register-UninstallEntry {
    if (-not $IsWindowsHost) { return }
    $key = Get-UninstallRegistryPath
    New-Item -Path $key -Force | Out-Null
    $uninstaller = Join-Path $script:DataDir 'uninstall.ps1'
    $shell = Get-CommandPath 'powershell.exe'
    $command = if ($shell) {
        "`"$shell`" -NoProfile -ExecutionPolicy Bypass -File `"$uninstaller`""
    } else {
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstaller`""
    }
    $values = [ordered]@{
        DisplayName         = 'gputop'
        DisplayVersion      = $script:InstalledVersion
        Publisher           = 'gputop contributors'
        InstallLocation     = $script:DataDir
        UninstallString     = $command
        QuietUninstallString = $command
        NoModify            = 1
        NoRepair            = 1
    }
    foreach ($entry in $values.GetEnumerator()) {
        Set-ItemProperty -Path $key -Name $entry.Key -Value $entry.Value -Type String
    }
    Write-Detail "registered gputop $($script:InstalledVersion) in Apps and features"
}

function Remove-UninstallEntry {
    if (-not $IsWindowsHost) { return }
    $key = Get-UninstallRegistryPath
    if (Test-Path -LiteralPath $key) {
        Remove-Item -LiteralPath $key -Recurse -Force
        Write-Detail 'removed the Apps and features entry'
    }
}

function Get-ForeignInstall {
    $command = Get-Command -Name $AppName -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { (Split-Path -Parent $_.Source) -ne $script:BinDir } |
        Select-Object -First 1
    if (-not $command) { return $null }
    $folder = Split-Path -Parent $command.Source
    $python = $null
    foreach ($exe in @('python.exe', 'python3.exe', 'python')) {
        $candidate = Join-Path $folder $exe
        if (Test-Path -LiteralPath $candidate) { $python = $candidate; break }
    }
    if (-not $python) { return $null }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $version = & $python -c 'import importlib.metadata as m; print(m.version("gputop"))' 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $version) { return $null }
    } finally {
        $ErrorActionPreference = $previous
    }
    $homeDirectory = Get-HomeDirectory
    $userOwned = $python.StartsWith($homeDirectory, [System.StringComparison]::OrdinalIgnoreCase) -or
        $python -match '\\(venv|\.venv|envs|pyenv)\\' -or
        $python -match 'uv\\tools\\'
    return [PSCustomObject]@{
        Path      = $command.Source
        Python    = $python
        Version   = "$version".Trim()
        UserOwned = [bool]$userOwned
    }
}

function Get-SourceLabel {
    if ($script:LocalSource) { return "$($script:LocalSource) (local checkout)" }
    return "$($script:Repository) at $($script:GitRef)"
}

function Write-Plan {
    param([System.Collections.Specialized.OrderedDictionary] $Entries)
    Write-Plain ''
    Write-Step 'Plan'
    foreach ($key in $Entries.Keys) {
        Write-Field $key $Entries[$key]
    }
    Write-Plain ''
}

function Confirm-Plan {
    if ($DryRun) { return $true }
    if (-not (Test-Interactive)) { return $true }
    return (Confirm-Question 'Proceed?' $true)
}

function Write-RunHint {
    Write-Plain ''
    if (Test-UserPath) {
        Write-Plain "   run $AppName to start"
    } else {
        Write-Plain "   run $($script:LauncherPath) --version to check the install, then open a new terminal"
    }
    Write-Host "   $AppName --doctor lists the metrics this machine can read" -ForegroundColor DarkGray
    Write-Plain ''
}

function Invoke-Install {
    Write-Plan ([ordered]@{
        action  = "install $AppName from $(Get-SourceLabel)"
        method  = $script:InstallMethod
        python  = "$($script:Interpreter.Label) $(Get-PythonFullVersion $script:Interpreter)"
        data    = $script:DataDir
        command = $script:LauncherPath
    })
    if ($DryRun) { return }
    if (-not (Confirm-Plan)) { throw 'cancelled' }
    Write-Step 'Fetching the source'
    Install-SourceTree -Destination $script:SrcDir
    New-PythonEnvironment
    $layout = Get-VenvLayout
    $script:VenvBin = $layout.Folder
    $script:VenvPython = $layout.Python
    Install-GputopPackage
    Write-Step "Linking the $AppName command"
    Write-Command
    Add-BinDirToPath
    Write-State
    Write-Uninstaller -Path (Join-Path $script:DataDir 'uninstall.ps1')
    Register-UninstallEntry
    Write-Plain ''
    Write-Success "installed $($script:InstalledVersion) in $($script:DataDir)"
    Write-RunHint
}

function Invoke-Update {
    $current = Get-InstalledVersion
    if (-not $current) { $current = 'unknown' }
    Write-Plan ([ordered]@{
        action  = "update $AppName in $($script:DataDir)"
        source  = Get-SourceLabel
        method  = $script:InstallMethod
        version = $current
        command = $script:LauncherPath
    })
    if ($DryRun) { return }
    if (-not (Confirm-Plan)) { throw 'cancelled' }
    Write-Step 'Refreshing the source'
    Install-SourceTree -Destination $script:SrcDir -Refresh
    if (-not $script:HasEnvironment) {
        $script:Interpreter = Resolve-Python
        New-PythonEnvironment
    }
    $layout = Get-VenvLayout
    $script:VenvBin = $layout.Folder
    $script:VenvPython = $layout.Python
    Install-GputopPackage
    if (-not (Test-Path -LiteralPath $script:LauncherPath)) {
        Write-Step "Linking the $AppName command"
        Write-Command
    }
    Add-BinDirToPath
    $sourceVersion = Get-SourceVersion
    Write-State
    Write-Plain ''
    if ($script:InstalledVersion -eq $current) {
        Write-Success "$($script:InstalledVersion) is up to date"
    } else {
        Write-Success "updated: $current to $($script:InstalledVersion)"
    }
    if ($sourceVersion -and $sourceVersion -ne $script:InstalledVersion) {
        Write-Notice "the source reports $sourceVersion while the environment reports $($script:InstalledVersion)"
    }
    Write-RunHint
}

function Update-ForeignInstall {
    param($Foreign)
    Write-Plan ([ordered]@{
        action   = "update the existing $AppName"
        location = $Foreign.Path
        python   = $Foreign.Python
        source   = Get-SourceLabel
        version  = $Foreign.Version
    })
    if ($DryRun) { return }
    if (-not (Confirm-Question 'Update this installation in place?' $true)) { throw 'cancelled' }
    $staging = Join-Path $script:TempDir 'src'
    Install-SourceTree -Destination $staging
    $env:PIP_ROOT_USER_ACTION = 'ignore'
    try {
        & $Foreign.Python -m pip install --quiet --upgrade "$staging[windows]"
        if ($LASTEXITCODE -ne 0) { throw "pip could not update $AppName in $($Foreign.Python)" }
    } finally {
        Remove-Item -LiteralPath 'env:PIP_ROOT_USER_ACTION' -ErrorAction SilentlyContinue
    }
    $after = & $Foreign.Python -c 'import importlib.metadata as m; print(m.version("gputop"))' 2>$null
    Write-Plain ''
    Write-Success "updated: $($Foreign.Version) to $("$after".Trim())"
    Write-Plain "   $($Foreign.Path)"
    Write-Plain ''
}

function Remove-ForeignInstall {
    param($Foreign)
    Write-Step "Removing $AppName from $($Foreign.Python)"
    if ($DryRun) { return }
    if (-not (Confirm-Question "Uninstall $AppName from $($Foreign.Python)?" $false)) { throw 'cancelled' }
    & $Foreign.Python -m pip uninstall -y $AppName
    if ($LASTEXITCODE -ne 0) { throw "pip could not uninstall $AppName" }
    Write-Success "removed from $($Foreign.Python)"
}

function Invoke-Uninstall {
    $foreign = Get-ForeignInstall
    if (-not (Test-Path -LiteralPath $script:DataDir) -and -not $foreign) {
        Write-Plain "no $AppName installation found"
        return
    }
    Write-Plan ([ordered]@{
        action  = "uninstall $AppName"
        data    = $script:DataDir
        command = $script:LauncherPath
    })
    if ($DryRun) { return }
    if (-not (Confirm-Plan)) { throw 'cancelled' }
    if (Test-Path -LiteralPath $script:LauncherPath) {
        Remove-Item -LiteralPath $script:LauncherPath -Force
        Write-Detail "removed $($script:LauncherPath)"
    }
    Remove-BinDirFromPath
    Remove-UninstallEntry
    if (Test-Path -LiteralPath $script:DataDir) {
        Remove-Item -LiteralPath $script:DataDir -Recurse -Force
        Write-Detail "removed $($script:DataDir)"
    }
    if ($foreign) { Remove-ForeignInstall $foreign }
    Write-Plain ''
    Write-Success 'is uninstalled'
    Write-Plain ''
}

function Invoke-ManagedUpdate {
    Write-Step 'Existing installation found'
    Write-Field 'location' $script:DataDir
    Write-Field 'version' (Get-InstalledVersion)
    Write-Plain ''
    if (-not (Confirm-Question 'Update this installation?' $true)) {
        Write-Plain 'left the existing installation unchanged'
        return
    }
    $state = Read-State
    if ($state['method']) { $script:InstallMethod = $state['method'] }
    Resolve-Method
    Invoke-Update
}

function Invoke-InteractiveInstall {
    Write-Step "Setting up $AppName"
    if (-not (Confirm-Question "Install into $($script:DataDir)?" $true)) {
        $chosen = Read-Answer 'Install directory' $script:DataDir
        if (-not $chosen) { throw 'an install directory is required' }
        Set-Layout -DataDirectory $chosen -BinDirectory $script:BinDir
    }
    if (Get-CommandPath 'uv') {
        $choice = Read-Choice "How should $AppName be installed?" @(
            'pip, the standard installer',
            'uv, fast and able to install Python'
        )
        $script:InstallMethod = if ($choice -eq 2) { 'uv' } else { 'pip' }
    } else {
        $script:InstallMethod = 'pip'
    }
    Resolve-Method
    $candidates = @(Get-PythonCandidates)
    if ($candidates.Count -eq 0) {
        if (Confirm-Question "No Python $MinPython or newer found here. Install one with uv?" $true) {
            $candidates = @(New-Interpreter -Path (Install-ManagedPython))
        } else {
            throw "Python $MinPython or newer is required"
        }
        $script:Candidates = $candidates
        $script:Interpreter = $candidates[0]
    } else {
        $script:Candidates = $candidates
        $script:Interpreter = Select-Interpreter $candidates
    }
    Invoke-Install
}

function Find-LocalSource {
    if (-not $PSScriptRoot) { return '' }
    $here = $PSScriptRoot
    if (Test-Path -LiteralPath (Join-Path $here 'pyproject.toml')) {
        if (Test-Path -LiteralPath (Join-Path $here (Join-Path $AppName '__init__.py'))) { return $here }
    }
    return ''
}

function Invoke-InstallAction {
    $interactive = (Test-Interactive) -and (-not $script:LocalSource) -and (-not $script:RequestedPython) -and (-not $script:InstallMethod) -and (-not $DryRun)
    if ($interactive) {
        $state = Read-State
        if ($state.Count -gt 0 -or $script:HasEnvironment) {
            Invoke-ManagedUpdate
            return
        }
        $foreign = Get-ForeignInstall
        if ($foreign) {
            Write-Step "Existing $AppName found"
            Write-Field 'location' $foreign.Path
            Write-Field 'version' $foreign.Version
            Write-Plain ''
            if (Confirm-Question 'Update that installation?' $true) {
                if ($foreign.UserOwned) {
                    Update-ForeignInstall $foreign
                } else {
                    Write-Notice "$($foreign.Path) belongs to an environment outside your user account, update it with the tool that installed it"
                    Write-Notice "installing a separate copy in $($script:DataDir), run $($script:LauncherPath) to use it"
                    Resolve-Method
                    $script:Interpreter = Resolve-Python
                    Invoke-Install
                }
                return
            }
        }
        Invoke-InteractiveInstall
        return
    }
    Resolve-Method
    $script:Interpreter = Resolve-Python
    Invoke-Install
}

function Invoke-UpdateAction {
    $state = Read-State
    $managed = $script:HasEnvironment -or $state.Count -gt 0
    if (-not $managed) {
        $foreign = Get-ForeignInstall
        if (-not $foreign) {
            Write-Plain "no $AppName installation found, installing instead"
            Resolve-Method
            $script:Interpreter = Resolve-Python
            Invoke-Install
            return
        }
        if (-not $foreign.UserOwned) {
            throw "$($foreign.Path) belongs to an environment outside your user account, update it with the tool that installed it"
        }
        Update-ForeignInstall $foreign
        return
    }
    if (-not $script:InstallMethod -and $state['method']) { $script:InstallMethod = $state['method'] }
    Resolve-Method
    if (-not $script:HasEnvironment) { $script:Interpreter = Resolve-Python }
    Invoke-Update
}

function Write-Usage {
    $lines = @(
        "$AppName installer $ScriptVersion for Windows",
        '',
        "  irm https://raw.githubusercontent.com/$($script:Repository)/$($script:GitRef)/install.ps1 | iex",
        '',
        'With no arguments the installer offers an interactive menu, including updating',
        'an installation that is already on this machine. Parameters do not bind when',
        'the script is piped, so the environment variables below work in both cases.',
        '',
        'Actions:',
        '  -Action Install      Install gputop (default)',
        '  -Action Update       Update an existing installation',
        '  -Action Uninstall    Remove an installation made by this script',
        '  -Yes                 Accept the defaults, never prompt',
        '  -DryRun              Print the plan and exit without changing anything',
        '',
        'Install options:',
        '  -Dir DIR             Data directory (default: %LOCALAPPDATA%\Programs\gputop)',
        '  -BinDir DIR          Directory for the gputop command',
        '  -Python PATH         Python interpreter to use',
        '  -Method pip|uv       Installer to use (default: pip, uv when uv is available)',
        '  -Ref REF             Git ref to install (default: main)',
        '  -Repo OWNER/NAME     GitHub repository',
        '  -Source PATH         Install from a local checkout instead of the repository',
        '  -NoPath              Leave the user PATH alone',
        '  -Help                Show this help',
        '',
        'Environment equivalents: GPUTOP_DIR, GPUTOP_BIN_DIR, GPUTOP_PYTHON,',
        'GPUTOP_METHOD, GPUTOP_REF, GPUTOP_REPO, GPUTOP_SOURCE, GPUTOP_YES.',
        '',
        'The installer runs as your own user, never needs administrator rights, and',
        'keeps its Python environment inside the data directory so an update can',
        'replace it cleanly.'
    )
    Write-Plain ($lines -join [System.Environment]::NewLine)
}

function Remove-TempDirectory {
    if ($script:TempDir -and (Test-Path -LiteralPath $script:TempDir)) {
        Remove-Item -LiteralPath $script:TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Main {
    if ($PSVersionTable.PSVersion.Major -lt 5) { throw 'Windows PowerShell 5.1 or PowerShell 7 is required' }
    $script:Repository = Get-PreferredSetting $Repo 'GPUTOP_REPO' 'RobertFlexx/gputop'
    $script:GitRef = Get-PreferredSetting $Ref 'GPUTOP_REF' 'main'
    if ($Help) {
        Write-Usage
        return
    }
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        $script:OldTls = 'Tls12'
        [System.Net.ServicePointManager]::SecurityProtocol =
            [System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12
    }
    $script:LocalSource = Get-PreferredSetting $Source 'GPUTOP_SOURCE' (Find-LocalSource)
    $script:RequestedPython = Get-PreferredSetting $Python 'GPUTOP_PYTHON' ''
    $script:InstallMethod = Get-PreferredSetting $Method 'GPUTOP_METHOD' ''
    $script:SourceMode = if ($env:GPUTOP_SOURCE_MODE) { $env:GPUTOP_SOURCE_MODE } else { 'auto' }
    $dataDirectory = Get-PreferredSetting $Dir 'GPUTOP_DIR' (Get-DefaultDataDir)
    $binDirectory = Get-PreferredSetting $BinDir 'GPUTOP_BIN_DIR' (Get-DefaultBinDir)
    $script:TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ('gputop-install-' + [System.Guid]::NewGuid().ToString('N').Substring(0, 12))
    New-Item -ItemType Directory -Force -Path $script:TempDir | Out-Null
    Set-Layout -DataDirectory $dataDirectory -BinDirectory $binDirectory
    try {
        switch ($Action) {
            'Uninstall' { Invoke-Uninstall }
            'Update' { Invoke-UpdateAction }
            default { Invoke-InstallAction }
        }
    } finally {
        Remove-TempDirectory
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-Main
    } catch {
        Write-Fail $_.Exception.Message
        exit 1
    }
}
