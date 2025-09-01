import json
from datetime import datetime
from pathlib import Path

# File path to persist the log
LOG_FILE = Path("/home/microscope_auto/project_files/folder_name_log.json")

# In-memory log
log = []

# Load existing log from disk at startup
if LOG_FILE.exists():
    try:
        with open(LOG_FILE, "r") as f:
            log = json.load(f)
    except json.JSONDecodeError:
        log = []

def save_log():
    """Save the in-memory log to disk."""
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)

def add_entry(folder_name):
    """Add a folder name with current date to the log."""
    entry = {"folder_name": folder_name, "date": datetime.today().strftime("%Y%m%d")}
    log.insert(0, entry)  # newest first
    save_log()

def clear_last_entry():
    """Remove the most recent entry from the log."""
    global log
    if log:
        log.pop(0)  # remove the first (newest) entry
        save_log()

def clear_log():
    """Empty the log."""
    global log
    log = []
    save_log()
