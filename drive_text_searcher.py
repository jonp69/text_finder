import sys
import os
import re
import json
import threading
import time
import string
import shutil
from pathlib import Path
from queue import Queue
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QWidget, QPushButton,
    QLabel, QListWidget, QProgressBar, QFileDialog, QHBoxLayout, QCheckBox
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer, QThread

# Global variables for log categories and flags
LOG_SETTINGS = {
    "console": {
        "debug": True,
        "progress": False,
        "file": False
    },
    "file": {
        "debug": True,
        "progress": True,
        "file": True
    }
}

# Function to log messages based on category and flags
def log_message(message, category="debug", to_console=True, to_file=True):
    if to_console and LOG_SETTINGS["console"].get(category, False):
        print(f"[{category.upper()}] {message}")
    if to_file and LOG_SETTINGS["file"].get(category, False):
        with open(f"{category}_log.txt", "a", encoding="utf-8") as log_file:
            log_file.write(f"[{category.upper()}] {message}\n")

# Debug logging function
def debug_log(message):
    """Print debug messages with timestamp"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_message(f"{timestamp} DEBUG: {message}", category="debug")

# ------------- Settings -------------
MIN_FILE_SIZE = 256  # bytes
RESULTS_FILE = "detected_text_files.json"
PARSED_DIRS_FILE = "parsed_directories.json"
FILE_COUNT_FILE = "file_count_cache.json"  # Store file count between sessions
PROGRESSIVE_SAVE_BATCH_SIZE = 100  # Save every N completed directories
PROGRESSIVE_SAVE_TIME_INTERVAL = 30  # Save every N seconds during scanning
SYSTEM_DIRS = [
    os.environ.get('SystemRoot', r'C:\Windows'),
    r'C:\Program Files', r'C:\Program Files (x86)',
    r'C:\$Recycle.Bin', r'C:\Users\All Users', r'C:\ProgramData'
]
TEXT_SAMPLE_SIZE = 2048  # bytes to sample per file for detection

def is_text_file(filepath):
    """Detect if a file is likely text by sampling its start."""
    try:
        with open(filepath, 'rb') as f:
            sample = f.read(TEXT_SAMPLE_SIZE)
        if not sample:
            return False
        # Heuristic: ASCII + some Unicode range, little binary
        text_chars = bytearray({7,8,9,10,12,13,27} | set(range(0x20, 0x100)))
        nontext = sum(byte not in text_chars for byte in sample)
        return nontext / len(sample) < 0.10
    except Exception:
        return False

def is_system_path(path):
    """Check whether path is a system directory."""
    path = str(path)
    for sysdir in SYSTEM_DIRS:
        if path.startswith(sysdir):
            return True
    # Also exclude hidden and dot dirs
    if any(part.startswith('.') for part in Path(path).parts):
        return True
    return False

class FileCountWorker(QObject):
    """Worker to count potential files in parallel with main scan"""
    drive_counted = Signal(str, int)  # drive, file_count
    counting_finished = Signal(int)   # total_file_count
    updated_count_response = Signal(int)  # Response with current best estimate when requested
    
    def __init__(self, drives, parent=None, mixed_counts=None):
        super().__init__(parent)
        self.drives = drives
        self._abort = False
        self.current_total_estimate = 0  # Current best estimate of total files
        self.mixed_counts = mixed_counts or {}  # Info about cached vs uncached drives
        
    def abort(self):
        self._abort = True
        
    def provide_current_estimate(self):
        """Provide current best estimate when requested by search worker"""
        debug_log(f"FileCountWorker providing current estimate: {self.current_total_estimate}")
        self.updated_count_response.emit(self.current_total_estimate)
        
    def count_files(self):
        """Count files - only for uncached drives if mixed_counts provided"""
        debug_log("Starting file counting operation")
        
        # If we have mixed counts info, start with cached totals
        if self.mixed_counts and 'total_cached' in self.mixed_counts:
            total_files = self.mixed_counts['total_cached']
            drives_to_count = self.mixed_counts.get('uncached_drives', [])
            if not drives_to_count:
                drives_to_count = self.drives
            debug_log(f"Mixed counting mode: Starting with {total_files} cached files, counting {len(drives_to_count)} uncached drives")
        else:
            total_files = 0
            drives_to_count = self.drives
            debug_log(f"Full counting mode: Counting all {len(drives_to_count)} drives")
        
        self.current_total_estimate = total_files  # Start with cached amount
        
        for drive in drives_to_count:
            if self._abort:
                debug_log("File counting aborted")
                return
                
            debug_log(f"Counting files on drive: {drive}")
            drive_start_time = time.time()
            drive_file_count = 0
            
            try:
                for root, dirs, files in os.walk(drive):
                    if self._abort:
                        return
                    if is_system_path(root):
                        dirs[:] = []  # don't descend
                        continue
                    
                    for fname in files:
                        if self._abort:
                            return
                        try:
                            fpath = os.path.join(root, fname)
                            if os.path.getsize(fpath) >= MIN_FILE_SIZE:
                                drive_file_count += 1
                        except Exception:
                            continue
                            
            except Exception as e:
                debug_log(f"Error counting files on drive {drive}: {e}")
                continue
                
            drive_duration = time.time() - drive_start_time
            debug_log(f"Drive {drive} counting complete: {drive_file_count} files in {drive_duration:.1f}s")
            
            # Update totals and notify
            total_files += drive_file_count
            self.current_total_estimate = total_files
            self.drive_counted.emit(drive, drive_file_count)
            
        debug_log(f"File counting completed - Total: {total_files} files (including {self.mixed_counts.get('total_cached', 0)} cached)")
        self.current_total_estimate = total_files
        self.counting_finished.emit(total_files)

class SearchWorker(QObject):
    update_progress = Signal(str, int, int, str)  # current_dir, files_processed, total_files, current_drive
    drive_completed = Signal(str, int, int)  # drive, files_found, files_processed
    finished = Signal(list, list)  # detected_files, parsed_dirs
    progressive_save = Signal(list, list)  # detected_files, parsed_dirs for progressive save
    save_progress = Signal(int, str)  # save_count, save_description
    save_countdown = Signal(int, int, int)  # dirs_until_save, seconds_until_save, total_saves
    request_updated_count = Signal()  # Request updated file count when approaching maximum

    def __init__(self, drives, parent=None, resume_data=None):
        super().__init__(parent)
        self.drives = drives
        self._abort = False
        self.last_save_time = time.time()
        self.resume_data = resume_data or {}
        self.per_drive_data = {}  # Track data per drive for saving
        self.total_files_to_process = 0  # Will be set by counting worker
        self.files_processed_so_far = 0
        self.save_count = 0  # Track number of saves performed
        self.completed_dirs_since_save = 0  # Track directories since last save
        
        debug_log(f"SearchWorker initialized for drives: {drives}")
        if self.resume_data:
            debug_log(f"Resume mode: {len(self.resume_data.get('detected_files', []))} files, {len(self.resume_data.get('parsed_dirs', []))} dirs")

    def set_total_files(self, total_files):
        """Set the total number of files to process (from counting worker)"""
        self.total_files_to_process = total_files
        debug_log(f"SearchWorker received total file count: {total_files}")

    def _check_if_update_needed(self):
        """Check if we need an updated count because we're approaching the limit"""
        if self.total_files_to_process > 0:
            # If we're at 95% of estimated total, request an update
            progress_percentage = (self.files_processed_so_far / self.total_files_to_process) * 100
            if progress_percentage >= 95:
                debug_log(f"SearchWorker at {progress_percentage:.1f}% of estimate, requesting count update")
                self.request_updated_count.emit()

    def _get_drive_from_path(self, path):
        """Extract drive letter from path (e.g., C:\\ from C:\\Users\\...)"""
        if os.name == 'nt':
            return str(Path(path).parts[0]) if Path(path).parts else path[:3]
        return '/'

    def _save_drive_progress(self, drive, detected_files_for_drive, parsed_dirs_for_drive):
        """Save progress for a specific drive"""
        try:
            # Create drive-specific filenames (replace : with _ for Windows)
            drive_safe = drive.replace(':', '').replace('\\', '')
            results_file = f"{RESULTS_FILE}.{drive_safe}.progress"
            dirs_file = f"{PARSED_DIRS_FILE}.{drive_safe}.progress"
            
            # Save detected files for this drive
            with open(results_file, 'w', encoding='utf-8') as f:
                json.dump(detected_files_for_drive, f, indent=2)
            
            # Save parsed directories for this drive  
            with open(dirs_file, 'w', encoding='utf-8') as f:
                json.dump(parsed_dirs_for_drive, f, indent=2)
                
            self.save_count += 1
            debug_log(f"Saved drive {drive} progress: {len(detected_files_for_drive)} files, {len(parsed_dirs_for_drive)} dirs")
            self.save_progress.emit(self.save_count, f"Saved {drive}: {len(detected_files_for_drive)} files")
        except Exception as e:
            debug_log(f"ERROR: Could not save progress for drive {drive}: {e}")

    def abort(self):
        self._abort = True
        debug_log("SearchWorker abort requested")

    def scan(self):
        debug_log("Starting scan operation")
        
        # Initialize with resume data if available
        detected_files = self.resume_data.get('detected_files', []).copy()
        parsed_dirs = self.resume_data.get('parsed_dirs', [])
        already_scanned = set(parsed_dirs)  # Convert to set for fast lookup
        
        self.completed_dirs_since_save = 0  # Reset counter for this scan
        
        # Initialize per-drive tracking
        for drive in self.drives:
            self.per_drive_data[drive] = {
                'detected_files': [],
                'parsed_dirs': []
            }
        
        debug_log(f"Starting with {len(detected_files)} existing files, {len(already_scanned)} already scanned dirs")
        
        for drive in self.drives:
            debug_log(f"Beginning scan of drive: {drive}")
            drive_start_time = time.time()
            drive_files_processed = 0
            drive_files_found = 0
            
            for root, dirs, files in os.walk(drive):
                if self._abort:
                    debug_log("Scan aborted by user")
                    return
                if is_system_path(root):
                    dirs[:] = []  # don't descend
                    debug_log(f"Skipping system path: {root}")
                    continue
                
                # Skip if this directory was already scanned in previous session
                if root in already_scanned:
                    debug_log(f"Skipping already scanned directory: {root}")
                    continue
                
                # Process all files in current directory
                files_in_dir = 0
                for fname in files:
                    if self._abort:
                        debug_log("Scan aborted during file processing")
                        return
                    fpath = os.path.join(root, fname)
                    try:
                        if os.path.getsize(fpath) < MIN_FILE_SIZE:
                            continue
                        
                        drive_files_processed += 1
                        self.files_processed_so_far += 1
                        
                        # Check if we need an updated count estimate
                        if drive_files_processed % 100 == 0:  # Check every 100 files
                            self._check_if_update_needed()
                        
                        # Update progress every 50 files to avoid too many signals
                        if drive_files_processed % 50 == 0 or drive_files_processed <= 10:
                            self.update_progress.emit(root, self.files_processed_so_far, 
                                                    self.total_files_to_process, drive)
                        
                        if is_text_file(fpath):
                            detected_files.append(fpath)
                            # Track per-drive
                            self.per_drive_data[drive]['detected_files'].append(fpath)
                            files_in_dir += 1
                            drive_files_found += 1
                            
                    except Exception as e:
                        debug_log(f"Error processing file {fpath}: {e}")
                        continue
                
                # Directory is now fully processed
                parsed_dirs.append(root)
                self.per_drive_data[drive]['parsed_dirs'].append(root)
                self.completed_dirs_since_save += 1
                
                if files_in_dir > 0:
                    debug_log(f"Found {files_in_dir} text files in {root}")
                
                # Emit save countdown progress every few directories
                if self.completed_dirs_since_save % 5 == 0:  # Update every 5 directories
                    dirs_until_save = PROGRESSIVE_SAVE_BATCH_SIZE - self.completed_dirs_since_save
                    current_time = time.time()
                    seconds_until_save = max(0, PROGRESSIVE_SAVE_TIME_INTERVAL - (current_time - self.last_save_time))
                    self.save_countdown.emit(dirs_until_save, int(seconds_until_save), self.save_count)
                
                # Progressive save based on batch size or time interval
                current_time = time.time()
                should_save = (
                    self.completed_dirs_since_save >= PROGRESSIVE_SAVE_BATCH_SIZE or
                    (current_time - self.last_save_time) >= PROGRESSIVE_SAVE_TIME_INTERVAL
                )
                
                if should_save:
                    debug_log(f"Triggering progressive save - {len(detected_files)} files, {len(parsed_dirs)} dirs")
                    self.progressive_save.emit(detected_files.copy(), parsed_dirs.copy())
                    # Also save per-drive progress
                    self._save_drive_progress(drive, 
                                            self.per_drive_data[drive]['detected_files'].copy(),
                                            self.per_drive_data[drive]['parsed_dirs'].copy())
                    self.completed_dirs_since_save = 0  # Reset counter after save
                    self.last_save_time = current_time
            
            drive_duration = time.time() - drive_start_time
            debug_log(f"Completed drive {drive} in {drive_duration:.1f}s - {drive_files_found} text files from {drive_files_processed} processed")
            
            # Emit drive completion signal
            self.drive_completed.emit(drive, drive_files_found, drive_files_processed)
            
            # Save final drive progress when drive is complete
            self._save_drive_progress(drive, 
                                    self.per_drive_data[drive]['detected_files'],
                                    self.per_drive_data[drive]['parsed_dirs'])
                    
        debug_log(f"Scan completed - Total: {len(detected_files)} text files from {self.files_processed_so_far} files processed")
        self.finished.emit(detected_files, parsed_dirs)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        debug_log("MainWindow initializing...")
        self.setWindowTitle("Drive Text Searcher")
        self.setMinimumSize(800, 500)
        self.setStyleSheet("""
            QWidget {
                background-color: #232629; color: #f0f0f0;
                font-family: Segoe UI, Arial, sans-serif;
            }
            QPushButton { background-color: #32363b; color: #f0f0f0; border-radius: 7px; }
            QPushButton:hover { background-color: #4b5160; }
            QProgressBar { background: #1c1d21; color: #fff; border-radius: 7px; }
            QListWidget { background: #232629; color: #e0e0e0; }
            QLabel { color: #d4d4d4; }
        """)
        self._build_ui()
        self.search_thread = None
        self.worker = None
        self.count_thread = None
        self.count_worker = None
        self.total_files_to_process = 0
        self.current_drive = ""
        self.save_operations = 0
        self.running_file_count = 0  # Track cumulative file count for incremental saves
        
        # Per-drive tracking dictionaries
        self.drive_files_processed = {}  # Track files processed per drive: {drive: count}
        self.drive_start_counts = {}     # Track where each drive started: {drive: overall_count}
        self.drive_max_files = {}        # Track expected maximum files per drive: {drive: max_count}
        self.drive_status = {}          # Track drive scanning status: {drive: 'scanning'|'completed'}
        
        debug_log("MainWindow initialization complete")

    def _build_ui(self):
        central = QWidget()
        vbox = QVBoxLayout()
        h1 = QHBoxLayout()
        self.dir_label = QLabel("Drives to scan:")
        self.dir_list = QListWidget()
        # Enable multi-selection for drives
        self.dir_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.refresh_drives_btn = QPushButton("Refresh Drives")
        self.refresh_drives_btn.clicked.connect(self.load_drives)
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self.select_all_drives)
        self.select_none_btn = QPushButton("Select None")
        self.select_none_btn.clicked.connect(self.select_no_drives)
        self.start_btn = QPushButton("Start Scan")
        self.start_btn.clicked.connect(self.start_scan)
        self.resume_btn = QPushButton("Resume Scan")
        self.resume_btn.clicked.connect(self.resume_scan)
        self.resume_btn.setEnabled(False)  # Disabled until progress files found
        h1.addWidget(self.dir_label)
        h1.addWidget(self.dir_list)
        h1.addWidget(self.refresh_drives_btn)
        h1.addWidget(self.select_all_btn)
        h1.addWidget(self.select_none_btn)
        h1.addWidget(self.start_btn)
        h1.addWidget(self.resume_btn)
        vbox.addLayout(h1)
        
        # Progress section with triple progress bars
        progress_layout = QVBoxLayout()
        
        # Overall progress across all drives
        overall_label = QLabel("Overall Progress:")
        progress_layout.addWidget(overall_label)
        self.overall_progress = QProgressBar()
        self.overall_progress.setMaximum(100)
        self.overall_progress.setFormat("%p% - %v of %m files")
        progress_layout.addWidget(self.overall_progress)
        
        # Current drive progress
        drive_label = QLabel("Current Drive Progress:")
        progress_layout.addWidget(drive_label)
        self.drive_progress = QProgressBar()
        self.drive_progress.setMaximum(100)
        self.drive_progress.setFormat("Drive %p% - Scanning...")
        progress_layout.addWidget(self.drive_progress)
        
        # Save progress bar
        save_label = QLabel("Save Progress:")
        progress_layout.addWidget(save_label)
        self.save_progress = QProgressBar()
        self.save_progress.setMaximum(100)
        self.save_progress.setValue(0)
        self.save_progress.setFormat("No saves yet")
        progress_layout.addWidget(self.save_progress)
        
        vbox.addLayout(progress_layout)
        self.status_lbl = QLabel("Ready.")
        vbox.addWidget(self.status_lbl)
        self.results_list = QListWidget()
        vbox.addWidget(self.results_list, 2)
        central.setLayout(vbox)
        self.setCentralWidget(central)
        self.load_drives()
        self.check_for_resume_files()

    def check_for_resume_files(self):
        """Check if progress files exist and enable resume button if they do"""
        # Check for per-drive progress files
        drives = self.get_all_drives()
        per_drive_files_exist = []
        
        for drive in drives:
            drive_safe = drive.replace(':', '').replace('\\', '')
            results_file = f"{RESULTS_FILE}.{drive_safe}.progress"
            dirs_file = f"{PARSED_DIRS_FILE}.{drive_safe}.progress"
            
            if os.path.exists(results_file) and os.path.exists(dirs_file):
                per_drive_files_exist.append(drive)
        
        # Also check for legacy combined progress files
        legacy_files_exist = (
            os.path.exists(f"{RESULTS_FILE}.progress") and 
            os.path.exists(f"{PARSED_DIRS_FILE}.progress")
        )
        
        if per_drive_files_exist or legacy_files_exist:
            debug_log(f"Progress files found - Per-drive: {per_drive_files_exist}, Legacy: {legacy_files_exist}")
            self.resume_btn.setEnabled(True)
            if per_drive_files_exist:
                self.status_lbl.setText(f"Ready. Progress found for drives: {', '.join(per_drive_files_exist)} - you can resume.")
            else:
                self.status_lbl.setText("Ready. Previous scan progress detected - you can resume.")
        else:
            debug_log("No progress files found")
            self.resume_btn.setEnabled(False)

    def load_drives(self):
        debug_log("Loading available drives...")
        self.dir_list.clear()
        drives = self.get_all_drives()
        debug_log(f"Found drives: {drives}")
        for d in drives:
            self.dir_list.addItem(d)
            self.dir_list.item(self.dir_list.count()-1).setSelected(True)
        debug_log(f"Loaded {len(drives)} drives to UI")

    def select_all_drives(self):
        """Select all drives in the list"""
        debug_log("Selecting all drives")
        for i in range(self.dir_list.count()):
            self.dir_list.item(i).setSelected(True)

    def select_no_drives(self):
        """Deselect all drives in the list"""
        debug_log("Deselecting all drives")
        for i in range(self.dir_list.count()):
            self.dir_list.item(i).setSelected(False)

    def get_all_drives(self):
        drives = []
        if os.name == 'nt':
            import string
            for l in string.ascii_uppercase:
                drive = f"{l}:\\"
                if os.path.exists(drive):
                    drives.append(drive)
        else:
            drives.append('/')
        return drives

    def load_cached_file_count(self):
        """Load previously cached file count if available (legacy global cache)"""
        try:
            if os.path.exists(FILE_COUNT_FILE):
                with open(FILE_COUNT_FILE, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    cached_count = cache_data.get('total_files', 0)
                    cache_age = time.time() - cache_data.get('timestamp', 0)
                    
                    # Always use recent cache (less than 24 hours)
                    if cache_age < 86400:  # 24 hours in seconds
                        debug_log(f"Using recent cached file count: {cached_count} (age: {cache_age/3600:.1f}h)")
                        return cached_count
                    else:
                        # Ask user about older cache
                        from PySide6.QtWidgets import QMessageBox
                        age_days = cache_age / 86400
                        msg = QMessageBox()
                        msg.setWindowTitle("Cached File Count Found")
                        msg.setText(f"Found cached file count: {cached_count:,} files")
                        msg.setInformativeText(f"Cache is {age_days:.1f} days old. How would you like to proceed?")
                        
                        # Add three buttons for different options
                        use_btn = msg.addButton("Use Cached (No Recount)", QMessageBox.ButtonRole.AcceptRole)
                        initial_btn = msg.addButton("Use as Initial + Recount", QMessageBox.ButtonRole.ActionRole) 
                        recount_btn = msg.addButton("Ignore Cache + Recount", QMessageBox.ButtonRole.RejectRole)
                        msg.setDefaultButton(initial_btn)
                        
                        result = msg.exec()
                        clicked_button = msg.clickedButton()
                        
                        if clicked_button == use_btn:
                            debug_log(f"User chose to use old cached count without recounting: {cached_count} (age: {age_days:.1f} days)")
                            return cached_count
                        elif clicked_button == initial_btn:
                            debug_log(f"User chose to use cached count as initial but still recount: {cached_count} (age: {age_days:.1f} days)")
                            return ("initial", cached_count)  # Special return value to indicate "use as initial"
                        else:
                            debug_log(f"User chose to ignore cache and recount from scratch (age: {age_days:.1f} days)")
        except Exception as e:
            debug_log(f"Could not load cached file count: {e}")
        return None
        
    def load_per_drive_cached_counts(self, drives):
        """Load cached file counts for individual drives, return mixed counts info"""
        mixed_counts = {
            'cached_drives': {},
            'uncached_drives': [],
            'total_cached': 0,
            'estimated_uncached': 0,
            'needs_counting': False
        }
        
        # First check if we have a global cache to split if no per-drive caches exist
        global_cache_to_split = None
        has_any_per_drive_cache = False
        
        # Quick check for any existing per-drive caches
        for drive in drives:
            drive_safe = drive.replace(':', '').replace('\\', '')
            drive_cache_file = f"file_count_cache_{drive_safe}.json"
            if os.path.exists(drive_cache_file):
                has_any_per_drive_cache = True
                break
        
        # If no per-drive caches exist, check for global cache to split
        if not has_any_per_drive_cache:
            try:
                if os.path.exists(FILE_COUNT_FILE):
                    with open(FILE_COUNT_FILE, 'r', encoding='utf-8') as f:
                        cache_data = json.load(f)
                        cached_count = cache_data.get('total_files', 0)
                        cache_age = time.time() - cache_data.get('timestamp', 0)
                        
                        if cached_count > 0:
                            global_cache_to_split = {
                                'total_files': cached_count,
                                'age': cache_age,
                                'age_days': cache_age / 86400
                            }
                            debug_log(f"Found global cache to split: {cached_count} files, age: {cache_age/3600:.1f}h")
            except Exception as e:
                debug_log(f"Error checking global cache: {e}")
        
        # Process each drive
        for drive in drives:
            drive_safe = drive.replace(':', '').replace('\\', '')
            drive_cache_file = f"file_count_cache_{drive_safe}.json"
            
            try:
                if os.path.exists(drive_cache_file):
                    with open(drive_cache_file, 'r', encoding='utf-8') as f:
                        cache_data = json.load(f)
                        cached_count = cache_data.get('file_count', 0)
                        cache_age = time.time() - cache_data.get('timestamp', 0)
                        
                        # Use cache if less than 24 hours old
                        if cache_age < 86400:
                            mixed_counts['cached_drives'][drive] = cached_count
                            mixed_counts['total_cached'] += cached_count
                            debug_log(f"Drive {drive}: Using cached count {cached_count} (age: {cache_age/3600:.1f}h)")
                        else:
                            # Cache is old, ask user what to do
                            user_choice = self._ask_user_per_drive_cache(drive, cached_count, cache_age)
                            if user_choice in ['use_cached', 'use_initial']:
                                mixed_counts['cached_drives'][drive] = cached_count
                                mixed_counts['total_cached'] += cached_count
                                if user_choice == 'use_initial':
                                    mixed_counts['needs_counting'] = True
                                debug_log(f"Drive {drive}: User chose {user_choice} with old cache {cached_count}")
                            else:
                                # User chose default estimate
                                mixed_counts['uncached_drives'].append(drive)
                                estimate = 200000 if 'C:' in drive.upper() else 50000
                                mixed_counts['estimated_uncached'] += estimate
                                if user_choice == 'default_scan':
                                    mixed_counts['needs_counting'] = True
                                debug_log(f"Drive {drive}: User chose {user_choice} with estimate {estimate}")
                else:
                    # No per-drive cache - check if we should split global cache
                    if global_cache_to_split:
                        # Calculate weighted split of global cache
                        weighted_count = self._calculate_weighted_split(drive, global_cache_to_split['total_files'], drives)
                        
                        # Ask user what to do with weighted estimate
                        user_choice = self._ask_user_per_drive_weighted(drive, weighted_count, global_cache_to_split['age_days'])
                        
                        if user_choice in ['weighted_no_scan', 'weighted_scan']:
                            mixed_counts['cached_drives'][drive] = weighted_count
                            mixed_counts['total_cached'] += weighted_count
                            if user_choice == 'weighted_scan':
                                mixed_counts['needs_counting'] = True
                            debug_log(f"Drive {drive}: User chose {user_choice} with weighted {weighted_count}")
                        else:
                            # User chose default estimate
                            mixed_counts['uncached_drives'].append(drive)
                            estimate = 200000 if 'C:' in drive.upper() else 50000
                            mixed_counts['estimated_uncached'] += estimate
                            if user_choice == 'default_scan':
                                mixed_counts['needs_counting'] = True
                            debug_log(f"Drive {drive}: User chose {user_choice} with default estimate {estimate}")
                    else:
                        # No cache at all for this drive
                        mixed_counts['uncached_drives'].append(drive)
                        estimate = 200000 if 'C:' in drive.upper() else 50000
                        mixed_counts['estimated_uncached'] += estimate
                        debug_log(f"Drive {drive}: No cache found, using estimate {estimate}")
                        
            except Exception as e:
                debug_log(f"Error loading cache for drive {drive}: {e}")
                # Treat as uncached if error
                mixed_counts['uncached_drives'].append(drive)
                estimate = 200000 if 'C:' in drive.upper() else 50000
                mixed_counts['estimated_uncached'] += estimate
        
        # Determine if counting is needed
        if not mixed_counts['needs_counting']:
            mixed_counts['needs_counting'] = len(mixed_counts['uncached_drives']) > 0
            
        total_initial = mixed_counts['total_cached'] + mixed_counts['estimated_uncached']
        
        debug_log(f"Mixed counts summary: {len(mixed_counts['cached_drives'])} cached drives ({mixed_counts['total_cached']} files), "
                 f"{len(mixed_counts['uncached_drives'])} uncached drives (~{mixed_counts['estimated_uncached']} estimated), "
                 f"total initial: {total_initial}")
        
        return mixed_counts
    
    def _calculate_weighted_split(self, drive, total_files, all_drives):
        """Calculate weighted split of global cache for a specific drive using points system"""

        # Calculate points for each drive
        drive_points = {}
        os_drive_size = None

        # First, find the OS drive and get its used space
        for d in all_drives:
            if 'C:' in d.upper():
                try:
                    os_drive_size = shutil.disk_usage(d).used
                    drive_points[d] = 3.0  # OS drive gets 3 points automatically
                    debug_log(f"OS drive {d} found with used space: {os_drive_size}")
                except Exception as e:
                    debug_log(f"Error getting used space for OS drive {d}: {e}")
                break

        # If no OS drive found, treat first drive as OS drive
        if os_drive_size is None:
            os_drive_size = 1024**3 * 100  # 100GB fallback
            drive_points[all_drives[0]] = 3.0
            debug_log(f"No OS drive found, treating {all_drives[0]} as OS drive with 3.0 points")

        # Calculate points for other drives based on their used space relative to OS drive
        for d in all_drives:
            if d not in drive_points:  # Skip OS drive (already processed)
                try:
                    used_space = shutil.disk_usage(d).used
                    drive_points[d] = used_space / os_drive_size  # Relative to OS drive
                    debug_log(f"Drive {d} used space: {used_space}, points: {drive_points[d]:.2f}")
                except Exception as e:
                    debug_log(f"Error getting used space for drive {d}: {e}")

        # Calculate total points
        total_points = sum(drive_points.values())

        # Calculate weight for the requested drive (percentage of total points)
        drive_weight = drive_points.get(drive, 1.0) / total_points if total_points > 0 else 1.0 / len(all_drives)

        weighted_count = int(total_files * drive_weight)

        debug_log(f"Points system: {drive} gets {drive_points.get(drive, 1.0):.2f}/{total_points:.2f} points = {drive_weight:.2f} weight = {weighted_count} files")

        return weighted_count
    
    def _ask_user_per_drive_cache(self, drive, cached_count, cache_age):
        """Ask user what to do with old cached count for a specific drive"""
        from PySide6.QtWidgets import QMessageBox
        age_days = cache_age / 86400
        
        msg = QMessageBox()
        msg.setWindowTitle(f"Drive {drive} Cache Found")
        msg.setText(f"Drive {drive}: Found cached count: {cached_count:,} files")
        msg.setInformativeText(f"Cache is {age_days:.1f} days old. How would you like to proceed?")
        
        use_cached_btn = msg.addButton("Use Cached (No Scan)", QMessageBox.ButtonRole.AcceptRole)
        use_initial_btn = msg.addButton("Use as Initial + Scan", QMessageBox.ButtonRole.ActionRole)
        default_no_scan_btn = msg.addButton("Default Estimate (No Scan)", QMessageBox.ButtonRole.RejectRole)
        default_scan_btn = msg.addButton("Default Estimate + Scan", QMessageBox.ButtonRole.DestructiveRole)
        msg.setDefaultButton(use_initial_btn)
        
        msg.exec()
        clicked_button = msg.clickedButton()
        
        if clicked_button == use_cached_btn:
            return 'use_cached'
        elif clicked_button == use_initial_btn:
            return 'use_initial'
        elif clicked_button == default_no_scan_btn:
            return 'default_no_scan'
        else:
            return 'default_scan'
    
    def _ask_user_per_drive_weighted(self, drive, weighted_count, age_days):
        """Ask user what to do with weighted estimate from global cache for a specific drive"""
        from PySide6.QtWidgets import QMessageBox
        
        msg = QMessageBox()
        msg.setWindowTitle(f"Drive {drive} - Weighted Estimate")
        msg.setText(f"Drive {drive}: Weighted estimate from global cache: {weighted_count:,} files")
        msg.setInformativeText(f"Based on {age_days:.1f} day old global cache. How would you like to proceed?")
        
        weighted_no_scan_btn = msg.addButton("Use Weighted (No Scan)", QMessageBox.ButtonRole.AcceptRole)
        weighted_scan_btn = msg.addButton("Use Weighted + Scan", QMessageBox.ButtonRole.ActionRole)
        default_no_scan_btn = msg.addButton("Default Estimate (No Scan)", QMessageBox.ButtonRole.RejectRole)
        default_scan_btn = msg.addButton("Default Estimate + Scan", QMessageBox.ButtonRole.DestructiveRole)
        msg.setDefaultButton(weighted_scan_btn)
        
        msg.exec()
        clicked_button = msg.clickedButton()
        
        if clicked_button == weighted_no_scan_btn:
            return 'weighted_no_scan'
        elif clicked_button == weighted_scan_btn:
            return 'weighted_scan'
        elif clicked_button == default_no_scan_btn:
            return 'default_no_scan'
        else:
            return 'default_scan'
            
    def _initialize_drive_tracking(self, drives):
        """Initialize per-drive tracking dictionaries for the selected drives"""
        self.drive_files_processed.clear()
        self.drive_start_counts.clear() 
        self.drive_max_files.clear()
        self.drive_status.clear()
        
        for drive in drives:
            self.drive_files_processed[drive] = 0
            self.drive_start_counts[drive] = 0  # Will be set when drive actually starts
            self.drive_max_files[drive] = 0     # Will be set from cache or estimates
            self.drive_status[drive] = 'pending'
            
        debug_log(f"Initialized drive tracking for {len(drives)} drives: {drives}")
        
    def get_drive_tracking_summary(self):
        """Get a summary of current drive tracking status for debugging"""
        summary = []
        for drive in self.drive_status.keys():
            status = self.drive_status[drive]
            files_processed = self.drive_files_processed.get(drive, 0)
            start_count = self.drive_start_counts.get(drive, 0)
            max_files = self.drive_max_files.get(drive, 0)
            summary.append(f"{drive}: {status}, {files_processed}/{max_files} files (started at {start_count})")
        return "; ".join(summary)

    def save_cached_file_count(self, total_files):
        """Save file count to cache with timestamp (legacy global cache)"""
        try:
            with open(FILE_COUNT_FILE, 'w', encoding='utf-8') as f:
                json.dump({'total_files': total_files, 'timestamp': time.time()}, f)
            debug_log(f"Cached file count {total_files} for future sessions")
        except Exception as e:
            debug_log(f"ERROR: Could not cache file count: {e}")
            
    def save_per_drive_cached_count(self, drive, file_count):
        """Save file count for a specific drive with timestamp"""
        try:
            drive_safe = drive.replace(':', '').replace('\\', '')
            drive_cache_file = f"file_count_cache_{drive_safe}.json"
            with open(drive_cache_file, 'w', encoding='utf-8') as f:
                json.dump({'file_count': file_count, 'timestamp': time.time(), 'drive': drive}, f)
            debug_log(f"Cached drive {drive} file count: {file_count}")
        except Exception as e:
            debug_log(f"ERROR: Could not cache file count for drive {drive}: {e}")
            
    def save_drive_tracking_state(self, drives):
        """Save complete drive tracking state to progress files"""
        try:
            drive_tracking_file = "drive_tracking_state.json"
            tracking_state = {
                'drive_files_processed': self.drive_files_processed.copy(),
                'drive_start_counts': self.drive_start_counts.copy(),
                'drive_max_files': self.drive_max_files.copy(),
                'drive_status': self.drive_status.copy(),
                'timestamp': time.time(),
                'drives': drives
            }
            with open(drive_tracking_file, 'w', encoding='utf-8') as f:
                json.dump(tracking_state, f, indent=2)
            debug_log(f"Saved drive tracking state for {len(drives)} drives")
        except Exception as e:
            debug_log(f"ERROR: Could not save drive tracking state: {e}")
            
    def load_drive_tracking_state(self, drives):
        """Load drive tracking state from progress files"""
        try:
            drive_tracking_file = "drive_tracking_state.json"
            if os.path.exists(drive_tracking_file):
                with open(drive_tracking_file, 'r', encoding='utf-8') as f:
                    tracking_state = json.load(f)
                
                # Restore tracking dictionaries for drives that match current selection
                for drive in drives:
                    if drive in tracking_state.get('drive_files_processed', {}):
                        self.drive_files_processed[drive] = tracking_state['drive_files_processed'][drive]
                        self.drive_start_counts[drive] = tracking_state['drive_start_counts'].get(drive, 0)
                        self.drive_max_files[drive] = tracking_state['drive_max_files'].get(drive, 0)
                        self.drive_status[drive] = tracking_state['drive_status'].get(drive, 'pending')
                
                debug_log(f"Restored drive tracking state for drives: {list(self.drive_files_processed.keys())}")
                return True
        except Exception as e:
            debug_log(f"ERROR: Could not load drive tracking state: {e}")
        return False

    def start_scan(self):
        debug_log("start_scan() called")
        if self.search_thread and self.search_thread.is_alive():
            debug_log("Scan already running - skipping")
            self.status_lbl.setText("Scan already running.")
            return
        drives = [self.dir_list.item(i).text() for i in range(self.dir_list.count()) if self.dir_list.item(i).isSelected()]
        if not drives:
            debug_log("No drives selected")
            self.status_lbl.setText("No drives selected.")
            return
        debug_log(f"Starting scan for drives: {drives}")
        self.status_lbl.setText("Starting parallel file counting and scanning...")
        self.overall_progress.setValue(0)
        self.drive_progress.setValue(0)
        self.results_list.clear()
        self.running_file_count = 0  # Reset running count for new scan
        
        # Initialize per-drive tracking dictionaries for selected drives
        self._initialize_drive_tracking(drives)
        
        # Check for mixed per-drive cached counts (now handles global cache splitting)
        mixed_counts = self.load_per_drive_cached_counts(drives)
        
        # Set per-drive maximum values from cached or estimated counts
        for drive in drives:
            if drive in mixed_counts['cached_drives']:
                self.drive_max_files[drive] = mixed_counts['cached_drives'][drive]
            else:
                # Use default estimates for uncached drives
                estimate = 200000 if 'C:' in drive.upper() else 50000
                self.drive_max_files[drive] = estimate
                
        debug_log(f"Set per-drive max files: {self.drive_max_files}")
        
        # Determine initial total using mixed counts
        if mixed_counts['cached_drives'] or mixed_counts['estimated_uncached'] > 0:
            # We have some cached drives or weighted estimates
            estimated_total = mixed_counts['total_cached'] + mixed_counts['estimated_uncached']
            cache_info = f"{len(mixed_counts['cached_drives'])} cached/weighted drives ({mixed_counts['total_cached']} files)"
            if mixed_counts['needs_counting']:
                cache_info += f", {len(mixed_counts['uncached_drives'])} to count (~{mixed_counts['estimated_uncached']} est)"
                debug_log(f"Mixed mode: {cache_info}, total initial: {estimated_total}")
                self.overall_progress.setFormat(f"%p% - %v of ~{estimated_total:,} files (mixed cache)")
            else:
                debug_log(f"All drives cached/weighted: {cache_info}, total: {estimated_total}")
                self.overall_progress.setFormat(f"%p% - %v of {estimated_total:,} files (cached/weighted)")
        else:
            # Fallback to standard estimates (shouldn't happen now, but just in case)
            estimated_total = 0
            for drive in drives:
                if 'C:' in drive.upper():
                    estimated_total += 200000  # System drive estimate
                else:
                    estimated_total += 50000   # Other drives estimate
            debug_log(f"Fallback to estimated total: {estimated_total} files")
            self.overall_progress.setFormat(f"%p% - %v of ~{estimated_total:,} files (estimating...)")
            mixed_counts['needs_counting'] = True  # Force counting in fallback case
        
        self.total_files_to_process = estimated_total
        self.overall_progress.setMaximum(estimated_total)
        
        debug_log("Starting file counting worker thread")
        # Start file counting worker (will only count uncached drives if mixed_counts provided)
        if mixed_counts['needs_counting']:
            self.count_worker = FileCountWorker(drives, mixed_counts=mixed_counts)
            self.count_worker.drive_counted.connect(self.on_drive_counted, Qt.ConnectionType.QueuedConnection)
            self.count_worker.counting_finished.connect(self.on_counting_finished, Qt.ConnectionType.QueuedConnection)
            
            def count_run():
                debug_log("File counting thread execution started")
                self.count_worker.count_files()
                debug_log("File counting thread execution finished")
            
            self.count_thread = threading.Thread(target=count_run, daemon=True)
            self.count_thread.start()
            debug_log("File counting thread started for uncached drives")
        else:
            debug_log("All drives cached - skipping file counting")
            # Set running count to current cached total
            self.running_file_count = mixed_counts['total_cached']

        debug_log("Starting search worker thread")
        # Start search worker immediately with estimated totals
        self.worker = SearchWorker(drives)
        self.worker.set_total_files(estimated_total)  # Set estimated total immediately
        self.worker.update_progress.connect(self.update_progress, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self.on_scan_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.progressive_save.connect(self.on_progressive_save, Qt.ConnectionType.QueuedConnection)
        self.worker.drive_completed.connect(self.on_drive_completed, Qt.ConnectionType.QueuedConnection)
        self.worker.save_progress.connect(self.on_save_progress, Qt.ConnectionType.QueuedConnection)
        self.worker.save_countdown.connect(self.on_save_countdown, Qt.ConnectionType.QueuedConnection)

        # Connect signals for progress communication between workers
        if self.count_worker:
            self.worker.request_updated_count.connect(self.count_worker.provide_current_estimate, Qt.ConnectionType.QueuedConnection)
            self.count_worker.updated_count_response.connect(self.on_updated_count_received, Qt.ConnectionType.QueuedConnection)
        
        def scan_run():
            debug_log("Search thread execution started")
            self.worker.scan()
            debug_log("Search thread execution finished")
        
        self.search_thread = threading.Thread(target=scan_run, daemon=True)
        self.search_thread.start()
        debug_log("Text detection scan thread started")

    def on_drive_counted(self, drive, file_count):
        """Called when file counting completes for a drive"""
        debug_log(f"Drive {drive} counting completed: {file_count} files")
        
        # Update the per-drive maximum with the actual counted value
        self.drive_max_files[drive] = file_count
        debug_log(f"Updated drive {drive} maximum to {file_count} files")
        
        # Update running total and save incrementally
        self.running_file_count += file_count
        debug_log(f"Running file count total: {self.running_file_count}")
        
        # Save incremental cache after each drive (both global and per-drive)
        self.save_cached_file_count(self.running_file_count)
        self.save_per_drive_cached_count(drive, file_count)
        
    def on_updated_count_received(self, updated_count):
        """Called when file counter provides an updated estimate due to search worker request"""
        debug_log(f"Received updated count from file counter: {updated_count}")
        
        # Get current progress to validate the updated count
        current_value = self.overall_progress.value() if self.overall_progress else 0
        current_max = self.overall_progress.maximum() if self.overall_progress else 0
        
        # Fallback: If updated count is lower than current processed files, 
        # use current processed + buffer instead
        if updated_count < current_value:
            fallback_count = current_value + 5000  # Add buffer for remaining files
            debug_log(f"WARNING: Updated count {updated_count} < processed {current_value}, using fallback: {fallback_count}")
            updated_count = fallback_count
        
        # Only update if the new count is actually larger than current maximum
        if self.overall_progress and updated_count > current_max:
            self.overall_progress.setMaximum(updated_count)
            self.total_files_to_process = updated_count
            self.overall_progress.setFormat(f"%p% - %v of {updated_count:,} files (updated estimate)")
            debug_log(f"Updated progress maximum from search worker request: {updated_count}")
            
            # Update the search worker with the new estimate
            if self.worker:
                self.worker.set_total_files(updated_count)
        else:
            debug_log(f"Not updating progress - updated count {updated_count} not greater than current max {current_max}")
        
    def on_counting_finished(self, total_files):
        """Called when file counting across all drives is complete - updates real totals"""
        debug_log(f"File counting completed: {total_files} total files (replacing estimates)")
        
        # Update totals with the real count
        self.total_files_to_process = total_files
        if self.overall_progress:
            current_value = self.overall_progress.value()
            self.overall_progress.setMaximum(total_files)
            self.overall_progress.setFormat(f"%p% - %v of {total_files:,} files")
            
            # Keep current progress value if it's reasonable, otherwise reset to 0
            if current_value <= total_files:
                self.overall_progress.setValue(current_value)
            else:
                debug_log(f"Progress value {current_value} > new max {total_files}, resetting to 0")
                self.overall_progress.setValue(0)
        
        self.status_lbl.setText("File counting complete - real totals now available!")
        
        # Save final file count (this may be redundant with incremental saves, but ensures accuracy)
        self.save_cached_file_count(total_files)
        
        # Update the search worker with real totals (it's already running)
        if self.worker:
            self.worker.set_total_files(total_files)
            debug_log("Updated running search worker with real file count")
    
    def on_drive_completed(self, drive, files_found, files_processed):
        """Called when scanning completes for a drive"""
        debug_log(f"Drive {drive} scan completed: {files_found} text files found from {files_processed} processed")
        
        # Thread safety check
        if not self.drive_progress:
            return
        
        # Mark drive as completed in tracking
        self.drive_status[drive] = 'completed'
        
        # Set drive progress to 100% when drive completes (only if it's the currently displayed drive)
        if drive == self.current_drive:
            self.drive_progress.setValue(100)
            self.drive_progress.setFormat(f"Drive {drive} completed - {files_found} text files found!")
            debug_log(f"Drive progress set to 100% for completed drive {drive}")
        
        # Log completion status for all drives
        completed_drives = [d for d, status in self.drive_status.items() if status == 'completed']
        debug_log(f"Drive completion status: {len(completed_drives)} completed drives: {completed_drives}")
        debug_log(f"Full drive tracking: {self.get_drive_tracking_summary()}")
    
    def on_save_progress(self, save_count, save_description):
        """Called when a save operation completes"""
        debug_log(f"Save progress update: {save_count} saves completed - {save_description}")
        
        # Thread safety check
        if not self.save_progress:
            return
            
        # Reset save progress bar when save completes
        self.save_progress.setValue(0)
        self.save_progress.setFormat(f"Save #{save_count} completed: {save_description}")
    
    def on_save_countdown(self, dirs_until_save, seconds_until_save, total_saves):
        """Called to update countdown to next save"""
        # Thread safety check
        if not self.save_progress:
            return
            
        # Show progress toward next save trigger
        dir_progress = max(0, 100 - int((dirs_until_save / 100) * 100))  # PROGRESSIVE_SAVE_BATCH_SIZE = 100
        time_progress = max(0, 100 - int((seconds_until_save / 30) * 100))  # PROGRESSIVE_SAVE_TIME_INTERVAL = 30
        
        # Use the higher progress (whichever trigger is closer)
        save_progress = max(dir_progress, time_progress)
        self.save_progress.setValue(save_progress)
        
        # Show which trigger is closer
        if dirs_until_save <= seconds_until_save:
            self.save_progress.setFormat(f"Next save in {dirs_until_save} dirs ({total_saves} saves done)")
        else:
            self.save_progress.setFormat(f"Next save in {seconds_until_save}s ({total_saves} saves done)")

    def resume_scan(self):
        """Resume scanning from previous progress files"""
        debug_log("resume_scan() called")
        if self.search_thread and self.search_thread.is_alive():
            debug_log("Scan already running - skipping resume")
            self.status_lbl.setText("Scan already running.")
            return
        
        # Load previous progress - try per-drive files first, then legacy
        all_detected_files = []
        all_parsed_dirs = []
        
        drives = [self.dir_list.item(i).text() for i in range(self.dir_list.count()) if self.dir_list.item(i).isSelected()]
        if not drives:
            debug_log("No drives selected")
            self.status_lbl.setText("No drives selected.")
            return
        
        try:
            # Try to load per-drive progress files
            per_drive_loaded = False
            for drive in drives:
                drive_safe = drive.replace(':', '').replace('\\', '')
                results_file = f"{RESULTS_FILE}.{drive_safe}.progress"
                dirs_file = f"{PARSED_DIRS_FILE}.{drive_safe}.progress"
                
                if os.path.exists(results_file) and os.path.exists(dirs_file):
                    with open(results_file, 'r', encoding='utf-8') as f:
                        drive_detected_files = json.load(f)
                    with open(dirs_file, 'r', encoding='utf-8') as f:
                        drive_parsed_dirs = json.load(f)
                    
                    all_detected_files.extend(drive_detected_files)
                    all_parsed_dirs.extend(drive_parsed_dirs)
                    per_drive_loaded = True
                    debug_log(f"Loaded progress for drive {drive}: {len(drive_detected_files)} files, {len(drive_parsed_dirs)} dirs")
            
            # If no per-drive files found, try legacy combined files
            if not per_drive_loaded:
                if os.path.exists(f"{RESULTS_FILE}.progress") and os.path.exists(f"{PARSED_DIRS_FILE}.progress"):
                    with open(f"{RESULTS_FILE}.progress", 'r', encoding='utf-8') as f:
                        all_detected_files = json.load(f)
                    with open(f"{PARSED_DIRS_FILE}.progress", 'r', encoding='utf-8') as f:
                        all_parsed_dirs = json.load(f)
                    debug_log(f"Loaded legacy progress: {len(all_detected_files)} files, {len(all_parsed_dirs)} dirs")
                else:
                    raise FileNotFoundError("No progress files found")
            
            debug_log(f"Total loaded progress: {len(all_detected_files)} files, {len(all_parsed_dirs)} dirs")
            
        except Exception as e:
            debug_log(f"ERROR: Could not load progress files: {e}")
            self.status_lbl.setText("Error: Could not load progress files.")
            return
        
        debug_log(f"Resuming scan for drives: {drives}")
        self.status_lbl.setText("Resuming scan with parallel counting and detection...")
        self.overall_progress.setValue(0)
        self.drive_progress.setValue(0)
        self.results_list.clear()
        self.running_file_count = 0  # Reset running count for resumed scan
        
        # Initialize per-drive tracking dictionaries for selected drives
        self._initialize_drive_tracking(drives)
        
        # Try to restore previous drive tracking state
        drive_tracking_restored = self.load_drive_tracking_state(drives)
        if drive_tracking_restored:
            debug_log("Restored previous drive tracking state from save files")
        else:
            debug_log("No previous drive tracking state found, using default initialization")
        
        # Add previous results to display
        for f in all_detected_files:
            self.results_list.addItem(f)
        
        # Set initial file counts - check for mixed per-drive caches first
        mixed_counts = self.load_per_drive_cached_counts(drives)
        
        # Set per-drive maximum values from cached or estimated counts (if not already restored)
        for drive in drives:
            if self.drive_max_files.get(drive, 0) <= 0:  # Only set if not restored from tracking state
                if drive in mixed_counts['cached_drives']:
                    self.drive_max_files[drive] = mixed_counts['cached_drives'][drive]
                else:
                    # Use default estimates for uncached drives
                    estimate = 200000 if 'C:' in drive.upper() else 50000
                    self.drive_max_files[drive] = estimate
                    
        debug_log(f"Resume: Set per-drive max files: {self.drive_max_files}")
        
        if mixed_counts['cached_drives']:
            # Use mixed counts approach
            estimated_total = mixed_counts['total_cached'] + mixed_counts['estimated_uncached']
            debug_log(f"Resume with mixed counts: {len(mixed_counts['cached_drives'])} cached drives, {len(mixed_counts['uncached_drives'])} to count")
            
            if mixed_counts['needs_counting']:
                self.overall_progress.setFormat(f"%p% - %v of ~{estimated_total:,} remaining files (mixed cache)")
            else:
                self.overall_progress.setFormat(f"%p% - %v of {estimated_total:,} remaining files (all cached)")
        else:
            # Fall back to legacy cache approach
            cached_result = self.load_cached_file_count()
            if isinstance(cached_result, tuple) and cached_result[0] == "initial":
                # User chose to use cached count as initial but still recount
                cached_count = cached_result[1]
                remaining_files = max(cached_count - len(all_detected_files), 1000)
                debug_log(f"Resume using legacy cached count as initial: {cached_count}, remaining: {remaining_files}, will recount")
                estimated_total = remaining_files
                self.overall_progress.setFormat(f"%p% - %v of ~{estimated_total:,} remaining files (cached initial)")
            elif isinstance(cached_result, int):
                # User chose to use cached count without recounting
                cached_count = cached_result
                remaining_files = max(cached_count - len(all_detected_files), 1000)
                debug_log(f"Resume using legacy cached count: {cached_count}, remaining: {remaining_files}")
                estimated_total = remaining_files
                self.overall_progress.setFormat(f"%p% - %v of ~{estimated_total:,} remaining files (cached total)")
            else:
                # Use estimated counts for remaining files
                estimated_total = 0
                for drive in drives:
                    if 'C:' in drive.upper():
                        estimated_total += 150000  # Reduced estimate for resume (some already scanned)
                    else:
                        estimated_total += 30000   # Reduced estimate for other drives
                debug_log(f"Resume using estimated remaining: {estimated_total} files until counting completes")
                self.overall_progress.setFormat(f"%p% - %v of ~{estimated_total:,} remaining files (estimating...)")
        
        self.total_files_to_process = estimated_total
        self.overall_progress.setMaximum(estimated_total)
        
        # Start file counting worker (for remaining/uncached files)
        if mixed_counts['needs_counting'] or not mixed_counts['cached_drives']:
            self.count_worker = FileCountWorker(drives, mixed_counts=mixed_counts if mixed_counts['cached_drives'] else None)
            self.count_worker.drive_counted.connect(self.on_drive_counted, Qt.ConnectionType.QueuedConnection)
            self.count_worker.counting_finished.connect(self.on_counting_finished, Qt.ConnectionType.QueuedConnection)
            
            def count_run():
                self.count_worker.count_files()
            self.count_thread = threading.Thread(target=count_run, daemon=True)
            self.count_thread.start()
        else:
            debug_log("All drives cached for resume - skipping file counting")
            self.running_file_count = mixed_counts['total_cached']
        
        # Start search worker immediately with resume data and estimated totals
        self.worker = SearchWorker(drives, resume_data={
            'detected_files': all_detected_files,
            'parsed_dirs': all_parsed_dirs
        })
        self.worker.set_total_files(estimated_total)  # Set estimated total immediately
        self.worker.update_progress.connect(self.update_progress, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self.on_scan_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.progressive_save.connect(self.on_progressive_save, Qt.ConnectionType.QueuedConnection)
        self.worker.drive_completed.connect(self.on_drive_completed, Qt.ConnectionType.QueuedConnection)
        self.worker.save_progress.connect(self.on_save_progress, Qt.ConnectionType.QueuedConnection)
        self.worker.save_countdown.connect(self.on_save_countdown, Qt.ConnectionType.QueuedConnection)
        
        # Connect signals for progress communication between workers
        if self.count_worker:
            self.worker.request_updated_count.connect(self.count_worker.provide_current_estimate, Qt.ConnectionType.QueuedConnection)
            self.count_worker.updated_count_response.connect(self.on_updated_count_received, Qt.ConnectionType.QueuedConnection)
        
        def scan_run():
            self.worker.scan()
        self.search_thread = threading.Thread(target=scan_run, daemon=True)
        self.search_thread.start()
        
        debug_log("Resume: Both counting and scanning threads started")

    def update_progress(self, current_dir, files_processed, total_files, current_drive):
        """Updated progress method for dual progress bars with proper per-drive tracking"""
        # Thread safety check
        if not self.overall_progress or not self.drive_progress:
            return
            
        # Update overall progress with simple overflow protection
        if total_files > 0:
            # Simply clamp the progress value to maximum to prevent >100%
            current_max = self.overall_progress.maximum()
            safe_value = min(files_processed, current_max)
            overall_percentage = min(100, int((safe_value / current_max) * 100))
            self.overall_progress.setValue(safe_value)
        else:
            overall_percentage = 0
            
        # Initialize drive tracking if this is the first time we see this drive
        if current_drive not in self.drive_start_counts:
            self.drive_start_counts[current_drive] = files_processed
            self.drive_files_processed[current_drive] = 0
            self.drive_status[current_drive] = 'scanning'
            self.drive_progress.setValue(0)
            self.drive_progress.setFormat(f"Scanning {current_drive} - Starting...")
            debug_log(f"Drive progress initialized for {current_drive}, starting at overall file {files_processed}")
        
        # Update current drive (for UI display purposes)
        self.current_drive = current_drive
        
        # Calculate files processed in current drive only
        self.drive_files_processed[current_drive] = files_processed - self.drive_start_counts[current_drive]
        
        # Use actual per-drive maximum if available, otherwise fall back to estimates
        drive_max_files = self.drive_max_files.get(current_drive, 0)
        if drive_max_files <= 0:
            # Fallback to estimates if no maximum set
            if 'C:' in current_drive.upper():
                drive_max_files = 200000  # System drive estimate
            else:
                drive_max_files = 50000   # Other drives estimate
            self.drive_max_files[current_drive] = drive_max_files
            debug_log(f"Using fallback estimate for {current_drive}: {drive_max_files}")
            
        # Calculate drive progress percentage (cap at 95% until drive completes)
        current_drive_processed = self.drive_files_processed[current_drive]
        if current_drive_processed > 0 and drive_max_files > 0:
            drive_progress_pct = min(95, int((current_drive_processed / drive_max_files) * 100))
            self.drive_progress.setValue(drive_progress_pct)
            self.drive_progress.setFormat(f"Scanning {current_drive} - {current_drive_processed:,}/{drive_max_files:,} files ({drive_progress_pct}%)")
        elif current_drive_processed > 0:
            # Show progress without percentage if no maximum known
            self.drive_progress.setFormat(f"Scanning {current_drive} - {current_drive_processed:,} files")
        
        # Update status
        if total_files > 0:
            # Check if we're still using estimates (format contains "~" or "estimating")
            current_format = self.overall_progress.format()
            if "~" in current_format or "estimating" in current_format:
                self.status_lbl.setText(f"Scanning: {current_dir} (~{overall_percentage}% estimated - {files_processed:,} of ~{total_files:,} files)")
            else:
                self.status_lbl.setText(f"Scanning: {current_dir} ({overall_percentage}% complete - {files_processed:,} of {total_files:,} files)")
        else:
            self.status_lbl.setText(f"Scanning: {current_dir}")

    def on_scan_finished(self, detected_files, parsed_dirs):
        debug_log(f"Scan finished - {len(detected_files)} files found, {len(parsed_dirs)} directories processed")
        self.status_lbl.setText(f"Scan done. Found {len(detected_files)} text files.")
        self.overall_progress.setValue(self.overall_progress.maximum())
        self.drive_progress.setValue(100)
        self.drive_progress.setFormat("All drives completed!")
        for f in detected_files:
            self.results_list.addItem(f)
        # Save results
        debug_log(f"Saving final results to {RESULTS_FILE}")
        with open(RESULTS_FILE, 'w', encoding='utf-8') as jf:
            json.dump(detected_files, jf, indent=2)
        # Find "topmost" parsed dirs: only directories whose parent not in list
        parsed_dirs = [Path(d) for d in parsed_dirs]
        topmost = []
        seen = set()
        for d in parsed_dirs:
            parent_in = any(str(d).startswith(str(other) + os.sep) for other in parsed_dirs if other != d)
            if not parent_in:
                topmost.append(str(d))
        debug_log(f"Saving {len(topmost)} topmost directories to {PARSED_DIRS_FILE}")
        with open(PARSED_DIRS_FILE, 'w', encoding='utf-8') as jf:
            json.dump(topmost, jf, indent=2)
        debug_log("Final save complete")

    def on_progressive_save(self, detected_files, parsed_dirs):
        """Progressive save during scanning - saves current state to background files"""
        debug_log(f"Progressive save triggered - {len(detected_files)} files, {len(parsed_dirs)} dirs")
        
        # Thread safety check
        if not self.save_progress or not self.status_lbl:
            return
            
        # Create progressive/backup filenames
        progressive_results_file = f"{RESULTS_FILE}.progress"
        progressive_dirs_file = f"{PARSED_DIRS_FILE}.progress"
        
        # Save current detected files
        try:
            with open(progressive_results_file, 'w', encoding='utf-8') as jf:
                json.dump(detected_files, jf, indent=2)
            debug_log(f"Saved progress files to {progressive_results_file}")
            
            # Reset save progress bar to show save completed
            self.save_progress.setValue(100)
            self.save_progress.setFormat(f"Progressive save completed: {len(detected_files)} files")
            
        except Exception as e:
            debug_log(f"ERROR: Could not save progressive results: {e}")
            print(f"Warning: Could not save progressive results: {e}")
        
        # Find and save topmost directories for current state
        try:
            parsed_paths = [Path(d) for d in parsed_dirs]
            topmost = []
            for d in parsed_paths:
                parent_in = any(str(d).startswith(str(other) + os.sep) for other in parsed_paths if other != d)
                if not parent_in:
                    topmost.append(str(d))
            
            with open(progressive_dirs_file, 'w', encoding='utf-8') as jf:
                json.dump(topmost, jf, indent=2)
            debug_log(f"Saved {len(topmost)} progress directories to {progressive_dirs_file}")
            
            # Save drive tracking state as well
            drives = list(self.drive_status.keys())
            if drives:
                self.save_drive_tracking_state(drives)
            
            # Keep save progress bar at 100% briefly to show completion
            self.save_progress.setValue(100)
            self.save_progress.setFormat(f"Auto-save complete: {len(detected_files)} files, {len(topmost)} dirs")
                
            # Optional: Update status to show progressive save happened
            current_status = self.status_lbl.text()
            if "Scanning:" in current_status:
                self.status_lbl.setText(f"{current_status} [Saved: {len(detected_files)} files, {len(topmost)} dirs]")
        except Exception as e:
            debug_log(f"ERROR: Could not save progressive directories: {e}")
            print(f"Warning: Could not save progressive directories: {e}")

# Parse flags passed to the script
def parse_flags(flags):
    for flag in flags:
        if flag.startswith("--disable-console-progress"):
            toggle_log("progress", "console", False)
        elif flag.startswith("--enable-console-progress"):
            toggle_log("progress", "console", True)
        elif flag.startswith("--disable-file-debug"):
            toggle_log("debug", "file", False)
        elif flag.startswith("--enable-file-debug"):
            toggle_log("debug", "file", True)

# Toggle log settings
def toggle_log(category, output_type, enabled):
    if category in LOG_SETTINGS[output_type]:
        LOG_SETTINGS[output_type][category] = enabled
        debug_log(f"Log setting updated: {output_type} {category} set to {enabled}")
    else:
        debug_log(f"Invalid log category or output type: {output_type} {category}")

if __name__ == "__main__":
    import sys
    parse_flags(sys.argv[1:])

    debug_log("=== Drive Text Searcher Starting ===")
    debug_log(f"Python version: {sys.version}")
    debug_log(f"Current working directory: {os.getcwd()}")
    debug_log(f"Settings: MIN_FILE_SIZE={MIN_FILE_SIZE}, PROGRESSIVE_SAVE_BATCH_SIZE={PROGRESSIVE_SAVE_BATCH_SIZE}")
    debug_log(f"Settings: PROGRESSIVE_SAVE_TIME_INTERVAL={PROGRESSIVE_SAVE_TIME_INTERVAL}s")

    app = QApplication(sys.argv)
    debug_log("QApplication created")
    win = MainWindow()
    debug_log("MainWindow created")
    win.show()
    debug_log("MainWindow shown - entering event loop")
    result = app.exec()
    debug_log(f"Application exiting with code: {result}")
    sys.exit(result)