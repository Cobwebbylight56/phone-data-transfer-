@echo off
setlocal enabledelayedexpansion
title Phone Data Transfer - installer

REM Double-click this file to install the app from source.
REM It puts everything in a .venv folder here, so nothing is scattered around
REM your system and deleting this folder removes it completely.

echo.
echo  Phone Data Transfer - installer
echo  ==============================
echo.

cd /d "%~dp0"

REM --- find Python ---------------------------------------------------------
set PY=
where py >nul 2>&1 && set PY=py -3
if "%PY%"=="" (
    where python >nul 2>&1 && set PY=python
)

if "%PY%"=="" (
    echo  Python is not installed, and this installer needs it.
    echo.
    echo  Two ways to get it:
    echo    1. Run this in PowerShell:   winget install Python.Python.3.12
    echo    2. Or download it from       https://www.python.org/downloads/
    echo       ^(tick "Add python.exe to PATH" on the first screen^)
    echo.
    echo  Then run this installer again.
    echo.
    echo  If you would rather not install Python at all, download the
    echo  prebuilt PhoneDataTransfer.exe instead - see README.md.
    echo.
    pause
    exit /b 1
)

echo  Using Python: %PY%
%PY% --version
echo.

REM --- virtual environment -------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo  Creating a private Python environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo  Could not create the environment. Is Python installed correctly?
        pause
        exit /b 1
    )
)

set VENV_PY=.venv\Scripts\python.exe

echo  Installing the app and its dependencies ^(this takes a minute^) ...
"%VENV_PY%" -m pip install --upgrade pip --quiet
"%VENV_PY%" -m pip install -e ".[gui]" --quiet
if errorlevel 1 (
    echo.
    echo  Installation failed. Scroll up for the reason.
    echo  A common cause is no internet connection.
    pause
    exit /b 1
)

REM --- adb and fastboot ----------------------------------------------------
echo.
echo  Fetching adb and fastboot from Google ^(about 15 MB^) ...
"%VENV_PY%" -c "from ptransfer.platform_tools import download_platform_tools; print('  installed to', download_platform_tools())"
if errorlevel 1 (
    echo  Could not download them now - the app will offer again on first run.
)

REM --- launchers -----------------------------------------------------------
echo.
echo  Creating launchers ...

> "Phone Data Transfer.bat" echo @echo off
>> "Phone Data Transfer.bat" echo cd /d "%%~dp0"
>> "Phone Data Transfer.bat" echo start "" ".venv\Scripts\pythonw.exe" -m ptransfer_gui.app

REM pythonw shows no window at all, so a GUI that fails to start looks like
REM nothing happening. This one keeps the console and the error message.
> "Phone Data Transfer (show errors).bat" echo @echo off
>> "Phone Data Transfer (show errors).bat" echo cd /d "%%~dp0"
>> "Phone Data Transfer (show errors).bat" echo ".venv\Scripts\python.exe" -m ptransfer_gui.app
>> "Phone Data Transfer (show errors).bat" echo echo.
>> "Phone Data Transfer (show errors).bat" echo pause

> "ptransfer.bat" echo @echo off
>> "ptransfer.bat" echo cd /d "%%~dp0"
>> "ptransfer.bat" echo ".venv\Scripts\python.exe" -m ptransfer.cli %%*

REM Confirm the window will actually open, rather than finding out later.
"%VENV_PY%" -c "import PySide6" 2>nul
if errorlevel 1 (
    echo.
    echo  NOTE: PySide6 did not install, so the window will not open.
    echo  This usually means your Python version is too new for it yet.
    echo  Everything still works from the command line:
    echo      ptransfer.bat devices
    echo      ptransfer.bat nokia --fix-bootloop
    echo.
)

REM Build the shortcut from a real script file rather than a multi-line
REM inline command - the line continuations were fragile, and hiding the
REM output meant a failure looked like success.
echo.
echo  Creating a Desktop shortcut ...
set "SHORTCUT_PS=%TEMP%\ptransfer-shortcut.ps1"
> "%SHORTCUT_PS%" echo $ErrorActionPreference = 'Stop'
>> "%SHORTCUT_PS%" echo $desktop = [Environment]::GetFolderPath('Desktop')
>> "%SHORTCUT_PS%" echo if (-not $desktop) { $desktop = Join-Path $env:USERPROFILE 'Desktop' }
>> "%SHORTCUT_PS%" echo $linkPath = Join-Path $desktop 'Phone Data Transfer.lnk'
>> "%SHORTCUT_PS%" echo $shell = New-Object -ComObject WScript.Shell
>> "%SHORTCUT_PS%" echo $link = $shell.CreateShortcut($linkPath)
>> "%SHORTCUT_PS%" echo $link.TargetPath = '%CD%\Phone Data Transfer.bat'
>> "%SHORTCUT_PS%" echo $link.WorkingDirectory = '%CD%'
>> "%SHORTCUT_PS%" echo $link.Description = 'Copy data off a phone, and rescue one that will not boot'
>> "%SHORTCUT_PS%" echo $link.Save()
>> "%SHORTCUT_PS%" echo Write-Host ('  created: ' + $linkPath)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SHORTCUT_PS%"
if errorlevel 1 (
    echo.
    echo  Could not create the Desktop shortcut - everything else is fine.
    echo  Start the app with "Phone Data Transfer.bat" in this folder, or
    echo  right-click it and choose Send to ^> Desktop ^(create shortcut^).
)
del "%SHORTCUT_PS%" >nul 2>&1

echo.
echo  ==============================================================
echo   Done.
echo.
echo   Everything is installed in this folder:
echo     %CD%
echo.
echo   adb and fastboot are in:
echo     %LOCALAPPDATA%\PhoneDataTransfer\platform-tools
echo.
echo   To get back to this folder later, right-click the Desktop
echo   shortcut and choose "Open file location".
echo.
echo   Start the app:      double-click "Phone Data Transfer.bat"
echo                       (or the shortcut now on your Desktop)
echo.
echo   Command line:       ptransfer.bat devices
echo                       ptransfer.bat nokia --fix-bootloop
echo.
echo   Read docs\soft-brick-rescue.md before touching a phone that
echo   will not start.
echo  ==============================================================
echo.
pause
