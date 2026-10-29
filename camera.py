import time
import requests
import subprocess
import os
import sys
import signal
import zmq
import json
import paramiko
import numpy as np
from datetime import datetime
from PIL import Image
import config as c
import tifffile
# from camera_zmq import OutputZMQ

camera_dict = {
    "take_image": {"command": "accumulate", "nframes": 10, "filename": "test_img"},
    "get_status": {"command": "status"}
}

class Camera:
    def __init__(self):
        # self.rpiIp = "10.116.9.108"
        # self.rpiUrl = f"http://{self.rpiIp}:9898//picamhq"

        self.rpiIp = "127.0.0.1"
        self.rpiUrl = f"http://{self.rpiIp}:9898//picamhq"
        self.camera_server_script_path = f"/home/{c.MICROSCOPE_USERNAME}/project_files/camera_zmq.py"
        self.zmqSocket = None
        self.zmqContext = zmq.Context()

        self.start_zmq()

        self.status = None

    # Function to start the camera server
    def start_camera_server(self):
        # Starts the camera_zmq.py script as a separate background process.

        print(f"Attempting to start camera server: {self.camera_server_script_path}")
        try:
            # Use subprocess.Popen to run the script in the background.
            camera_process = subprocess.Popen(['python3', self.camera_server_script_path],
                                              # Remove stdout/stderr redirection temporarily for debugging
                                              # stdout=subprocess.DEVNULL,
                                              # stderr=subprocess.DEVNULL,
                                              preexec_fn=os.setsid)
            print(f"Camera server started (PID: {camera_process.pid}). Giving it time to initialize...", flush=True)
            time.sleep(15)  # Give the server time to bind its socket and start the camera

            return camera_process
        except FileNotFoundError:
            print(f"Error: Camera server script not found at {self.camera_server_script_path}. Check path.", flush=True)
            return None
        except Exception as e:
            print(f"An unexpected error occurred while starting camera server: {e}", flush=True)
            return None

    # --- Function to stop the camera server gracefully ---
    def stop_camera_server(self, process: subprocess.Popen):
        # Attempts to gracefully stop the background camera server process.

        if process and process.poll() is None: # Check if the process is still running
            print("Attempting to stop camera server process...", flush = True)
            try:
                # Send SIGINT (Ctrl+C equivalent) to the process group.
                os.killpg(process.pid, signal.SIGINT)

                # Wait a bit for the process to terminate gracefully
                process.wait(timeout=10)
                print("Camera server terminated gracefully.", flush = True)
            except subprocess.TimeoutExpired:
                print("Camera server did not terminate gracefully within timeout. Forcing kill.", flush = True)
                os.killpg(process.pid, signal.SIGTERM) # Send SIGTERM if it doesn't respond to SIGINT
                process.wait(timeout=5)
            except Exception as e:
                print(f"Error stopping camera server: {e}", flush = True)
        else:
            print("Camera server was not running or already stopped.", flush = True)

    def start_zmq(self):
        self.zmqSocket = self.zmqContext.socket(zmq.REQ)
        self.zmqSocket.RCVTIMEO = 500
        self.zmqSocket.connect(f"tcp://{self.rpiIp}:9898")

    def send_command(self, msg):
        print(msg)
        response = None
        try:
            if self.zmqSocket is not None:
                self.zmqSocket.send_string(json.dumps(msg))
                response = json.loads(self.zmqSocket.recv().decode())
        except zmq.error.Again:
            self.zmqSocket = None
        except zmq.error.ZMQError:
            self.zmqSocket = None

        if self.zmqSocket is None:
            response = {"timeout": "controller is busy"}
            # self.start_zmq()

        return response

    def take_rpi_image(self, nframes: int, filename: str, file_path: str = f"{c.PI_IMAGE_DIR}", z_height=None, magnification=None):
        print(f"\nCommanding camera server to accumulate {nframes} images...", flush=True)
        response = self.send_command(
            {"command": "accumulate", "nframes": nframes, "filename": filename, "file_path": file_path, "z_height": z_height, "magnification": magnification}
        )
        # Surface rejections/timeouts: if this is not a "started" reply, no
        # image will be captured.
        print(f"Camera server reply: {response}", flush=True)
        return response

    # --- Continuous series capture ---------------------------------------------
    # Start-and-poll protocol: start_series() returns once the camera service has
    # accepted (or rejected) the request; poll series_status() until the reply's
    # "is_terminal" is true. Acceptance does not mean the images are saved.

    def _send_series_command(self, msg):
        # send_command() drops its socket after a timeout and returns a
        # "timeout" reply. Reconnect here so one slow reply does not end series
        # polling; the legacy send_command() behavior is left unchanged.
        if self.zmqSocket is None:
            self.start_zmq()
        return self.send_command(msg)

    def start_series(self, image_count, nframes, filename, file_path, z_height=None,
                     magnification=None, metadata=None, queue_capacity=None, width=None):
        """Ask the camera service to start a continuous series capture.

        Returns the service reply: {"ok": True, "series_id": ..., "state": ...} on
        acceptance, or {"ok": False, "error": {...}} if rejected. A reply with a
        "timeout" key means the service did not answer in time.
        """
        msg = {
            "command": "start_series",
            "image_count": image_count,
            "nframes": nframes,
            "filename": filename,
            "file_path": file_path,
            "z_height": z_height,
            "magnification": magnification,
            "metadata": metadata or {},
        }
        # Omit optional values so the service's defaults (queue capacity 3,
        # active width 4056) apply.
        if queue_capacity is not None:
            msg["queue_capacity"] = queue_capacity
        if width is not None:
            msg["width"] = width
        return self._send_series_command(msg)

    def series_status(self, series_id):
        """Return the camera service's status snapshot for a series."""
        return self._send_series_command({"command": "series_status", "series_id": series_id})

    def cancel_series(self, series_id):
        """Stop a series. Images already captured and queued are still written."""
        return self._send_series_command({"command": "cancel_series", "series_id": series_id})

    # --- Frame-delivery diagnostics ---------------------------------------------
    # Records libcamera sequence numbers, sensor timestamps and callback times
    # for every frame delivered to the camera service, to find dropped frames.

    def start_frame_diagnostics(self, label=None):
        """Start recording per-frame diagnostics in the camera service."""
        return self._send_series_command({"command": "start_frame_diagnostics", "label": label})

    def frame_diagnostics_status(self):
        """Summary of the current (or last) diagnostics session, without per-frame data."""
        return self._send_series_command({"command": "frame_diagnostics_status"})

    def stop_frame_diagnostics(self, output_dir=None):
        """Stop recording. The camera service writes a per-frame JSON file and
        returns its path and a summary. ``output_dir`` defaults to
        <PI_IMAGE_DIR>/frame_diagnostics on the camera host."""
        msg = {"command": "stop_frame_diagnostics"}
        if output_dir is not None:
            msg["output_dir"] = output_dir
        return self._send_series_command(msg)

    # --- Repeated series (batch) -------------------------------------------------
    # One request runs series_count series of image_count images. The camera
    # service schedules the pause between series from sensor timestamps, so it
    # does not depend on client polling or on the writer finishing.

    def start_series_batch(self, image_count, series_count, nframes, filename, file_path,
                           inter_series_pause_us=0, z_height=None, magnification=None,
                           metadata=None, queue_capacity=None, width=None):
        """Ask the camera service to start a batch of repeated series.

        ``inter_series_pause_us`` is the minimum sensor-timestamp gap, in
        microseconds, from the last frame of one series to the first frame of
        the next. Returns {"ok": True, "batch_id": ..., ...} on acceptance,
        {"ok": False, "error": {...}} if rejected, or a "timeout" reply.
        """
        msg = {
            "command": "start_series_batch",
            "image_count": image_count,
            "series_count": series_count,
            "nframes": nframes,
            "filename": filename,
            "file_path": file_path,
            "inter_series_pause_us": inter_series_pause_us,
            "z_height": z_height,
            "magnification": magnification,
            "metadata": metadata or {},
        }
        if queue_capacity is not None:
            msg["queue_capacity"] = queue_capacity
        if width is not None:
            msg["width"] = width
        return self._send_series_command(msg)

    def series_batch_status(self, batch_id):
        """Return the camera service's status snapshot for a batch."""
        return self._send_series_command({"command": "series_batch_status", "batch_id": batch_id})

    def cancel_series_batch(self, batch_id):
        """Stop a batch; no further series start. Queued images are still written."""
        return self._send_series_command({"command": "cancel_series_batch", "batch_id": batch_id})

    def get_status(self):
        status = self.send_command({"command": "status"})
        print(f"\n{status}")

    def set_exposure_time(self, exposure_time):
        print(f"\nSetting Exposure Time to {exposure_time}")
        self.send_command({"command": "exposureTime", "exposureTime" : exposure_time})

    #def get_focus_score(self):
        #score = self.send_command({"command": "getFocus"})
        #print(type(score))
        #print(score)
        #return score["focus_score"]

    def get_focus_score(self):
        score1 = self.send_command({"command": "getFocus"})
        time.sleep(1)
        score2 = self.send_command({"command": "getFocus"})
        time.sleep(1)
        score3 = self.send_command({"command": "getFocus"})

        print(f"Focus scores: {score1['focus_score']}, {score2['focus_score']}, {score3['focus_score']}")

        score = max(score1['focus_score'], score2['focus_score'], score3['focus_score'])
        return score

    def update_latest_image_to_jpg(self, tif_path):
        try:
            # Ensure the file exists
            if not os.path.exists(tif_path):
                raise FileNotFoundError(f"TIF file not found: {tif_path}")

            # Read the .tif image
            img = tifffile.imread(tif_path)

            # Normalize if necessary (in case image is 12-bit or 16-bit)
            if img.dtype != np.uint8:
                img = (img / img.max() * 255).astype(np.uint8)

            # Convert grayscale to RGB for compatibility with browsers
            if img.ndim == 2:
                img = np.stack([img] * 3, axis=-1)

            # Define output path (does not overwrite the .tif)
            output_path = os.path.join(c.PI_IMAGE_DIR, "latest_image.jpg")

            # Save as JPEG
            Image.fromarray(img).save(output_path, "JPEG", quality=85)

            return output_path

        except Exception as e:
            print(f"Error converting TIF to JPEG: {e}")
            return None

if __name__ == "__main__":
   imager = Camera()
   #imager.set_exposure_time(30000)
   #time.sleep(1)

   apt = 2.5
   correction = "light"

   filename = f"no-{correction}_20261023_M1_{apt}APTR"
   folder_name = f"no-{correction}_20261023_M1_1frame"
   file_path = os.path.join(c.PI_IMAGE_DIR, f"{folder_name}")
   imager.take_rpi_image(1, filename, file_path, None, 20)
   time.sleep(5)

   filename = f"no-{correction}_20261023_M1_{apt}APTR"
   folder_name = f"no-{correction}_20261023_M1_100frame"
   file_path = os.path.join(c.PI_IMAGE_DIR, f"{folder_name}")
   imager.take_rpi_image(100, filename, file_path, None, 20)
   time.sleep(15)
