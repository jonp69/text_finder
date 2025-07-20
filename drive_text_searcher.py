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

class SearchWorker(QObject):
    update_progress = Signal(str, int)
    finished = Signal(list, list)  # detected_files, parsed_dirs
    progressive_save = Signal(list, list)  # detected_files, parsed_dirs for progressive save

    def __init__(self, drives, parent=None):
        super().__init__(parent)
        self.drives = drives
        self._abort = False
        self.last_save_time = time.time()
        debug_log(f"SearchWorker initialized for drives: {drives}")

    def abort(self):
        self._abort = True
        debug_log("SearchWorker abort requested")

    def scan(self):
        debug_log("Starting scan operation")
        detected_files = []
        parsed_dirs = []
        completed_dirs_since_save = 0
        total_files_processed = 0
        
        for drive in self.drives:
            debug_log(f"Beginning scan of drive: {drive}")
            drive_start_time = time.time()
            
            for root, dirs, files in os.walk(drive):
                if self._abort:
                    debug_log("Scan aborted by user")
                    return
                if is_system_path(root):
                    dirs[:] = []  # don't descend
                    debug_log(f"Skipping system path: {root}")
                    continue
                
                self.update_progress.emit(root, len(parsed_dirs))
                
                # Process all files in current directory
                files_in_dir = 0
                for fname in files:
                    total_files_processed += 1
                    if self._abort:
                        debug_log("Scan aborted during file processing")
                        return
                    fpath = os.path.join(root, fname)
                    try:
                        if os.path.getsize(fpath) < MIN_FILE_SIZE:
                            continue
                        if is_text_file(fpath):
                            detected_files.append(fpath)
                            files_in_dir += 1
                    except Exception as e:
                        debug_log(f"Error processing file {fpath}: {e}")
                        continue
                
                # Directory is now fully processed
                parsed_dirs.append(root)
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
                    completed_dirs_since_save = 0
                    self.last_save_time = current_time
            
            drive_duration = time.time() - drive_start_time
            debug_log(f"Completed drive {drive} in {drive_duration:.1f}s")
                    
        debug_log(f"Scan completed - Total: {len(detected_files)} text files from {total_files_processed} files processed")
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
        debug_log("MainWindow initialization complete")

    def _build_ui(self):
        central = QWidget()
        vbox = QVBoxLayout()
        h1 = QHBoxLayout()
        self.dir_label = QLabel("Drives to scan:")
        self.dir_list = QListWidget()
        self.refresh_drives_btn = QPushButton("Refresh Drives")
        self.refresh_drives_btn.clicked.connect(self.load_drives)
        self.start_btn = QPushButton("Start Scan")
        self.start_btn.clicked.connect(self.start_scan)
        h1.addWidget(self.dir_label)
        h1.addWidget(self.dir_list)
        h1.addWidget(self.refresh_drives_btn)
        h1.addWidget(self.start_btn)
        vbox.addLayout(h1)
        self.progress = QProgressBar()
        self.progress.setMaximum(100)
        vbox.addWidget(self.progress)
        self.status_lbl = QLabel("Ready.")
        vbox.addWidget(self.status_lbl)
        self.results_list = QListWidget()
        vbox.addWidget(self.results_list, 2)
        central.setLayout(vbox)
        self.setCentralWidget(central)
        self.load_drives()

    def load_drives(self):
        debug_log("Loading available drives...")
        self.dir_list.clear()
        drives = self.get_all_drives()
        debug_log(f"Found drives: {drives}")
        for d in drives:
            self.dir_list.addItem(d)
            self.dir_list.item(self.dir_list.count()-1).setSelected(True)
        debug_log(f"Loaded {len(drives)} drives to UI")

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
        self.status_lbl.setText("Scanning...")
        self.progress.setValue(0)
        self.results_list.clear()
        # Threading: move worker off main
        self.worker = SearchWorker(drives)
        self.worker.update_progress.connect(self.update_progress)
        self.worker.finished.connect(self.on_scan_finished)
        self.worker.progressive_save.connect(self.on_progressive_save)
        def run():
            self.worker.scan()
        self.search_thread = threading.Thread(target=run, daemon=True)
        self.search_thread.start()
        debug_log("Scan thread started")

    def update_progress(self, current_dir, n):
        self.status_lbl.setText(f"Scanning: {current_dir}")
        self.progress.setValue(min(100, n % 100))

    def on_scan_finished(self, detected_files, parsed_dirs):
        debug_log(f"Scan finished - {len(detected_files)} files found, {len(parsed_dirs)} directories processed")
        self.status_lbl.setText(f"Scan done. Found {len(detected_files)} files.")
        self.progress.setValue(100)
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
                
            # Optional: Update status to show progressive save happened
            current_status = self.status_lbl.text()
            if "Scanning:" in current_status:
                self.status_lbl.setText(f"{current_status} [Saved: {len(detected_files)} files, {len(topmost)} dirs]")
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