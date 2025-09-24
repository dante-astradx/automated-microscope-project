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
import config as c
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

    def take_rpi_image(self, nframes: int, filename: str, file_path: str = f"{c.PI_IMAGE_DIR}"):
        print(f"\nCommanding camera server to accumulate {nframes} images...", flush=True)
        self.send_command(
            {"command": "accumulate", "nframes": nframes, "filename": filename, "file_path": file_path}
        )
        time.sleep(1)

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

if __name__ == "__main__":
   imager = Camera()
   #imager.set_exposure_time(11000)
   #time.sleep(5)

   filename = "no-light_10x_20250919_M1"
   #file_path = os.path.join(c.PI_IMAGE_DIR, "no-light_20250919_M1", "no-light_20250919_M1_10x")
   imager.take_rpi_image(10, filename)
   time.sleep(10)
