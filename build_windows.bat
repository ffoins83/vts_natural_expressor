@echo off
setlocal
cd /d "%~dp0"

set "TOOLS_DIR=%~dp0.build_tools"
set "UV_EXE=%TOOLS_DIR%\uv.exe"
set "UV_ARCHIVE=%TOOLS_DIR%\uv.zip"
set "UV_EXTRACT=%TOOLS_DIR%\uv-extract"
set "UV_CACHE_DIR=%TOOLS_DIR%\cache"
set "UV_PYTHON_INSTALL_DIR=%TOOLS_DIR%\python"
set "VENV_DIR=%TOOLS_DIR%\venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "BUILD_DIR=%TOOLS_DIR%\build"
set "SPEC_DIR=%TOOLS_DIR%\spec"

if not exist "%TOOLS_DIR%" mkdir "%TOOLS_DIR%"

if not exist "%UV_EXE%" call :install_uv
if errorlevel 1 goto :failed

if not exist "%PYTHON_EXE%" (
  "%UV_EXE%" venv --python 3.11 --managed-python "%VENV_DIR%"
  if errorlevel 1 goto :failed
)

"%UV_EXE%" pip install --python "%PYTHON_EXE%" -r requirements.txt pyinstaller
if errorlevel 1 goto :failed

"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean --onefile --windowed --distpath . --workpath "%BUILD_DIR%" --specpath "%SPEC_DIR%" --name "VTS Natural Expressor" vts_natural_expressor.py
if errorlevel 1 goto :failed

if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%SPEC_DIR%" rmdir /s /q "%SPEC_DIR%"

echo.
echo Build complete: %CD%\VTS Natural Expressor.exe
pause
exit /b 0

:install_uv
set "UV_TARGET=x86_64-pc-windows-msvc"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "UV_TARGET=aarch64-pc-windows-msvc"
set "UV_DOWNLOAD_URL=https://github.com/astral-sh/uv/releases/latest/download/uv-%UV_TARGET%.zip"

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri $env:UV_DOWNLOAD_URL -OutFile $env:UV_ARCHIVE; Expand-Archive -LiteralPath $env:UV_ARCHIVE -DestinationPath $env:UV_EXTRACT -Force; $uv = Get-ChildItem -Path $env:UV_EXTRACT -Filter uv.exe -Recurse | Select-Object -First 1; if (-not $uv) { throw 'uv.exe was not found in the downloaded archive.' }; Copy-Item -LiteralPath $uv.FullName -Destination $env:UV_EXE -Force; Remove-Item -LiteralPath $env:UV_ARCHIVE -Force; Remove-Item -LiteralPath $env:UV_EXTRACT -Recurse -Force"
if errorlevel 1 exit /b 1
exit /b 0

:failed
echo.
echo Build failed. Check the error output above.
pause
exit /b 1
