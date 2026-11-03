# GilzaBots Community uninstaller for direct Windows installations.
# Application files are removed by default. User data is removed only when
# -Purge is explicitly supplied.
param(
    [switch]$Purge,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$installDir = Join-Path $env:LOCALAPPDATA "GilzaBots"
$pointerFile = Join-Path $HOME ".gilzabotshome"
$registryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\GilzaBots"
$purgeDataDir = $null

# Run from "Installed apps" or the Start menu, this script owns its console
# window, which closes the moment the script ends. A failure that only throws
# therefore vanishes before it can be read, and the user is left with an
# installation that "will not uninstall" and no reason why. Every failure is
# written to a log and, unless -Yes asked for an unattended run, kept on screen
# until the user dismisses it.
$logPath = Join-Path $env:TEMP "GilzaBots-uninstall.log"
function Stop-Uninstall {
    param([Parameter(Mandatory = $true)][string]$Message)

    try {
        "[$(Get-Date -Format o)] $Message" | Out-File -FilePath $logPath -Append -Encoding utf8
    } catch {
    }
    Write-Host ""
    Write-Host $Message -ForegroundColor Red
    Write-Host "Details were saved to $logPath" -ForegroundColor Yellow
    if (-not $Yes) {
        # Read-Host throws in a -NonInteractive session; that must not
        # re-enter the trap below.
        try {
            Read-Host "Press Enter to close this window" | Out-Null
        } catch {
        }
    }
    exit 1
}

# Anything unexpected (a terminating error under ErrorActionPreference=Stop)
# is reported the same way instead of closing the window.
trap {
    Stop-Uninstall "The uninstaller failed: $($_.Exception.Message)"
}

# Resolve and confirm the destructive target before changing the installation,
# so a rejected or unsafe purge cannot leave a half-uninstalled application.
if ($Purge) {
    $dataDir = $env:GILZABOTS_HOME
    if (-not $dataDir -and (Test-Path $pointerFile)) {
        $dataDir = (Get-Content -Raw $pointerFile).Trim()
    }
    if (-not $dataDir) {
        $dataDir = Join-Path $HOME ".gilzabots"
    }

    if (-not [IO.Path]::IsPathRooted($dataDir)) {
        Stop-Uninstall "Refusing to purge a relative data directory: $dataDir"
    }

    $purgeDataDir = [IO.Path]::GetFullPath($dataDir).TrimEnd('\')
    $unsafePaths = @(
        [IO.Path]::GetPathRoot($purgeDataDir).TrimEnd('\'),
        [IO.Path]::GetFullPath($HOME).TrimEnd('\')
    )
    if ($unsafePaths | Where-Object { $_ -ieq $purgeDataDir }) {
        Stop-Uninstall "Refusing to purge unsafe data directory: $purgeDataDir"
    }

    if (-not $Yes) {
        $confirmation = Read-Host "Remove all GilzaBots global data at '$purgeDataDir'? Type 'remove' to continue"
        if ($confirmation -cne "remove") {
            Write-Host "Global data will be preserved."
            $Purge = $false
            $purgeDataDir = $null
        }
    }
}

# The monitor daemon (`GilzaBots.exe monitors daemon`) and the background turns it
# starts have no window to close, so they are stopped here rather than reported
# as "still running". Its login entry and notification registrations go too.
Get-CimInstance Win32_Process -Filter "Name = 'GilzaBots.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match '\smonitors\s' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "GilzaBotsMonitors" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "HKCU:\Software\Classes\gilzabots-monitor" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "HKCU:\Software\Classes\AppUserModelId\io.github.gilzamir18.GilzaBots" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "HKCU:\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\io.github.gilzamir18.GilzaBots" -ErrorAction SilentlyContinue

if (Get-Process -Name "GilzaBots" -ErrorAction SilentlyContinue) {
    Stop-Uninstall "GilzaBots is still running. Close it and run the uninstaller again."
}

# The executables the application starts from its own directory (the embedded
# browser's QtWebEngineProcess, tectonic, pandoc, winpty-agent) keep their files
# locked for as long as they run, and one can outlive the window. Deleting the
# directory under them fails partway through.
$installPrefix = $installDir.TrimEnd('\') + '\'
$lockingProcesses = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and $_.Path.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)
})
if ($lockingProcesses.Count -gt 0) {
    $names = ($lockingProcesses | ForEach-Object { "$($_.ProcessName) (PID $($_.Id))" }) -join ", "
    Stop-Uninstall "Programs started from the GilzaBots folder are still running: $names. Close them (or sign out and back in) and run the uninstaller again."
}

# A Start-menu shortcut starts PowerShell in the application directory. Windows
# cannot remove a process's current directory, so leave it before deleting files.
# Set-Location only moves PowerShell's own location; the process's working
# directory, which is what holds the folder, is a separate setting.
Set-Location $env:TEMP
[Environment]::CurrentDirectory = $env:TEMP

Write-Host "Uninstalling the direct GilzaBots installation..." -ForegroundColor Cyan

# Application files go first. If they cannot all be removed, the uninstaller
# stops here with PATH, shortcuts and the Installed-apps entry untouched, so the
# installation stays visible and can be uninstalled again once the cause is
# fixed, and no global data has been purged for an uninstall that did not
# happen. A file briefly held by an antivirus scan is retried.
if (Test-Path $installDir) {
    $removeError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Remove-Item -Recurse -Force $installDir
            $removeError = $null
            break
        } catch {
            $removeError = $_
            Start-Sleep -Seconds 2
        }
    }
    if ($removeError -and (Test-Path $installDir)) {
        Stop-Uninstall "Could not remove $installDir ($($removeError.Exception.Message)). Close any program using files in that folder and run the uninstaller again, or delete the folder manually."
    }
}

# Remove only exact PATH entries owned by the current installer. A substring
# replacement could corrupt an unrelated directory with a similar name.
$pathWasAdded = $false
if (Test-Path $registryPath) {
    $pathWasAdded = [bool](Get-ItemPropertyValue -Path $registryPath -Name "PathAdded" -ErrorAction SilentlyContinue)
}
$userPath = [Environment]::GetEnvironmentVariable("Path", [EnvironmentVariableTarget]::User)
if ($pathWasAdded -and $null -ne $userPath) {
    $knownEntries = @($installDir, (Join-Path $installDir "GilzaBots")) |
        ForEach-Object { $_.TrimEnd('\') }
    $keptEntries = @($userPath -split ';') | Where-Object {
        $entry = $_.Trim().TrimEnd('\')
        $entry -and -not ($knownEntries | Where-Object { $_ -ieq $entry })
    }
    [Environment]::SetEnvironmentVariable(
        "Path",
        ($keptEntries -join ';'),
        [EnvironmentVariableTarget]::User
    )
}

$wshShell = New-Object -ComObject WScript.Shell
$shortcutPaths = @(
    (Join-Path $wshShell.SpecialFolders("Desktop") "GilzaBots.lnk"),
    (Join-Path $wshShell.SpecialFolders("Programs") "GilzaBots.lnk"),
    (Join-Path $wshShell.SpecialFolders("Programs") "Uninstall GilzaBots.lnk")
)
foreach ($shortcutPath in $shortcutPaths) {
    if (Test-Path $shortcutPath) {
        $shortcut = $wshShell.CreateShortcut($shortcutPath)
        $target = $shortcut.TargetPath
        $arguments = $shortcut.Arguments
        $knownTargets = @(
            (Join-Path $installDir "GilzaBots.exe"),
            (Join-Path $installDir "GilzaBots\GilzaBots.exe")
        )
        # PyInstaller 6+ bundles this script under `_internal`; older releases
        # kept it beside the executable.
        $knownUninstallers = @(
            (Join-Path $installDir "uninstall.ps1"),
            (Join-Path $installDir "GilzaBots\uninstall.ps1"),
            (Join-Path $installDir "_internal\uninstall.ps1"),
            (Join-Path $installDir "GilzaBots\_internal\uninstall.ps1")
        )
        $isAppShortcut = [bool]($knownTargets | Where-Object { $_ -ieq $target })
        $isUninstallShortcut = [bool]($knownUninstallers | Where-Object {
            $arguments -and $arguments.IndexOf($_, [StringComparison]::OrdinalIgnoreCase) -ge 0
        })
        if ($isAppShortcut -or $isUninstallShortcut) {
            Remove-Item -Force $shortcutPath
        }
    }
}

if (Test-Path $registryPath) {
    Remove-Item -Recurse -Force $registryPath
}

if ($Purge) {
    if (Test-Path $purgeDataDir) {
        Remove-Item -Recurse -Force $purgeDataDir
    }
    Remove-Item -Force $pointerFile -ErrorAction SilentlyContinue
    Write-Host "Removed global data at $purgeDataDir. Project directories were preserved."
}

Write-Host "GilzaBots application files were removed." -ForegroundColor Green
if (-not $Purge) {
    Write-Host "Global settings, chats, caches, credentials, and project directories were preserved."
    # Say where, so a user who wanted them gone too does not have to hunt for
    # them. Informational only: a failure to resolve the path must not turn a
    # completed uninstall into a reported failure.
    try {
        $keptDataDir = $env:GILZABOTS_HOME
        if (-not $keptDataDir -and (Test-Path $pointerFile)) {
            $keptDataDir = (Get-Content -Raw $pointerFile).Trim()
        }
        if (-not $keptDataDir) {
            $keptDataDir = Join-Path $HOME ".gilzabots"
        }
        Write-Host "Global data is kept in $keptDataDir; delete that folder to remove it as well."
    } catch {
    }
}
