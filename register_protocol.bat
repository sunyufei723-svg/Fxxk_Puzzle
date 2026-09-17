@echo off
chcp 65001 >nul
echo Registering fxxk-puzzle:// protocol handler...

:: Get the directory where this bat file is located
set "SCRIPT_DIR=%~dp0"
set "MAIN_EXE=%SCRIPT_DIR%Fxxk_Puzzle.exe"

:: Check if Fxxk_Puzzle.exe exists
if not exist "%MAIN_EXE%" (
    echo [ERROR] Fxxk_Puzzle.exe not found in: %SCRIPT_DIR%
    pause
    exit /b 1
)

:: Register protocol handler using PowerShell
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$regPath = 'HKCU:\Software\Classes\fxxk-puzzle';" ^
    "New-Item -Path $regPath -Force | Out-Null;" ^
    "Set-ItemProperty -Path $regPath -Name '(Default)' -Value 'URL:fxxk-puzzle';" ^
    "Set-ItemProperty -Path $regPath -Name 'URL Protocol' -Value '';" ^
    "$shellPath = Join-Path $regPath 'shell\open\command';" ^
    "New-Item -Path $shellPath -Force | Out-Null;" ^
    "$cmd = '\"%MAIN_EXE%\" \"%%1\"';" ^
    "Set-ItemProperty -Path $shellPath -Name '(Default)' -Value $cmd;"

if %errorlevel% neq 0 (
    echo [ERROR] Failed to register protocol handler
    pause
    exit /b 1
)

echo [OK] Protocol handler registered: fxxk-puzzle://
echo      Program: %MAIN_EXE%
pause
