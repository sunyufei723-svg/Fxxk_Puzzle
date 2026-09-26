@echo off
setlocal
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
  echo Visual Studio locator not found. Install Visual Studio C++ Build Tools. 1>&2
  exit /b 1
)
for /f "usebackq tokens=*" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSROOT=%%I"
if not defined VSROOT (
  echo Visual Studio C++ Build Tools not found. 1>&2
  exit /b 1
)
set "VSDEVCMD=%VSROOT%\Common7\Tools\VsDevCmd.bat"
if not exist "%VSDEVCMD%" (
  echo Visual Studio C++ Build Tools not found. 1>&2
  exit /b 1
)
call "%VSDEVCMD%" -arch=x64 -host_arch=x64 >nul || exit /b 1
if not exist "build" mkdir "build"
rc.exe /nologo /fo "build\resource.res" resource.rc || exit /b 1
cl.exe /nologo /std:c++20 /O2 /MT /EHsc /utf-8 /W4 /DUNICODE /D_UNICODE ^
  /Fo:"build\\" /Fe:"build\Setup.exe" setup.cpp "build\resource.res" /link /SUBSYSTEM:WINDOWS || exit /b 1
cl.exe /nologo /std:c++20 /O2 /MT /EHsc /utf-8 /W4 /DUNICODE /D_UNICODE ^
  /Fo:"build\\" /Fe:"build\Uninstall.exe" uninstall.cpp "build\resource.res" /link /SUBSYSTEM:WINDOWS || exit /b 1
cl.exe /nologo /std:c++20 /O2 /MT /EHsc /utf-8 /W4 /DUNICODE /D_UNICODE ^
  /Fo:"build\\" /Fe:"build\UiaProbe.exe" uia_probe.cpp /link /SUBSYSTEM:CONSOLE || exit /b 1
endlocal
