@echo off
setlocal
cd /d "%~dp0"

set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo Creating the project virtual environment...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv "%~dp0.venv"
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Python 3.10 or newer was not found. Install Python and try again.
            goto failed
        )
        python -m venv "%~dp0.venv"
    )
    if errorlevel 1 goto failed
)

"%VENV_PYTHON%" -c "import streamlit, cv2, numpy, PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing project dependencies...
    "%VENV_PYTHON%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 goto failed
)

echo Starting the document scanner...
"%VENV_PYTHON%" -m streamlit run "%~dp0app.py" %*
if errorlevel 1 goto failed
goto finished

:failed
echo.
echo Startup failed. Check the message above and try again.

:finished
echo.
pause
endlocal
