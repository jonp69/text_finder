import sys
import os
import re
import json
import threading
import time
from pathlib import Path
from queue import Queue
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QWidget, QPushButton,
    QLabel, QListWidget, QProgressBar, QFileDialog, QHBoxLayout, QCheckBox
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer

# Debug logging function
def debug_log(message):
    """Print debug messages with timestamp"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] DEBUG: {message}")
    sys.stdout.flush()  # Ensure immediate output

# ------------- Settings -------------
MIN_FILE_SIZE = 256  # bytes
RESULTS_FILE = "detected_text_files.json"
PARSED_DIRS_FILE = "parsed_directories.json"
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
    
    def __init__(self, drives, parent=None):
        super().__init__(parent)
        self.drives = drives
        self._abort = False
        
    def abort(self):
        self._abort = True
        
    def count_files(self):
        """Count all potential files across all drives"""
        debug_log("Starting file counting operation")
        total_files = 0
        
        for drive in self.drives:
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
                                total_files += 1
                        except Exception:
                            continue
                            
            except Exception as e:
                debug_log(f"Error counting files on drive {drive}: {e}")
                continue
                
            drive_duration = time.time() - drive_start_time
            debug_log(f"Drive {drive} counting complete: {drive_file_count} files in {drive_duration:.1f}s")
            self.drive_counted.emit(drive, drive_file_count)
            
        debug_log(f"File counting completed - Total: {total_files} potential files")
        self.counting_finished.emit(total_files)

class SearchWorker(QObject):
    update_progress = Signal(str, int, int, str)  # current_dir, files_processed, total_files, current_drive
    drive_completed = Signal(str, int, int)  # drive, files_found, files_processed
    finished = Signal(list, list)  # detected_files, parsed_dirs
    progressive_save = Signal(list, list)  # detected_files, parsed_dirs for progressive save
    save_progress = Signal(int, str)  # save_count, save_description
    save_countdown = Signal(int, int, int)  # dirs_until_save, seconds_until_save, total_saves

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
        
        debug_log(f"SearchWorker initialized for drives: {drives}")
        if self.resume_data:
            debug_log(f"Resume mode: {len(self.resume_data.get('detected_files', []))} files, {len(self.resume_data.get('parsed_dirs', []))} dirs")

    def set_total_files(self, total_files):
        """Set the total number of files to process (from counting worker)"""
        self.total_files_to_process = total_files
        debug_log(f"SearchWorker received total file count: {total_files}")

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
        
        completed_dirs_since_save = 0
        
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
                completed_dirs_since_save += 1
                
                if files_in_dir > 0:
                    debug_log(f"Found {files_in_dir} text files in {root}")
                
                # Progressive save based on batch size or time interval
                current_time = time.time()
                should_save = (
                    completed_dirs_since_save >= PROGRESSIVE_SAVE_BATCH_SIZE or
                    (current_time - self.last_save_time) >= PROGRESSIVE_SAVE_TIME_INTERVAL
                )
                
                if should_save:
                    debug_log(f"Triggering progressive save - {len(detected_files)} files, {len(parsed_dirs)} dirs")
                    self.progressive_save.emit(detected_files.copy(), parsed_dirs.copy())
                    # Also save per-drive progress
                    self._save_drive_progress(drive, 
                                            self.per_drive_data[drive]['detected_files'].copy(),
                                            self.per_drive_data[drive]['parsed_dirs'].copy())
                    completed_dirs_since_save = 0
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
        self.status_lbl.setText("Starting file counting...")
        self.overall_progress.setValue(0)
        self.drive_progress.setValue(0)
        self.results_list.clear()
        
        # Start file counting worker first
        self.count_worker = FileCountWorker(drives)
        self.count_worker.drive_counted.connect(self.on_drive_counted)
        self.count_worker.counting_finished.connect(self.on_counting_finished)
        
        def count_run():
            self.count_worker.count_files()
        self.count_thread = threading.Thread(target=count_run, daemon=True)
        self.count_thread.start()
        debug_log("File counting thread started")
        
        # Prepare search worker (will start after counting finishes)
        self.worker = SearchWorker(drives)
        self.worker.update_progress.connect(self.update_progress)
        self.worker.finished.connect(self.on_scan_finished)
        self.worker.progressive_save.connect(self.on_progressive_save)
        self.worker.drive_completed.connect(self.on_drive_completed)
        self.worker.save_progress.connect(self.on_save_progress)
        
        debug_log("Search worker prepared, waiting for file count completion")

    def on_drive_counted(self, drive, file_count):
        """Called when file counting completes for a drive"""
        debug_log(f"Drive {drive} counting completed: {file_count} files")
        
    def on_counting_finished(self, total_files):
        """Called when file counting across all drives is complete"""
        debug_log(f"File counting completed: {total_files} total files")
        self.total_files_to_process = total_files
        self.overall_progress.setMaximum(total_files)
        self.overall_progress.setFormat(f"%p% - %v of {total_files:,} files")
        self.status_lbl.setText("File counting complete. Starting text detection scan...")
        
        # Now start the actual scanning
        if self.worker:
            self.worker.set_total_files(total_files)
            def scan_run():
                self.worker.scan()
            self.search_thread = threading.Thread(target=scan_run, daemon=True)
            self.search_thread.start()
            debug_log("Text detection scan thread started")
    
    def on_drive_completed(self, drive, files_found, files_processed):
        """Called when scanning completes for a drive"""
        debug_log(f"Drive {drive} scan completed: {files_found} text files found from {files_processed} processed")
        # Reset drive progress bar for next drive
        self.drive_progress.setValue(0)
        self.drive_progress.setFormat("Drive completed - Next drive...")
    
    def on_save_progress(self, save_count, save_description):
        """Called when a save operation completes"""
        debug_log(f"Save progress update: {save_count} saves completed - {save_description}")
        # Update save progress bar - use a rolling scale to show activity
        progress_value = min(100, (save_count * 10) % 100)
        self.save_progress.setValue(progress_value)
        self.save_progress.setFormat(f"Save #{save_count}: {save_description}")

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
        self.status_lbl.setText("Resuming scan - counting remaining files...")
        self.overall_progress.setValue(0)
        self.drive_progress.setValue(0)
        self.results_list.clear()
        
        # Add previous results to display
        for f in all_detected_files:
            self.results_list.addItem(f)
        
        # Start file counting worker first (for remaining files)
        self.count_worker = FileCountWorker(drives)
        self.count_worker.drive_counted.connect(self.on_drive_counted)
        self.count_worker.counting_finished.connect(self.on_counting_finished)
        
        def count_run():
            self.count_worker.count_files()
        self.count_thread = threading.Thread(target=count_run, daemon=True)
        self.count_thread.start()
        
        # Threading: move worker off main with resume data
        self.worker = SearchWorker(drives, resume_data={
            'detected_files': all_detected_files,
            'parsed_dirs': all_parsed_dirs
        })
        self.worker.update_progress.connect(self.update_progress)
        self.worker.finished.connect(self.on_scan_finished)
        self.worker.progressive_save.connect(self.on_progressive_save)
        self.worker.drive_completed.connect(self.on_drive_completed)
        self.worker.save_progress.connect(self.on_save_progress)
        
        debug_log("Resume counting thread started")

    def update_progress(self, current_dir, files_processed, total_files, current_drive):
        """Updated progress method for dual progress bars"""
        # Update overall progress
        if total_files > 0:
            overall_percentage = min(100, int((files_processed / total_files) * 100))
            self.overall_progress.setValue(files_processed)
        else:
            overall_percentage = 0
            
        # Update current drive progress (estimate based on directory scanning)
        if current_drive != self.current_drive:
            self.current_drive = current_drive
            self.drive_progress.setValue(0)
            self.drive_progress.setFormat(f"Scanning {current_drive} - Starting...")
        
        # Simple directory-based progress for current drive (this is approximate)
        drive_progress = (files_processed % 1000) / 10  # Rough estimate
        self.drive_progress.setValue(int(drive_progress))
        self.drive_progress.setFormat(f"Scanning {current_drive}...")
        
        # Update status
        if total_files > 0:
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
        # Create progressive/backup filenames
        progressive_results_file = f"{RESULTS_FILE}.progress"
        progressive_dirs_file = f"{PARSED_DIRS_FILE}.progress"
        
        # Save current detected files
        try:
            with open(progressive_results_file, 'w', encoding='utf-8') as jf:
                json.dump(detected_files, jf, indent=2)
            debug_log(f"Saved progress files to {progressive_results_file}")
            
            # Update save progress bar for progressive save
            self.save_progress.setValue(50)
            self.save_progress.setFormat(f"Progressive: {len(detected_files)} files saved")
            
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
            
            # Update save progress bar completion
            self.save_progress.setValue(100)
            self.save_progress.setFormat(f"Progressive: {len(detected_files)} files, {len(topmost)} dirs saved")
                
            # Optional: Update status to show progressive save happened
            current_status = self.status_lbl.text()
            if "Scanning:" in current_status:
                self.status_lbl.setText(f"{current_status} [Auto-saved]")
        except Exception as e:
            debug_log(f"ERROR: Could not save progressive directories: {e}")
            print(f"Warning: Could not save progressive directories: {e}")

if __name__ == "__main__":
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