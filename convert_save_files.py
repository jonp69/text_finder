#!/usr/bin/env python3
"""
Convert legacy combined save files to new per-drive format.

This script converts:
- detected_text_files.json.progress -> detected_text_files.json.C.progress, detected_text_files.json.D.progress, etc.
- parsed_directories.json.progress -> parsed_directories.json.C.progress, parsed_directories.json.D.progress, etc.
"""

import os
import json
from pathlib import Path
from datetime import datetime

# Settings (should match main application)
RESULTS_FILE = "detected_text_files.json"
PARSED_DIRS_FILE = "parsed_directories.json"

def debug_log(message):
    """Print debug messages with timestamp"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] CONVERT: {message}")

def get_drive_from_path(filepath):
    """Extract drive from a file path (e.g., C:\\ from C:\\Users\\file.txt)"""
    if os.name == 'nt':
        return str(Path(filepath).parts[0]) if Path(filepath).parts else filepath[:3]
    return '/'

def convert_save_files():
    """Convert legacy combined save files to per-drive format"""
    debug_log("=== Save File Converter Starting ===")
    
    # Check if legacy files exist
    legacy_results_file = f"{RESULTS_FILE}.progress"
    legacy_dirs_file = f"{PARSED_DIRS_FILE}.progress"
    
    if not (os.path.exists(legacy_results_file) and os.path.exists(legacy_dirs_file)):
        debug_log("No legacy save files found to convert")
        debug_log(f"Looking for: {legacy_results_file} and {legacy_dirs_file}")
        return False
    
    debug_log(f"Found legacy files: {legacy_results_file}, {legacy_dirs_file}")
    
    try:
        # Load legacy data
        debug_log("Loading legacy detected files...")
        with open(legacy_results_file, 'r', encoding='utf-8') as f:
            all_detected_files = json.load(f)
        
        debug_log("Loading legacy parsed directories...")
        with open(legacy_dirs_file, 'r', encoding='utf-8') as f:
            all_parsed_dirs = json.load(f)
        
        debug_log(f"Loaded {len(all_detected_files)} files and {len(all_parsed_dirs)} directories")
        
        # Group by drive
        files_by_drive = {}
        dirs_by_drive = {}
        
        debug_log("Grouping detected files by drive...")
        for filepath in all_detected_files:
            drive = get_drive_from_path(filepath)
            if drive not in files_by_drive:
                files_by_drive[drive] = []
            files_by_drive[drive].append(filepath)
        
        debug_log("Grouping parsed directories by drive...")
        for dirpath in all_parsed_dirs:
            drive = get_drive_from_path(dirpath)
            if drive not in dirs_by_drive:
                dirs_by_drive[drive] = []
            dirs_by_drive[drive].append(dirpath)
        
        # Get all drives involved
        all_drives = set(files_by_drive.keys()) | set(dirs_by_drive.keys())
        debug_log(f"Found data for drives: {sorted(all_drives)}")
        
        # Save per-drive files
        for drive in all_drives:
            drive_safe = drive.replace(':', '').replace('\\', '')
            
            # Save detected files for this drive
            drive_files = files_by_drive.get(drive, [])
            results_file = f"{RESULTS_FILE}.{drive_safe}.progress"
            with open(results_file, 'w', encoding='utf-8') as f:
                json.dump(drive_files, f, indent=2)
            debug_log(f"Saved {len(drive_files)} files for drive {drive} to {results_file}")
            
            # Save parsed directories for this drive
            drive_dirs = dirs_by_drive.get(drive, [])
            dirs_file = f"{PARSED_DIRS_FILE}.{drive_safe}.progress"
            with open(dirs_file, 'w', encoding='utf-8') as f:
                json.dump(drive_dirs, f, indent=2)
            debug_log(f"Saved {len(drive_dirs)} directories for drive {drive} to {dirs_file}")
        
        # Create backup of original files
        backup_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_results = f"{legacy_results_file}.backup_{backup_suffix}"
        backup_dirs = f"{legacy_dirs_file}.backup_{backup_suffix}"
        
        debug_log(f"Creating backup: {backup_results}")
        os.rename(legacy_results_file, backup_results)
        
        debug_log(f"Creating backup: {backup_dirs}")
        os.rename(legacy_dirs_file, backup_dirs)
        
        debug_log("=== Conversion Complete ===")
        debug_log(f"Converted {len(all_detected_files)} files and {len(all_parsed_dirs)} directories")
        debug_log(f"Created per-drive files for {len(all_drives)} drives: {sorted(all_drives)}")
        debug_log(f"Original files backed up with suffix: backup_{backup_suffix}")
        return True
        
    except Exception as e:
        debug_log(f"ERROR during conversion: {e}")
        return False

def list_existing_files():
    """List all existing save files in the directory"""
    debug_log("=== Existing Save Files ===")
    
    # Look for legacy files
    legacy_files = []
    if os.path.exists(f"{RESULTS_FILE}.progress"):
        legacy_files.append(f"{RESULTS_FILE}.progress")
    if os.path.exists(f"{PARSED_DIRS_FILE}.progress"):
        legacy_files.append(f"{PARSED_DIRS_FILE}.progress")
    
    if legacy_files:
        debug_log(f"Legacy files found: {legacy_files}")
    else:
        debug_log("No legacy files found")
    
    # Look for per-drive files
    per_drive_files = []
    for file in os.listdir('.'):
        if (file.startswith(RESULTS_FILE) or file.startswith(PARSED_DIRS_FILE)) and '.progress' in file and file not in legacy_files:
            per_drive_files.append(file)
    
    if per_drive_files:
        debug_log(f"Per-drive files found: {sorted(per_drive_files)}")
    else:
        debug_log("No per-drive files found")
    
    # Look for backup files
    backup_files = []
    for file in os.listdir('.'):
        if '.backup_' in file and (RESULTS_FILE in file or PARSED_DIRS_FILE in file):
            backup_files.append(file)
    
    if backup_files:
        debug_log(f"Backup files found: {sorted(backup_files)}")
    else:
        debug_log("No backup files found")

if __name__ == "__main__":
    debug_log("Save File Converter - Convert legacy combined files to per-drive format")
    debug_log(f"Working directory: {os.getcwd()}")
    
    # List existing files
    list_existing_files()
    
    # Ask user if they want to proceed
    print("\nThis will convert legacy save files to the new per-drive format.")
    print("Original files will be backed up with a timestamp suffix.")
    
    response = input("Do you want to proceed? (y/N): ").lower().strip()
    
    if response in ['y', 'yes']:
        success = convert_save_files()
        if success:
            print("\n✅ Conversion completed successfully!")
            print("You can now use the Resume function in the main application.")
        else:
            print("\n❌ Conversion failed. Check the output above for errors.")
    else:
        debug_log("Conversion cancelled by user")
        print("Conversion cancelled.")
