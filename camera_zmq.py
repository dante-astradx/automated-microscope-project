import base64
import cv2
import json
from picamera2 import Picamera2
from picamera2.encoders import Encoder
from picamera2.outputs import Output
import numpy as np
import signal
import tifffile
import time
import zmq
import os
from datetime import datetime
import config as c
import image_analysis_c as iac
import analysis as a

class OutputZMQ(Output):
    """
    Camera output handler for ZMQ control.
    """

    def __init__(self, size=None, verbose=None, **kwargs):
        super().__init__(**kwargs)
        self.accN = None
        self.accT = None
        self.cnt = 0
        self.im = None
        self.size = (size[1], size[0])
        self.startTime = None
        self.verbose = verbose
        self.X = None
        self.XX = None

        self.current_tif_path = None
        self.current_json_path = None
        self.save_in_progress = False

    def accumulate(self, accT, filename_base: str, folder_path: str):
        """
        Start frame accumulation process.
        """
        if self.accT is None:
            if self.verbose:
                print("starting accumulation")
            self.accN = 0
            self.accT = accT
            self.startTime = time.time()
            self.X = None
            self.XX = None

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.current_tif_path = os.path.join(folder_path, f"{filename_base}.tif")
            self.current_json_path = os.path.join(folder_path, f"{filename_base}.json")

            if self.verbose:
                print(f"Accumlation target filename set to {self.current_tif_path}")

    def accumulating(self):
        return (self.accT is not None) and (self.accN < self.accT) and (not self.save_in_progress)

    def done(self):
        return (self.n == self.nframes)

    def get_accumulated_im(self):
        X = self.X.astype(float)
        XX = self.XX.astype(float)
        pixelMean = X / self.accN
        pixelVar = XX / self.accN - pixelMean * pixelMean
        return [pixelMean, pixelVar]

    def get_centered_im(self):
        cim = self.im[::2, 1::2]
        c0 = cim.shape[0] // 2
        c1 = cim.shape[1] // 2
        w0 = 380 // 2  # Matches reduced size.
        w1 = 507 // 2
        cim = cim[c0 - w0:c0 + w0, c1 - w1:c1 + w1]
        cim = np.right_shift(cim, 4).astype(np.uint8)
        return cim

    def get_reduced_im(self):
        rim = self.im[:, :self.size[1]]
        rim = rim[::2, 1::2]
        rim = cv2.resize(rim, (0, 0), fx=0.25, fy=0.25)
        rim = np.right_shift(rim, 4).astype(np.uint8)
        return rim

    def get_status(self):
        return {"accumulating": self.accumulating(),
                "accumulatingN": self.accN,
                "accumulatingT": self.accT,
                "saving": self.save_in_progress}

    # NEW METHOD: Returns the path to the last saved accumulated TIFF and JSON
    def get_last_saved_filepaths(self):
        # This will return the dynamically generated filename used for the last save
        # along with the base paths on home/picamera/Images
        return {
            "tif_path": self.current_tif_path,
            "json_path": self.current_json_path
        }

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        self.cnt += 1
        self.im = np.frombuffer(frame, dtype=np.uint16)
        self.im = np.reshape(self.im, (self.size[0], -1))

        # Update accumulators if we are accumulating.
        if self.accT is not None:
            if (self.accN < self.accT):
                if (self.X is None):
                    self.X = np.zeros(self.im.shape, dtype=np.uint32)
                    self.XX = np.zeros_like(self.X)
                iac.update_x_xx(self.X, self.XX, self.im)
                self.accN += 1
            else:
                self.save_accumulated()
                self.accN = None
                self.accT = None
                # self.current_tif_path = None # clear filename after saving
                # self.current_json_path = None

    def save_accumulated(self):
        self.save_in_progress = True
        if self.verbose:
            print("Starting file saving process...")

        [pmean, pvar] = self.get_accumulated_im()
        with tifffile.TiffWriter(self.current_tif_path) as tfw:  # Use dynamic filename
            tfw.write(pmean.astype(np.float32))
            tfw.write(pvar.astype(np.float32))

        with open(self.current_json_path, "w") as fp:  # Use dynamic filename
            json.dump({"camera": "rpi-hq",
                       "frames": self.accT,
                       "time": time.time() - self.startTime,
                       "filename_used": os.path.basename(self.current_tif_path)},
                      fp)  # Record final filename in metadata

        self.save_in_progress = False
        if self.verbose:
            print(f"Accumulation finished. Saved to {self.current_tif_path}")

running = True

def camera_zmq(verbose = True):
    global running

    # Configure ZMQ.
    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.bind("tcp://127.0.0.1:9898")

    # Configure and start camera.
    picam2 = Picamera2()

    size = (4056, 3040) # HQ camera size.
    config = picam2.create_video_configuration(raw = {'format' : 'SRGGB12', 'size' : (4056, 3040)})
    picam2.configure(config)
    picam2.set_controls({"ExposureTime" : 11000, "AnalogueGain" : 1.0})
    picam2.encode_stream_name = "raw"

    encoder = Encoder()
    output = OutputZMQ(size = size, verbose = False)

    picam2.start()
    time.sleep(1)

    if verbose:
        metadata = picam2.capture_metadata()
        controls = {c: metadata[c] for c in ["ExposureTime", "AnalogueGain"]}
        print(controls)

    picam2.start_encoder(encoder, output)


    start = time.time()
    print("Running")
    while (running):
        if socket.poll(10, zmq.POLLIN):
            msg = json.loads(socket.recv().decode())
            if verbose:
                # print(msg)
                pass

            try:
                if (msg["command"] == "accumulate"):
                    filename_from_client = msg.get("filename", "default_image")
                    folder_path_from_client = msg.get("file_path", f"{c.PI_IMAGE_DIR}")

                    if (not output.accumulating()):
                        output.accumulate(int(msg["nframes"]), filename_base=filename_from_client, folder_path=folder_path_from_client)
                        socket.send_string(json.dumps({"accumulating": "started"}))
                    else:
                        socket.send_string(json.dumps({"accumulating": "already running"}))

                elif (msg["command"] == "centered"):
                    im = output.get_centered_im()
                    fs = a.get_focus_score(im)
                    success, im = cv2.imencode('.jpeg', im)
                    im = base64.b64encode(im).decode()
                    socket.send_string(json.dumps({"image": im, "focus_score": fs}))

                elif (msg["command"] == "getFocus"):
                    im = output.get_centered_im()
                    fs = a.get_focus_score(im)
                    socket.send_string(json.dumps({"focus_score" : fs}))

                elif (msg["command"] == "exposureTime"):
                    picam2.set_controls({"ExposureTime": int(msg["exposureTime"])})
                    socket.send_string(json.dumps({"handled": msg["command"]}))

                elif (msg["command"] == "reduced"):
                    im = output.get_reduced_im()
                    success, im = cv2.imencode('.jpeg', im)
                    im = base64.b64encode(im).decode()
                    socket.send_string(json.dumps({"image": im}))

                elif (msg["command"] == "status"):
                    metadata = picam2.capture_metadata()
                    controls = {c: metadata[c] for c in ["ExposureTime", "AnalogueGain"]}
                    print(controls, output.get_status())
                    socket.send_string(json.dumps(controls | output.get_status()))

                # NEW COMMAND HANDLER: To get the path of the last saved image
                elif (msg["command"] == "get_image_path"):
                    filepaths = output.get_last_saved_filepaths()
                    socket.send_string(json.dumps({"handled": msg["command"], "filepaths": filepaths}))

                # NEW COMMAND HANDLER: To check if saving is complete
                elif (msg["command"] == "is_save_complete"):
                    # Report True if NOT save_in_progress, False if save is still happening
                    socket.send_string(json.dumps({"save_complete": not output.save_in_progress}))

                elif (msg["command"] == "stop"):
                    running = False
                    socket.send_string(json.dumps({"handled": msg["command"]}))

                else:
                    socket.send_string(json.dumps({"unrecognized": msg["command"]}))
            except KeyError:
                socket.send_string(json.dumps({"key error in": msg}))

        time.sleep(0.1)
    stop = time.time()

    if verbose:
        n = output.cnt
        print("Acquired {0:d} frames at {1:.1f} FPS".format(n, n / (stop - start)))

    # Stop camera.
    picam2.stop_encoder()
    picam2.stop()

def sig_int_handler(signum, frame):
    global running
    running = False


if (__name__ == "__main__"):
    import sys

    signal.signal(signal.SIGINT, sig_int_handler)
    camera_zmq()
    

