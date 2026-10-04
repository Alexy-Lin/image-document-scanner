@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found. Install uv, then run this script again.
    goto failed
)

set "BUILD_ENV=%TEMP%\ImageDocumentScanner-build312"
set "BUILD_PYTHON=%BUILD_ENV%\Scripts\python.exe"

if not exist "%BUILD_PYTHON%" (
    echo Creating the Python 3.12 build environment...
    uv venv --python 3.12.13 "%BUILD_ENV"
    if errorlevel 1 goto failed
)

echo Installing the compatibility build tools...
uv pip install --python "%BUILD_PYTHON%" -r "%~dp0requirements.txt" -r "%~dp0requirements-build-compat.txt"
if errorlevel 1 goto failed

echo Building the compatibility standalone application...
"%BUILD_PYTHON%" -m PyInstaller --noconfirm --clean --onefile --windowed --name ImageDocumentScanner-compat --distpath "%~dp0dist\compat" --workpath "%~dp0build\compat" "%~dp0app.py"
if errorlevel 1 goto failed

echo.
echo Build complete: %~dp0dist\compat\ImageDocumentScanner-compat.exe
endlocal
exit /b 0

:failed
echo.
echo Build failed. Check the message above.
pause
endlocal
exit /b 1
