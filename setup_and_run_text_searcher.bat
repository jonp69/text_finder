@echo off
REM Batch script to create venv if not exists, install requirements, and run the Python GUI

REM Set the venv directory name
set VENV_DIR=venv

REM Try to find Python automatically
where python >nul 2>&1
if %errorlevel% equ 0 (
    set PARENT_PYTHON=python
) else (
    echo Python not found in PATH. Please install Python or add it to your PATH.
    echo You can also manually edit this script to specify the Python path.
    pause
    exit /b 1
)

REM Check if venv already exists
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Creating virtual environment in %VENV_DIR%
    %PARENT_PYTHON% -m venv %VENV_DIR%
    if %errorlevel% neq 0 (
        echo Failed to create venv. Make sure Python is installed properly.
        pause
        exit /b 1
    )
    echo Virtual environment created in %VENV_DIR%
)

REM Activate the venv
call "%VENV_DIR%\Scripts\activate.bat"

REM Install requirements from requirements.txt
pip install --upgrade pip
if exist requirements.txt (
    echo Installing requirements from requirements.txt
    pip install -r requirements.txt
) else (
    echo requirements.txt not found, installing PySide6 directly
    pip install PySide6
)

REM Run the Python GUI program
python drive_text_searcher.py

REM Deactivate the venv (optional)
REM deactivate