@echo off
REM Batch script to create venv if not exists using specific python, install requirements, and run the Python GUI

REM Set the venv directory name
set VENV_DIR=venv

REM Set the parent python interpreter
set PARENT_PYTHON="C:\Users\jonp6.conda\envs\img_gen\python.exe"

REM Check if venv already exists
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Creating virtual environment in %VENV_DIR% using Python at %PARENT_PYTHON%
    %PARENT_PYTHON% -m venv %VENV_DIR%
    if %errorlevel% neq 0 (
        echo Failed to create venv. Check if the python path is correct.
        exit /b 1
    )
    echo Virtual environment created in %VENV_DIR%
)

REM Activate the venv
call "%VENV_DIR%\Scripts\activate.bat"

REM Install requirements (PySide6)
pip install --upgrade pip
pip install PySide6

REM Run the Python GUI program
python drive_text_searcher.py

REM Deactivate the venv (optional)
REM deactivate