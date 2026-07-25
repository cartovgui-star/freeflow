param(
    [string]$Version = "",
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$SourceFile = Join-Path $ProjectRoot "freeflow.py"
$RequirementsFile = Join-Path $ProjectRoot "requirements.txt"
$BuildRequirementsFile = Join-Path $ProjectRoot "requirements-build.txt"
$IconFile = Join-Path $ProjectRoot "assets\freeflow.ico"
$BuildVenv = Join-Path $ProjectRoot ".build-venv"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
$BuildRoot = Join-Path $ProjectRoot "build"
$PyInstallerWork = Join-Path $BuildRoot "pyinstaller-work"
$PyInstallerSpec = Join-Path $BuildRoot "pyinstaller-spec"
$DistRoot = Join-Path $ProjectRoot "dist"
$AppBundle = Join-Path $DistRoot "FreeFlow"
$InstallerOutput = Join-Path $DistRoot "installer"
$InstallerScript = Join-Path $PSScriptRoot "freeflow.iss"

function Assert-Success([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

function Remove-ProjectBuildDirectory([string]$Path) {
    $ResolvedTarget = [System.IO.Path]::GetFullPath($Path)
    $ProjectPrefix = $ProjectRoot.TrimEnd("\") + "\"
    if (-not $ResolvedTarget.StartsWith(
        $ProjectPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to remove a directory outside the project: $ResolvedTarget"
    }
    if (Test-Path -LiteralPath $ResolvedTarget) {
        Remove-Item -LiteralPath $ResolvedTarget -Recurse -Force
    }
}

if (-not (Test-Path -LiteralPath $SourceFile)) {
    throw "Run this script from a complete FreeFlow checkout."
}

$SourceText = Get-Content -LiteralPath $SourceFile -Raw -Encoding UTF8
$VersionMatch = [regex]::Match($SourceText, 'VERSION\s*=\s*"(?<version>[0-9]+\.[0-9]+\.[0-9]+)"')
if (-not $VersionMatch.Success) {
    throw "Could not read VERSION from freeflow.py."
}
$SourceVersion = $VersionMatch.Groups["version"].Value
if (-not $Version) {
    $Version = $SourceVersion
}
if ($Version -ne $SourceVersion) {
    throw "Requested version $Version does not match freeflow.py version $SourceVersion."
}

$VersionParts = @($Version.Split(".") | ForEach-Object { [int]$_ })
while ($VersionParts.Count -lt 4) {
    $VersionParts += 0
}
$VersionTuple = $VersionParts[0..3] -join ", "

if (-not (Test-Path -LiteralPath $BuildPython)) {
    $PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    $SystemPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        & $PyLauncher.Source -3.11 -m venv $BuildVenv
        Assert-Success "Creating the build environment"
    } elseif ($SystemPython) {
        & $SystemPython.Source -m venv $BuildVenv
        Assert-Success "Creating the build environment"
    } else {
        throw "Python 3.11 or newer is required to build FreeFlow."
    }
}

if (-not $SkipDependencyInstall) {
    & $BuildPython -m pip install --upgrade pip
    Assert-Success "Updating pip"
    & $BuildPython -m pip install -r $RequirementsFile -r $BuildRequirementsFile
    Assert-Success "Installing build dependencies"
}

Remove-ProjectBuildDirectory $PyInstallerWork
Remove-ProjectBuildDirectory $PyInstallerSpec
Remove-ProjectBuildDirectory $AppBundle
Remove-ProjectBuildDirectory $InstallerOutput
New-Item -ItemType Directory -Path $PyInstallerWork,$PyInstallerSpec,$InstallerOutput -Force | Out-Null

$VersionInfoFile = Join-Path $PyInstallerWork "version_info.txt"
$VersionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($VersionTuple),
    prodvers=($VersionTuple),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'FreeFlow contributors'),
          StringStruct('FileDescription', 'Private, local-first voice dictation'),
          StringStruct('FileVersion', '$Version'),
          StringStruct('InternalName', 'FreeFlow'),
          StringStruct('LegalCopyright', 'Copyright (c) 2026 FreeFlow contributors'),
          StringStruct('OriginalFilename', 'FreeFlow.exe'),
          StringStruct('ProductName', 'FreeFlow'),
          StringStruct('ProductVersion', '$Version')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
[System.IO.File]::WriteAllText(
    $VersionInfoFile,
    $VersionInfo,
    [System.Text.UTF8Encoding]::new($false)
)

$PyInstallerArguments = @(
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",
    "--name", "FreeFlow",
    "--icon", $IconFile,
    "--version-file", $VersionInfoFile,
    "--distpath", $DistRoot,
    "--workpath", $PyInstallerWork,
    "--specpath", $PyInstallerSpec,
    "--collect-all", "faster_whisper",
    "--collect-all", "ctranslate2",
    "--collect-binaries", "sounddevice",
    "--copy-metadata", "faster-whisper",
    $SourceFile
)

Push-Location $ProjectRoot
try {
    & $BuildPython -m PyInstaller @PyInstallerArguments
    Assert-Success "Building the FreeFlow application"
} finally {
    Pop-Location
}

$BundledExe = Join-Path $AppBundle "FreeFlow.exe"
if (-not (Test-Path -LiteralPath $BundledExe)) {
    throw "PyInstaller did not create $BundledExe."
}

& $BundledExe --smoke-test
Assert-Success "Testing the packaged application"

$IsccCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 7\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 7\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
)
$Iscc = $IsccCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $Iscc) {
    throw "Inno Setup was not found. Install it with: winget install --id JRSoftware.InnoSetup -e"
}

& $Iscc "/DMyAppVersion=$Version" $InstallerScript
Assert-Success "Building the Windows installer"

$Installer = Join-Path $InstallerOutput "FreeFlow-Setup-$Version-x64.exe"
if (-not (Test-Path -LiteralPath $Installer)) {
    throw "Inno Setup did not create $Installer."
}

$Hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
$ChecksumFile = "$Installer.sha256"
$ChecksumLine = "$Hash  $([System.IO.Path]::GetFileName($Installer))`n"
[System.IO.File]::WriteAllText(
    $ChecksumFile,
    $ChecksumLine,
    [System.Text.UTF8Encoding]::new($false)
)

Write-Host ""
Write-Host "FreeFlow $Version installer ready:" -ForegroundColor Green
Write-Host "  $Installer"
Write-Host "  SHA-256: $Hash"
