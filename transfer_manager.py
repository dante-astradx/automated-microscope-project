import threading
import queue
import time
import os
from milestone5_file_transfer import FileTransfer5
from microscope_log import log_output
from config import PI_IMAGE_DIR

_transfer_queue = queue.Queue()
_worker = None


class TransferWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()

    def run(self):
        log_output("TransferWorker: started")

        while not self.stop_event.is_set():
            try:
                item = _transfer_queue.get(timeout=1)
            except queue.Empty:
                continue

            folder_path, barcode = item

            if not folder_path or not os.path.exists(folder_path):
                log_output(f"TransferWorker: folder path missing or removed: {folder_path}")
                _transfer_queue.task_done()
                continue

            try:
                file_transfer = FileTransfer5(logger=log_output)
                file_transfer.set_barcode(barcode)

                # Ensure folder_path is a directory and potentially add a marker check
                if not os.path.isdir(folder_path):
                    log_output(f"TransferWorker: not a directory: {folder_path}")
                    _transfer_queue.task_done()
                    continue

                # Build a relative path from PI_IMAGE_DIR to use with upload_to_laptop_rsync.
                relative_folder_path = os.path.relpath(folder_path, PI_IMAGE_DIR)

                remote_path_prefix = file_transfer.get_rsync_path(file_transfer.extract_prefix(barcode))
                remote_path_suffix = os.path.dirname(relative_folder_path)
                remote_path = os.path.join(remote_path_prefix, remote_path_suffix)

                log_output(f"TransferWorker: uploading {relative_folder_path} to {remote_path}")

                # Use upload_to_laptop_rsync and cleanup later as desired.
                file_transfer.upload_to_laptop_rsync(relative_folder_path, remote_path, delete_files=False)

                log_output(f"TransferWorker: upload complete for {folder_path}")

            except Exception as e:
                log_output(f"TransferWorker: error transferring {folder_path}: {e}")

            finally:
                _transfer_queue.task_done()

        log_output("TransferWorker: stopped")

    def stop(self):
        self.stop_event.set()


def start_worker():
    global _worker
    if _worker is not None and _worker.is_alive():
        return _worker

    _worker = TransferWorker()
    _worker.start()
    return _worker


def stop_worker():
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker.join(timeout=10)
        _worker = None


def enqueue_folder(folder_path, barcode):
    """Add a folder and barcode to the transfer queue."""
    if not folder_path or not barcode:
        log_output("enqueue_folder: invalid folder_path or barcode")
        return

    if not os.path.exists(folder_path):
        log_output(f"enqueue_folder: path does not exist {folder_path}")
        return

    if os.path.isfile(folder_path):
        log_output(f"enqueue_folder: path is a file not directory {folder_path}")
        return

    log_output(f"enqueue_folder: queued {folder_path} for barcode {barcode}")
    _transfer_queue.put((folder_path, barcode))


def queue_size():
    return _transfer_queue.qsize()
