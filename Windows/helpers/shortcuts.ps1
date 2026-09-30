# Creates or removes the RadarForge Start Menu and Desktop shortcuts.
# Called by install.bat / uninstall.bat.
param(
    [ValidateSet("create", "remove")] [string] $Action = "create",
    [string] $Target = "",
    [string] $Icon = "",
    [string] $WorkDir = ""
)
$ErrorActionPreference = "Stop"
# Start Menu -> Programs, and the Desktop (works with OneDrive-redirected desktops too)
$folders = @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop")) |
    Where-Object { $_ -and (Test-Path $_) }

if ($Action -eq "remove") {
    foreach ($f in $folders) {
        $lnk = Join-Path $f "RadarForge.lnk"
        if (Test-Path $lnk) { Remove-Item $lnk -Force }
    }
    exit 0
}

if (-not (Test-Path $Target)) { Write-Host "    Program not found: $Target"; exit 1 }
$shell = New-Object -ComObject WScript.Shell
$made = 0
foreach ($f in $folders) {
    try {
        $s = $shell.CreateShortcut((Join-Path $f "RadarForge.lnk"))
        $s.TargetPath = $Target
        $s.Arguments = "-m radarforge"
        $s.WorkingDirectory = $WorkDir
        $s.IconLocation = "$Icon,0"
        $s.Description = "RadarForge - NEXRAD weather radar viewer"
        $s.Save()
        $made++
    } catch {
        Write-Host "    Could not create a shortcut in ${f}: $($_.Exception.Message)"
    }
}
if ($made -eq 0) { exit 1 }
Write-Host "    Shortcuts added to the Start Menu and the Desktop."
