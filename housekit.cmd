@echo off
setlocal
rem One-word launcher: "housekit build ..." from anywhere in the project.
rem Makes sure ffmpeg is on PATH (winget puts it on the *user* PATH, which a stale shell may not see).
where ffmpeg >nul 2>nul
if errorlevel 1 (
  for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*") do (
    for /d %%B in ("%%D\ffmpeg-*") do set "PATH=%%B\bin;%PATH%"
  )
)
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0housekit\.venv\Scripts\housekit.exe" (
  echo housekit is not installed yet. From the housekit folder run: python -m venv .venv ^&^& .venv\Scripts\pip install -e .
  exit /b 2
)
pushd "%~dp0housekit"
"%~dp0housekit\.venv\Scripts\housekit.exe" %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
