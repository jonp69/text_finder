import sys
import os
import re
import json
import threading
import time
from pathlib import Path
from queue import Queue

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QWidget, QPushButton,
    QLabel, QListWidget, QProgressBar, QFileDialog, QHBoxLayout, QCheckBox
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer

# ------------- Settings -------------
SCAN_INTERVAL_SEC = 60 * 10  # 10 minutes
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

    def abort(self):
        self._abort = True

    def scan(self):
        detected_files = []
        parsed_dirs = []
        completed_dirs_since_save = 0
        
        for drive in self.drives:
            for root, dirs, files in os.walk(drive):
                if self._abort:
                    return
                if is_system_path(root):
                    dirs[:] = []  # don't descend
                    continue
                
                self.update_progress.emit(root, len(parsed_dirs))
                
                # Process all files in current directory
                for fname in files:
                    if self._abort:
                        return
                    fpath = os.path.join(root, fname)
                    try:
                        if os.path.getsize(fpath) < MIN_FILE_SIZE:
                            continue
                        if is_text_file(fpath):
                            detected_files.append(fpath)
                    except Exception:
                        continue
                
                # Directory is now fully processed
                parsed_dirs.append(root)
                completed_dirs_since_save += 1
                
                # Progressive save based on batch size or time interval
                current_time = time.time()
                should_save = (
                    completed_dirs_since_save >= PROGRESSIVE_SAVE_BATCH_SIZE or
                    (current_time - self.last_save_time) >= PROGRESSIVE_SAVE_TIME_INTERVAL
                )
                
                if should_save:
                    self.progressive_save.emit(detected_files.copy(), parsed_dirs.copy())
                    completed_dirs_since_save = 0
                    self.last_save_time = current_time
                    
        self.finished.emit(detected_files, parsed_dirs)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
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
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.start_scan)

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
        self.auto_check = QCheckBox("Auto-scan every 10 min")
        self.auto_check.stateChanged.connect(self.toggle_timer)
        h1.addWidget(self.dir_label)
        h1.addWidget(self.dir_list)
        h1.addWidget(self.refresh_drives_btn)
        h1.addWidget(self.start_btn)
        h1.addWidget(self.auto_check)
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
        self.dir_list.clear()
        drives = self.get_all_drives()
        for d in drives:
            self.dir_list.addItem(d)
            self.dir_list.item(self.dir_list.count()-1).setSelected(True)

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
        if self.search_thread and self.search_thread.is_alive():
            self.status_lbl.setText("Scan already running.")
            return
        drives = [self.dir_list.item(i).text() for i in range(self.dir_list.count()) if self.dir_list.item(i).isSelected()]
        if not drives:
            self.status_lbl.setText("No drives selected.")
            return
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

    def update_progress(self, current_dir, n):
        self.status_lbl.setText(f"Scanning: {current_dir}")
        self.progress.setValue(min(100, n % 100))

    def on_scan_finished(self, detected_files, parsed_dirs):
        self.status_lbl.setText(f"Scan done. Found {len(detected_files)} files.")
        self.progress.setValue(100)
        for f in detected_files:
            self.results_list.addItem(f)
        # Save results
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
        with open(PARSED_DIRS_FILE, 'w', encoding='utf-8') as jf:
            json.dump(topmost, jf, indent=2)

    def on_progressive_save(self, detected_files, parsed_dirs):
        """Progressive save during scanning - saves current state to background files"""
        # Create progressive/backup filenames
        progressive_results_file = f"{RESULTS_FILE}.progress"
        progressive_dirs_file = f"{PARSED_DIRS_FILE}.progress"
        
        # Save current detected files
        try:
            with open(progressive_results_file, 'w', encoding='utf-8') as jf:
                json.dump(detected_files, jf, indent=2)
        except Exception as e:
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
                
            # Optional: Update status to show progressive save happened
            current_status = self.status_lbl.text()
            if "Scanning:" in current_status:
                self.status_lbl.setText(f"{current_status} [Saved: {len(detected_files)} files, {len(topmost)} dirs]")
        except Exception as e:
            print(f"Warning: Could not save progressive directories: {e}")

    def toggle_timer(self, state):
        if state:
            self.timer.start(SCAN_INTERVAL_SEC * 1000)
            self.status_lbl.setText("Auto-scan enabled.")
        else:
            self.timer.stop()
            self.status_lbl.setText("Auto-scan disabled.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())