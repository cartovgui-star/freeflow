param(
    [switch]$StartWithWindows,
    [switch]$NoDesktopShortcut,
    [switch]$NoLaunch,
    [switch]$CpuOnly
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Launcher = Join-Path $ProjectDir "run_silent.vbs"

Write-Host ""
Write-Host "  FreeFlow setup" -ForegroundColor Magenta
Write-Host "  Private, local-first dictation for Windows" -ForegroundColor DarkGray
Write-Host ""

if (-not (Test-Path -LiteralPath $VenvPython)) {
    $PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    $Python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        Write-Host "[1/4] Creating Python environment..."
        & $PyLauncher.Source -3.11 -m venv (Join-Path $ProjectDir ".venv")
    } elseif ($Python) {
        Write-Host "[1/4] Creating Python environment..."
        & $Python.Source -m venv (Join-Path $ProjectDir ".venv")
    } else {
        throw "Python 3.11+ was not found. Install Python from python.org, then run setup.bat again."
    }
} else {
    Write-Host "[1/4] Python environment already exists." -ForegroundColor DarkGray
}

Write-Host "[2/4] Installing speech and audio components..."
& $VenvPython -m pip install --upgrade pip
$HasNvidiaGpu = $false
if (-not $CpuOnly) {
    try {
        $HasNvidiaGpu = [bool](Get-CimInstance Win32_VideoController |
            Where-Object { $_.Name -match "NVIDIA" } |
            Select-Object -First 1)
    } catch {
        $HasNvidiaGpu = $false
    }
}
if ($HasNvidiaGpu) {
    Write-Host "      NVIDIA GPU detected; enabling CUDA acceleration." -ForegroundColor DarkGray
    $RequirementsFile = Join-Path $ProjectDir "requirements-gpu.txt"
} else {
    Write-Host "      Installing CPU-compatible dependencies." -ForegroundColor DarkGray
    $RequirementsFile = Join-Path $ProjectDir "requirements.txt"
}
& $VenvPython -m pip install -r $RequirementsFile

Write-Host "[3/4] Checking the installation..."
& $VenvPython (Join-Path $ProjectDir "freeflow.py") --smoke-test

Write-Host "[4/4] Creating shortcuts..."
$Shell = New-Object -ComObject WScript.Shell
if (-not $NoDesktopShortcut) {
    $DesktopShortcut = $Shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "FreeFlow.lnk"))
    $DesktopShortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
    $DesktopShortcut.Arguments = "`"$Launcher`""
    $DesktopShortcut.WorkingDirectory = $ProjectDir
    $DesktopShortcut.Description = "Private, local-first dictation"
    $DesktopShortcut.Save()
}
if ($StartWithWindows) {
    $StartupDir = [Environment]::GetFolderPath("Startup")
    $StartupShortcut = $Shell.CreateShortcut((Join-Path $StartupDir "FreeFlow.lnk"))
    $StartupShortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
    $StartupShortcut.Arguments = "`"$Launcher`""
    $StartupShortcut.WorkingDirectory = $ProjectDir
    $StartupShortcut.Description = "Start FreeFlow with Windows"
    $StartupShortcut.Save()
}

Write-Host ""
Write-Host "FreeFlow is ready." -ForegroundColor Green
Write-Host "The first launch may download the selected Whisper model." -ForegroundColor DarkGray

if (-not $NoLaunch) {
    Start-Process -FilePath "$env:WINDIR\System32\wscript.exe" -ArgumentList "`"$Launcher`""
}
