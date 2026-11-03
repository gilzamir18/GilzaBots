# =========================================================
# GilzaBots Community Windows installer for PowerShell
# Usage: irm https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.ps1 | iex
# =========================================================

$ErrorActionPreference = "Stop"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "     GilzaBots Installer (Community)       " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

$installDir = "$env:LOCALAPPDATA\GilzaBots"
$tempZip    = "$env:TEMP\gilzabots_release.zip"
$repoOwner  = "gilzamir18"
$repoName   = "GilzaBots"
# "HEAD" lets raw.githubusercontent.com resolve the repository's default branch,
# so this installer does not break every time the release branch is renamed.
# A literal branch name here is what made the fallback fetch 404.
$uninstallerRef = "HEAD"

$headers = @{
    "Accept"               = "application/vnd.github+json"
    "User-Agent"           = "GilzaBots-Installer"
    "X-GitHub-Api-Version" = "2022-11-28"
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-GilzaBotsDownloadUrl {
    if ($env:GILZABOTS_DOWNLOAD_URL) {
        if ($env:GILZABOTS_DOWNLOAD_URL -match "/actions/runs/.*/artifacts/") {
            throw "GILZABOTS_DOWNLOAD_URL points to a GitHub Actions artifact. Please use a GitHub Release asset URL instead."
        }
        return $env:GILZABOTS_DOWNLOAD_URL
    }

    Write-Host "Checking the latest GitHub release ($repoOwner/$repoName)..." -ForegroundColor Yellow
    $releaseApi = Invoke-RestMethod -Uri "https://api.github.com/repos/$repoOwner/$repoName/releases/latest" -Headers $headers
    # Reported as DisplayVersion in Installed apps, so the entry tracks the
    # release that was actually installed instead of a hardcoded literal.
    $script:releaseTag = ($releaseApi.tag_name -replace '^v', '')
    $asset = $releaseApi.assets |
        Where-Object { $_.name -eq "GilzaBots-windows-x64.zip" -and $_.state -eq "uploaded" } |
        Select-Object -First 1

    if (-not $asset) {
        throw "The latest GitHub release does not contain GilzaBots-windows-x64.zip. Create a tagged release with the Windows asset before running this installer."
    }

    if ($asset.browser_download_url -match "/actions/runs/.*/artifacts/") {
        throw "The latest Windows download points to a GitHub Actions artifact instead of a GitHub Release asset."
    }

    if ($asset.size) {
        $script:downloadSizeMB = [math]::Round($asset.size / 1MB)
    }
    return $asset.browser_download_url
}

function Test-GilzaBotsRunning {
    param([Parameter(Mandatory = $true)][string]$Directory)

    # Extracting over a running copy fails on its locked executables partway
    # through, leaving a mix of old and new files behind.
    $prefix = $Directory.TrimEnd('\') + '\'
    $running = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    }
    return [bool]$running
}

function Invoke-DownloadWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$OutFile
    )

    # Windows PowerShell 5.1 redraws its progress bar for every chunk received
    # (and Expand-Archive for every file extracted), which makes both many
    # times slower on a release this size. The preference is set only in this
    # function's scope and in the extraction block below: this script runs in
    # the caller's session (`irm | iex`), and a script-level assignment would
    # silence progress in the user's own console as well.
    $ProgressPreference = "SilentlyContinue"
    $maxAttempts = 3
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        try {
            if (Test-Path $OutFile) {
                Remove-Item -Path $OutFile -Force
            }

            Invoke-WebRequest -Uri $Uri -OutFile $OutFile -Headers $headers -UseBasicParsing -MaximumRedirection 10

            if ((Test-Path $OutFile) -and ((Get-Item $OutFile).Length -gt 0)) {
                return
            }

            throw "Download completed but the output file is empty."
        } catch {
            if ($attempt -eq $maxAttempts) {
                throw "Failed to download GilzaBots after $maxAttempts attempts. Last error: $($_.Exception.Message)"
            }

            Write-Host "Download failed. Retrying ($attempt/$maxAttempts)..." -ForegroundColor Yellow
            Start-Sleep -Seconds (2 * $attempt)
        }
    }
}

if ((Test-Path $installDir) -and (Test-GilzaBotsRunning -Directory $installDir)) {
    throw "GilzaBots is running from $installDir. Close it and run the installer again."
}

$downloadUrl = Get-GilzaBotsDownloadUrl
Write-Host "Downloading GilzaBots from: $downloadUrl" -ForegroundColor Yellow
if ($script:downloadSizeMB) {
    Write-Host "Download size: about $($script:downloadSizeMB) MB. This can take a few minutes." -ForegroundColor Yellow
}

if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Force -Path $installDir | Out-Null
}

Invoke-DownloadWithRetry -Uri $downloadUrl -OutFile $tempZip

Write-Host "Extracting files to $installDir (this can take a minute)..." -ForegroundColor Yellow
& {
    $ProgressPreference = "SilentlyContinue"
    Expand-Archive -Path $tempZip -DestinationPath $installDir -Force
}

$exeDir = "$installDir\GilzaBots"
if (-not (Test-Path "$exeDir\GilzaBots.exe")) {
    $exeDir = $installDir
}

# The uninstaller is a convenience, not part of the application. A release that
# predates it, or a branch that does not carry it yet, must not abort an
# installation whose payload is already extracted: warn and continue. The
# Start-menu shortcut and the Installed-apps entry are only published when the
# script is really present, so Windows never offers an uninstall that cannot run.
# PyInstaller 6+ bundles data files in an `_internal` contents directory next to
# the executable; older releases kept them beside it. Resolve both layouts.
$uninstallerPath = @("$exeDir\uninstall.ps1", "$exeDir\_internal\uninstall.ps1") |
    Where-Object { Test-Path $_ } |
    Select-Object -First 1
if (-not $uninstallerPath) {
    $uninstallerPath = "$exeDir\uninstall.ps1"
    Write-Host "This release predates the bundled uninstaller; downloading it separately..." -ForegroundColor Yellow
    try {
        Invoke-DownloadWithRetry `
            -Uri "https://raw.githubusercontent.com/$repoOwner/$repoName/$uninstallerRef/uninstall.ps1" `
            -OutFile $uninstallerPath
    } catch {
        Remove-Item -Path $uninstallerPath -Force -ErrorAction SilentlyContinue
        Write-Warning "Could not fetch the GilzaBots uninstaller; continuing without it."
        Write-Warning "To remove GilzaBots later, delete $installDir and its PATH entry."
    }
}
$hasUninstaller = Test-Path $uninstallerPath
$appVersion = if ($script:releaseTag) { $script:releaseTag } else { "unknown" }

# Add the GilzaBots executable directory to the user PATH.
$pathAdded = $false
$userPath = [Environment]::GetEnvironmentVariable("Path", [EnvironmentVariableTarget]::User)
if ($userPath -notlike "*$exeDir*") {
    Write-Host "Adding GilzaBots to PATH..." -ForegroundColor Yellow
    $newPath = "$userPath;$exeDir"
    [Environment]::SetEnvironmentVariable("Path", $newPath, [EnvironmentVariableTarget]::User)
    $env:Path = "$env:Path;$exeDir"
    $pathAdded = $true
}

# Create Desktop and Start menu shortcuts.
Write-Host "Creating Desktop and Start menu shortcuts..." -ForegroundColor Yellow
$exePath  = "$exeDir\GilzaBots.exe"
$wshShell = New-Object -ComObject WScript.Shell

$desktopPath     = $wshShell.SpecialFolders("Desktop")
$desktopShortcut = $wshShell.CreateShortcut("$desktopPath\GilzaBots.lnk")
$desktopShortcut.TargetPath       = $exePath
$desktopShortcut.WorkingDirectory = $exeDir
$desktopShortcut.Description      = "GilzaBots: free, open-source AI agent for your projects"
$desktopShortcut.IconLocation     = "$exePath,0"
$desktopShortcut.Save()

$startMenuDir = $wshShell.SpecialFolders("Programs")
if (-not (Test-Path $startMenuDir)) {
    New-Item -ItemType Directory -Force -Path $startMenuDir | Out-Null
}
$startMenuShortcut = $wshShell.CreateShortcut("$startMenuDir\GilzaBots.lnk")
$startMenuShortcut.TargetPath       = $exePath
$startMenuShortcut.WorkingDirectory = $exeDir
$startMenuShortcut.Description      = "GilzaBots: free, open-source AI agent for your projects"
$startMenuShortcut.IconLocation     = "$exePath,0"
$startMenuShortcut.Save()

if ($hasUninstaller) {
    $uninstallShortcut = $wshShell.CreateShortcut("$startMenuDir\Uninstall GilzaBots.lnk")
    $uninstallShortcut.TargetPath = "powershell.exe"
    $uninstallShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$uninstallerPath`""
    $uninstallShortcut.WorkingDirectory = $exeDir
    $uninstallShortcut.Description = "Uninstall GilzaBots"
    $uninstallShortcut.Save()

    # Register a per-user uninstall entry so GilzaBots appears in Windows Settings >
    # Apps > Installed apps without requiring administrator privileges.
    $uninstallRegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\GilzaBots"
    if (-not (Test-Path $uninstallRegistryPath)) {
        New-Item -Path $uninstallRegistryPath -Force | Out-Null
    }
    $uninstallCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstallerPath`""
    Set-ItemProperty -Path $uninstallRegistryPath -Name "DisplayName" -Value "GilzaBots"
    Set-ItemProperty -Path $uninstallRegistryPath -Name "DisplayVersion" -Value $appVersion
    Set-ItemProperty -Path $uninstallRegistryPath -Name "Publisher" -Value "Gilzamir Gomes"
    Set-ItemProperty -Path $uninstallRegistryPath -Name "DisplayIcon" -Value $exePath
    Set-ItemProperty -Path $uninstallRegistryPath -Name "InstallLocation" -Value $installDir
    Set-ItemProperty -Path $uninstallRegistryPath -Name "UninstallString" -Value $uninstallCommand
    Set-ItemProperty -Path $uninstallRegistryPath -Name "PathAdded" -Type DWord -Value ([int]$pathAdded)
    Set-ItemProperty -Path $uninstallRegistryPath -Name "NoModify" -Type DWord -Value 1
    Set-ItemProperty -Path $uninstallRegistryPath -Name "NoRepair" -Type DWord -Value 1
}

Remove-Item -Path $tempZip -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  GilzaBots installed successfully!       " -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host "Terminal command: gilzabots" -ForegroundColor Cyan
Write-Host "Shortcuts were created on the Desktop and Start menu." -ForegroundColor Cyan
Write-Host "To uninstall, use Windows Settings > Apps > Installed apps, or the Start menu shortcut." -ForegroundColor Cyan
