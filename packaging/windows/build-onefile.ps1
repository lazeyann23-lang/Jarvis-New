#Requires -Version 5.1
<#
.SYNOPSIS
  Build one portable PersonalJarvis.exe for Windows x64 (no target-PC Python/Node).
.DESCRIPTION
  Build-host only: Python, PyInstaller, project desktop dependencies and Internet.
  Requires the checked-in compiled React/Vite dist. Never embeds personal config.
  Does not modify the established onedir installer build.
#>
[CmdletBinding()]
param(
    [string] $Python = 'python',
    [string] $NodeVersion = '22.16.0'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
    -not [Environment]::Is64BitOperatingSystem) {
    throw 'This build must run on Windows x64.'
}
if ($NodeVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw 'NodeVersion must be a pinned major.minor.patch release.'
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dist = Join-Path $root 'jarvis\ui\web\dist\index.html'
$config = Join-Path $root 'jarvis.toml'
$example = Join-Path $root 'jarvis.toml.example'
$spec = Join-Path $root 'jarvis.spec'
$exe = Join-Path $root 'dist\PersonalJarvis.exe'
$outZip = Join-Path $root 'dist\PersonalJarvis-OneFile-Windows-x64.zip'
$nodeScratch = Join-Path ([IO.Path]::GetTempPath()) ('jarvis-onefile-' + [guid]::NewGuid().ToString('N'))
$originalNodeDir = [Environment]::GetEnvironmentVariable('JARVIS_PORTABLE_NODE_DIR', 'Process')
$originalOnefile = [Environment]::GetEnvironmentVariable('JARVIS_WINDOWS_ONEFILE', 'Process')

if (-not (Test-Path -LiteralPath $spec)) { throw 'jarvis.spec missing' }
if (-not (Test-Path -LiteralPath $dist)) { throw 'Built React dist/index.html missing. Build with real npm dependencies first.' }
if (-not (Test-Path -LiteralPath $example)) { throw 'Default jarvis.toml.example missing' }
# Do not distribute private keys/config from the developer machine.
if (Test-Path -LiteralPath $config) {
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $config).Hash -ne
        (Get-FileHash -Algorithm SHA256 -LiteralPath $example).Hash) {
        throw 'Personal jarvis.toml detected: refusing to freeze private settings.'
    }
} else {
    Copy-Item -LiteralPath $example -Destination $config
}

New-Item -ItemType Directory -Path $nodeScratch -Force | Out-Null
try {
    $nodeFile = "node-v$NodeVersion-win-x64.zip"
    $baseUrl = "https://nodejs.org/dist/v$NodeVersion"
    $manifest = Join-Path $nodeScratch 'SHASUMS256.txt'
    $nodeArchive = Join-Path $nodeScratch $nodeFile
    Invoke-WebRequest -Uri "$baseUrl/SHASUMS256.txt" -OutFile $manifest -UseBasicParsing
    $matches = @(Get-Content -LiteralPath $manifest | Where-Object {
        $_ -match ('^[A-Fa-f0-9]{64}\s+' + [regex]::Escape($nodeFile) + '$')
    })
    if ($matches.Count -ne 1) { throw 'Missing or ambiguous Node SHA256 entry' }
    $expected = ($matches[0] -split '\s+')[0].ToUpperInvariant()
    Invoke-WebRequest -Uri "$baseUrl/$nodeFile" -OutFile $nodeArchive -UseBasicParsing
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $nodeArchive).Hash.ToUpperInvariant()
    if ($actual -ne $expected) { throw 'Node archive SHA256 does not match vendor manifest' }

    Expand-Archive -LiteralPath $nodeArchive -DestinationPath $nodeScratch
    $nodeDir = Join-Path $nodeScratch "node-v$NodeVersion-win-x64"
    $nodeExe = Join-Path $nodeDir 'node.exe'
    if (-not (Test-Path $nodeExe) -or -not (Test-Path (Join-Path $nodeDir 'LICENSE'))) {
        throw 'Vendor Node archive does not include node.exe and LICENSE'
    }
    $detectedVersion = & $nodeExe --version
    if ($LASTEXITCODE -ne 0 -or $detectedVersion.Trim() -ne "v$NodeVersion") {
        throw 'Extracted Node runtime failed version check'
    }

    $env:JARVIS_WINDOWS_ONEFILE = '1'
    $env:JARVIS_PORTABLE_NODE_DIR = $nodeDir
    Push-Location $root
    try {
        & $Python -m PyInstaller $spec --noconfirm --clean
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller exited $LASTEXITCODE" }
    } finally { Pop-Location }

    if (-not (Test-Path -LiteralPath $exe)) { throw 'PyInstaller returned without creating PersonalJarvis.exe' }
    # A real PE executable has MZ and PE signatures, not a renamed script.
    $stream = [IO.File]::OpenRead($exe)
    try {
        $reader = [IO.BinaryReader]::new($stream)
        if ($reader.ReadUInt16() -ne 0x5A4D) { throw 'Not an MZ executable' }
        $stream.Position = 0x3c
        $peOffset = $reader.ReadInt32()
        if ($peOffset -lt 64 -or $peOffset -gt ($stream.Length - 4)) { throw 'Invalid PE header offset' }
        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x00004550) { throw 'Missing PE signature' }
    } finally { $stream.Dispose() }

    if (Test-Path $outZip) { Remove-Item -LiteralPath $outZip -Force }
    Compress-Archive -LiteralPath $exe -DestinationPath $outZip -CompressionLevel Optimal
    & $Python (Join-Path $PSScriptRoot 'verify-onefile.py') $outZip
    if ($LASTEXITCODE -ne 0) { throw 'Portable ZIP integrity validation failed' }
    Write-Host "CREATED: $outZip"
    Write-Host "SHA256: $((Get-FileHash -Algorithm SHA256 -LiteralPath $outZip).Hash.ToLowerInvariant())"
} finally {
    [Environment]::SetEnvironmentVariable('JARVIS_PORTABLE_NODE_DIR', $originalNodeDir, 'Process')
    [Environment]::SetEnvironmentVariable('JARVIS_WINDOWS_ONEFILE', $originalOnefile, 'Process')
    Remove-Item -LiteralPath $nodeScratch -Recurse -Force -ErrorAction SilentlyContinue
}
