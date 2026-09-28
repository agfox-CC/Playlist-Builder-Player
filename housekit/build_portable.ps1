# Builds / updates the portable HouseKit folder (Windows).
#   .\build_portable.ps1 [-Out "..\HouseKit-Portable"] [-IncludeState] [-ForceState] [-Layout python|exe]
#
# Default layout "python": a copy of the (signed) python.org interpreter, the tool as source, and
# .cmd / .lnk launchers. Nothing in it is an unknown executable, so Windows Smart App Control
# leaves it alone. Layout "exe" is the older PyInstaller build (HouseKit.exe); Smart App Control
# blocks unsigned freshly built exes, so only use it on machines without it.
#
# Updates IN PLACE: only the program parts (python\, housekit\, launchers, ffmpeg\, README.md, and any
# old _internal\ / HouseKit.exe / housekit-cli.exe) are replaced. The folder's own data (housekit.toml,
# library.db, charts\, setlists\, pins, gigs\, playlists\) is never deleted. -IncludeState copies this
# source install's catalog/config/charts/setlists in only if the folder has no library.db yet;
# -ForceState overwrites them. Chart CSVs missing from the folder's charts\ are always copied in
# (they are downloaded data the app then uses offline), never overwritten.
param(
    [string]$Out = "..\HouseKit-Portable",
    [switch]$IncludeState,
    [switch]$ForceState,
    [ValidateSet("python", "exe")][string]$Layout = "python"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")

# --- safety: never touch the project folder, only ever replace program parts ---
if ([System.IO.Path]::IsPathRooted($Out)) { $outFull = [System.IO.Path]::GetFullPath($Out) } else { $outFull = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Out)) }
$projFull = [System.IO.Path]::GetFullPath($PSScriptRoot)
if ($outFull.TrimEnd('\').ToLower() -eq $projFull.TrimEnd('\').ToLower()) { throw "Output folder is the project folder itself ($outFull). Choose another -Out." }
if ($projFull.ToLower().StartsWith($outFull.TrimEnd('\').ToLower() + "\")) { throw "Output folder contains the project folder ($outFull). Choose another -Out." }
if ((Test-Path $outFull) -and (Test-Path (Join-Path $outFull "pyproject.toml"))) { throw "$outFull looks like a source checkout. Refusing." }
$running = Get-Process | Where-Object { $_.Path -and $_.Path.ToLower().StartsWith($outFull.TrimEnd('\').ToLower() + "\") }
if ($running) { throw "Something is running from $outFull ($($running.ProcessName -join ', '), PID $($running.Id -join ',')). Close it and run again." }

$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ff) { $ffdir = Split-Path $ff.Source } else { Write-Warning "ffmpeg not found on PATH; the portable build will not include it." }

New-Item -ItemType Directory -Force $outFull | Out-Null
# Remove program parts from either layout; leave data alone.
foreach ($part in "_internal","ffmpeg","python","housekit") { $p = Join-Path $outFull $part; if (Test-Path $p) { Remove-Item -Recurse -Force $p } }
foreach ($part in "HouseKit.exe","housekit-cli.exe","HouseKit.cmd","housekit-cli.cmd","HouseKit.lnk","HouseKit Player.cmd","HouseKit Player.lnk","README.md") { $p = Join-Path $outFull $part; if (Test-Path $p) { Remove-Item -Force $p } }

if ($Layout -eq "exe") {
    if (-not (Test-Path ".venv\Scripts\pyinstaller.exe")) { .\.venv\Scripts\pip install pyinstaller | Out-Null }
    if ($ffdir) { $env:HOUSEKIT_FFMPEG_DIR = $ffdir }
    .\.venv\Scripts\pyinstaller.exe housekit.spec --noconfirm --clean --distpath build\dist --workpath build\work
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    Copy-Item "build\dist\HouseKit\_internal" "$outFull\_internal" -Recurse -Force
    Copy-Item "build\dist\HouseKit\HouseKit.exe","build\dist\HouseKit\housekit-cli.exe" $outFull -Force
} else {
    # 1. the interpreter: the python.org install the venv was made from (signed binaries)
    $pyHome = (Select-String -Path ".venv\pyvenv.cfg" -Pattern "^home\s*=\s*(.+)$").Matches[0].Groups[1].Value.Trim()
    if (-not (Test-Path "$pyHome\pythonw.exe")) { throw "Base Python not found at $pyHome" }
    $py = Join-Path $outFull "python"
    New-Item -ItemType Directory -Force $py | Out-Null
    foreach ($f in "python.exe","pythonw.exe","python3.dll","python312.dll","vcruntime140.dll","vcruntime140_1.dll","LICENSE.txt") {
        if (Test-Path "$pyHome\$f") { Copy-Item "$pyHome\$f" $py -Force }
    }
    Get-ChildItem "$pyHome" -Filter "python3*.dll" | Copy-Item -Destination $py -Force
    Copy-Item "$pyHome\DLLs" "$py\DLLs" -Recurse -Force
    Copy-Item "$pyHome\tcl" "$py\tcl" -Recurse -Force
    New-Item -ItemType Directory -Force "$py\Lib" | Out-Null
    # standard library without the test suite and docs
    robocopy "$pyHome\Lib" "$py\Lib" /E /XD test tests idlelib turtledemo __pycache__ site-packages /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed copying the standard library" }
    New-Item -ItemType Directory -Force "$py\Lib\site-packages" | Out-Null
    # pip from the base install so the portable python can install the dependencies
    robocopy "$pyHome\Lib\site-packages" "$py\Lib\site-packages" /E /XD __pycache__ /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
    # 2. dependencies, straight from pyproject (network needed the first time; pip caches after that)
    & "$py\python.exe" -m pip install --quiet --no-warn-script-location --upgrade pip
    & "$py\python.exe" -m pip install --quiet --no-warn-script-location `
        "mutagen==1.47.0" "numpy>=1.26,<3" "pyloudnorm==0.1.1" "rapidfuzz>=3.9,<4" "pdfplumber>=0.11,<0.12" "typer>=0.12,<1" "rich>=13.7,<15" "tomli-w>=1.0,<2" "sounddevice>=0.5,<1" "sv-ttk>=2.6,<3" "tkinterdnd2>=0.4,<1"
    if ($LASTEXITCODE -ne 0) { throw "pip install into the portable python failed" }
    # 3. the tool itself, as source
    robocopy "housekit" "$outFull\housekit" /E /XD __pycache__ /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
    # 4. launchers
    @'
@echo off
rem HouseKit window. Uses the folder's own (signed) Python; nothing here needs installing.
cd /d "%~dp0"
start "" "%~dp0python\pythonw.exe" -m housekit.app
'@ | Set-Content -Path "$outFull\HouseKit.cmd" -Encoding ASCII
    @'
@echo off
rem HouseKit command line. Example: housekit-cli build --out "2026-10-03 The Bar"
cd /d "%~dp0"
"%~dp0python\python.exe" -m housekit.cli_main %*
'@ | Set-Content -Path "$outFull\housekit-cli.cmd" -Encoding ASCII
    @'
@echo off
rem HouseKit Player: plays a gig or playlist folder cue-list style. Drop a folder on this file or open one inside.
cd /d "%~dp0"
start "" "%~dp0python\pythonw.exe" -m housekit.player_main %*
'@ | Set-Content -Path "$outFull\HouseKit Player.cmd" -Encoding ASCII
    try {
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut("$outFull\HouseKit.lnk")
        $lnk.TargetPath = "$outFull\python\pythonw.exe"
        $lnk.Arguments = "-m housekit.app"
        $lnk.WorkingDirectory = $outFull
        $lnk.Description = "HouseKit - venue house music builder"
        $lnk.Save()
        $lnk2 = $ws.CreateShortcut("$outFull\HouseKit Player.lnk")
        $lnk2.TargetPath = "$outFull\python\pythonw.exe"
        $lnk2.Arguments = "-m housekit.player_main"
        $lnk2.WorkingDirectory = $outFull
        $lnk2.Description = "HouseKit Player - cue-list playback for gig and playlist folders"
        $lnk2.Save()
    } catch { Write-Warning "Could not create the shortcuts: $($_.Exception.Message)" }
}

if ($ffdir) {
    New-Item -ItemType Directory -Force "$outFull\ffmpeg" | Out-Null
    Copy-Item "$ffdir\ffmpeg.exe","$ffdir\ffprobe.exe" "$outFull\ffmpeg\" -Force
}
foreach ($d in "setlists","gigs","playlists","charts") { New-Item -ItemType Directory -Force "$outFull\$d" | Out-Null }
Copy-Item README.md "$outFull\README.md" -Force
# chart data files the folder doesn't have yet (so it works offline out of the box); existing ones are left alone
Get-ChildItem charts -Filter *.csv -File -ErrorAction SilentlyContinue | ForEach-Object {
    $dst = Join-Path "$outFull\charts" $_.Name
    if (-not (Test-Path $dst)) { Copy-Item $_.FullName $dst }
}

$hasState = Test-Path (Join-Path $outFull "library.db")
if ($IncludeState -and (-not $hasState -or $ForceState)) {
    foreach ($f in "housekit.toml","library.db","setlist_overrides.toml") { if (Test-Path $f) { Copy-Item $f $outFull -Force } }
    Get-ChildItem charts -Filter *.csv -File -ErrorAction SilentlyContinue | Copy-Item -Destination "$outFull\charts\" -Force
    Get-ChildItem setlists -File -ErrorAction SilentlyContinue | Copy-Item -Destination "$outFull\setlists\" -Force
    Write-Host "Copied this install's catalog, config, charts and setlists into the folder."
} elseif ($IncludeState -and $hasState) {
    Write-Host "Folder already has its own catalog; left its data alone (use -ForceState to overwrite)."
}

$size = (Get-ChildItem $outFull -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Portable folder ready ({0} layout): {1}  ({2:N0} MB)" -f $Layout, $outFull, $size)
if ($Layout -eq "python") { Write-Host "Double-click HouseKit.lnk (or HouseKit.cmd) for the window; housekit-cli.cmd for the command line." }
else { Write-Host "Run HouseKit.exe for the window, housekit-cli.exe for the command line." }
