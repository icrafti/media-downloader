@echo off
rem Launch the YouTube/media downloader GUI without a console window
set "SCRIPT=%~dp0yt_downloader.py"

where pythonw >nul 2>nul
if errorlevel 1 (
  echo [ERROR] pythonw not found. Install Python from python.org with "tcl/tk" checked.
  pause
  exit /b 1
)

start "" pythonw "%SCRIPT%"
