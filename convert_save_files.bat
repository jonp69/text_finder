@echo off
REM Batch script to run the save file converter

echo Converting legacy save files to per-drive format...
echo.

python convert_save_files.py

echo.
pause
