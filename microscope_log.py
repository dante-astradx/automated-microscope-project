import os
import sys
import time
import threading
import queue
from datetime import datetime
import config as c

# --- Globals shared across app ---
_log_queue = queue.Queue()        # thread-safe queue for streaming
_file_queue = queue.Queue()       # thread-safe queue for file writes
_status_message = None
_stop_event = threading.Event()   # signal for stopping writer thread
_scoreboard_lock = threading.Lock()
_UNSET = object()
_scoreboard_state = {
    "barcode": None,
    "smear": None,
    "fov": None,
    "status": "idle",
    "updated_at": None,
}

# --- Where logs are stored ---
LOG_DIR = f"/home/{c.MICROSCOPE_USERNAME}/project_files"
BASE_LOG_NAME = "microscope_log.txt"

def _current_log_path():
    """Return full path for today's log file with YYYY-MM-DD suffix."""
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"{BASE_LOG_NAME}.{today}")

# --- Background file writer thread ---
def _log_writer():
    """Continuously write log messages from _file_queue to file."""
    while not _stop_event.is_set():
        try:
            message = _file_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(_current_log_path(), "a") as f:
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{timestamp}] {message}\n")
        except Exception as e:
            # push error to in-memory queue so it's visible on UI
            _log_queue.put(f"LOGGING ERROR: {str(e)}")
        finally:
            _file_queue.task_done()

# Start background thread immediately
_thread = threading.Thread(target=_log_writer, daemon=True)
_thread.start()

# --- Logging functions ---
def log_output(message: str):
    """
    Log a message to both the in-memory queue and the rotating daily log file.
    Called by Motor, FileTransfer, and Flask routes.
    """
    # Add to in-memory queue (for SSE streaming to browser)
    _log_queue.put(message)

    # Queue for background file writing
    _file_queue.put(message)

def log_to_file_only(message: str):
    """
    Write a message only to the daily log file (not the web terminal).
    """
    _file_queue.put(message)

def update_status(message: str):
    """
    Update the global status message and also log it.
    Use this for any status updates shown on the web UI.
    """
    global _status_message
    _status_message = message
    log_output(f"STATUS: {message}")

def get_status_message():
    """Return the latest status message for display in UI and /status route."""
    return _status_message

def get_log_queue():
    """Return the in-memory log queue (used by /stream SSE)."""
    items = []
    while not _log_queue.empty():
        try:
            items.append(_log_queue.get_nowait())
        except queue.Empty:
            break
    return items

def _utc_now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")

def get_scoreboard_state():
    """Return a snapshot of the scoreboard state for API responses."""
    with _scoreboard_lock:
        return dict(_scoreboard_state)

def update_scoreboard(barcode=_UNSET, smear=_UNSET, fov=_UNSET, status=_UNSET):
    """Update one or more scoreboard fields; pass None explicitly to clear a field."""
    with _scoreboard_lock:
        if barcode is not _UNSET:
            _scoreboard_state["barcode"] = barcode
        if smear is not _UNSET:
            _scoreboard_state["smear"] = smear
        if fov is not _UNSET:
            _scoreboard_state["fov"] = fov
        if status is not _UNSET:
            _scoreboard_state["status"] = status
        _scoreboard_state["updated_at"] = _utc_now_iso()

def reset_scoreboard():
    """Reset scoreboard to idle defaults."""
    with _scoreboard_lock:
        _scoreboard_state["barcode"] = None
        _scoreboard_state["smear"] = None
        _scoreboard_state["fov"] = None
        _scoreboard_state["status"] = "idle"
        _scoreboard_state["updated_at"] = _utc_now_iso()

# --- Redirect stdout/stderr to log file ---
class StdoutLogger:
    def __init__(self):
        self.terminal = sys.__stdout__

    def write(self, message):
        if message.strip():
            #log_to_file_only(message)
            log_output(message)

    def flush(self):
        pass  # required for compatibility

# Redirect all prints and errors to daily log file (optional)
#sys.stdout = StdoutLogger()
#sys.stderr = StdoutLogger()

# --- Cleanup function for app shutdown ---
def shutdown_logging():
    """Stop background logging thread cleanly."""
    _stop_event.set()
    _thread.join(timeout=1)
