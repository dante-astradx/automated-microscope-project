import base64
import cv2
import json
from picamera2 import Picamera2
from picamera2.encoders import Encoder
from picamera2.outputs import Output
import numpy as np
import signal
import tifffile
import threading
import time
import traceback
import zmq
import os
from datetime import datetime
import config as c
import continuous_capture as cc
import frame_diagnostics as fd
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
        self.current_base_path = None
        self.rel_tif_path = None
        self.save_in_progress = False
        self.exposure_time = None
        self.analogue_gain = None
        self.z_height = None
        self.magnification = None

        # Continuous series capture, set by camera_zmq(). Separate from the
        # legacy accumulator above.
        self.series_manager = None

        # Optional frame-delivery diagnostics, set by camera_zmq(). Records
        # nothing unless a diagnostics session has been started.
        self.frame_diagnostics = None

    def accumulate(self, accT, filename_base: str, folder_path: str, exposure_time=None, analogue_gain=None, z_height=None, magnification=None):
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
            self.exposure_time = exposure_time
            self.analogue_gain = analogue_gain

            if magnification is not None:
                self.magnification = f"{magnification}x"
            else:
                self.magnification = None
                
            self.z_height = z_height

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.current_base_path = os.path.dirname(folder_path)
            self.current_tif_path = os.path.join(folder_path, f"{filename_base}.tif")
            self.current_json_path = os.path.join(folder_path, f"{filename_base}.json")
            self.rel_tif_path = os.path.relpath(self.current_tif_path, self.current_base_path)

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
        diagnostics = self.frame_diagnostics
        if diagnostics is not None and diagnostics.active:
            started = time.perf_counter()
            mode = self._frame_mode()
            if self._outputframe(frame, timestamp):
                mode = fd.MODE_LEGACY_SAVE
            diagnostics.record_callback(timestamp, time.perf_counter() - started, mode)
        else:
            self._outputframe(frame, timestamp)

    def _frame_mode(self):
        """Which capture path this frame is for (diagnostics only)."""
        if self.accT is not None:
            return fd.MODE_LEGACY
        if self.series_manager is not None and self.series_manager.is_active():
            return fd.MODE_SERIES
        return fd.MODE_IDLE

    def _outputframe(self, frame, timestamp):
        """Process one frame. Returns True if the legacy TIFF was saved in this call."""
        self.cnt += 1
        self.im = np.frombuffer(frame, dtype=np.uint16)
        self.im = np.reshape(self.im, (self.size[0], -1))

        # Continuous series capture. The series consumes the frame before this
        # callback returns and keeps no reference to the camera buffer.
        if self.series_manager is not None:
            self.series_manager.on_frame(self.im, timestamp)

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
                return True
        return False

    def save_accumulated(self):
        self.save_in_progress = True
        if self.verbose:
            print("Starting file saving process...")

        [pmean, pvar] = self.get_accumulated_im()
        with tifffile.TiffWriter(self.current_tif_path) as tfw:  # Use dynamic filename
            tfw.write(pmean.astype(np.float32))
            tfw.write(pvar.astype(np.float32))

        #rel_tif_path = os.path.relpath(self.current_tif_path, self.current_base_path)
        #if os.path.normpath(rel_tif_path) == ".":
            #rel_tif_path = None

        with open(self.current_json_path, "w") as fp:  # Use dynamic filename
            json.dump({"camera": c.CAMERA,
                       "sensor": c.SENSOR,
                       "lens_adapter": c.LENS_ADAPTER,
                       "light_source": c.LIGHT_SOURCE,
                       "exposure_time": self.exposure_time,
                       "analogue_gain": self.analogue_gain,
                       "frames": self.accT,
                       "time": time.time() - self.startTime,
                       "data_timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
                       "image_filename": os.path.basename(self.current_tif_path),
                       "image_filepath": self.rel_tif_path,
                       "magnification": self.magnification,
                       "z_height": self.z_height},
                      fp)  # Record final filename in metadata


        self.save_in_progress = False
        if self.verbose:
            print(f"Accumulation finished. Saved to {self.current_tif_path}")


# Protocol error codes for series commands. Capture/writer errors use the codes
# defined in continuous_capture.
ERROR_INVALID_REQUEST = "INVALID_REQUEST"
ERROR_BUSY = "BUSY"
ERROR_UNKNOWN_SERIES = "UNKNOWN_SERIES"
ERROR_CALLBACK = "CALLBACK_ERROR"
ERROR_SERVER = "SERVER_ERROR"


def _error_response(code, message, **extra):
    response = {"ok": False, "error": {"code": code, "message": message}}
    response.update(extra)
    return response


class SeriesManager:
    """Holds the active (or most recent) continuous series for the camera service.

    Only one series exists at a time. A finished series is kept, with its latched
    status and error, until the next series is started, so a failure can always
    be read by a later status poll. A series that has not reached a terminal state
    blocks new series requests; a terminal one does not.
    """

    def __init__(self, logger=print):
        self.logger = logger
        self._lock = threading.Lock()
        self._series = None
        self._callback_error = None

    def is_active(self):
        with self._lock:
            series = self._series
        return series is not None and not series.is_terminal()

    def _prepare_start(self, msg, legacy_busy, camera_metadata, required):
        """Shared start validation. Return (metadata, None) or (None, error response)."""
        if legacy_busy:
            return None, _error_response(ERROR_BUSY, "A legacy accumulate capture is in progress")
        if self.is_active():
            return None, _error_response(ERROR_BUSY, "A continuous series is already in progress")

        missing = [k for k in required if k not in msg]
        if missing:
            return None, _error_response(ERROR_INVALID_REQUEST, f"Missing required fields: {missing}")
        metadata = msg.get("metadata") or {}
        if not isinstance(metadata, dict):
            return None, _error_response(ERROR_INVALID_REQUEST, "metadata must be a JSON object")

        # Caller metadata first; the camera-reported exposure/gain and the
        # request's own fields are recorded in the legacy sidecar format.
        metadata = dict(metadata)
        magnification = msg.get("magnification")
        metadata.update({
            "exposure_time": camera_metadata.get("ExposureTime"),
            "analogue_gain": camera_metadata.get("AnalogueGain"),
            "magnification": f"{magnification}x" if magnification is not None else None,
            "z_height": msg.get("z_height"),
        })
        return metadata, None

    def _install(self, series):
        """Start ``series`` and make it current. Return an error response or None."""
        with self._lock:
            # Re-check under the lock in case another start raced this one.
            if self._series is not None and not self._series.is_terminal():
                return _error_response(ERROR_BUSY, "A continuous series is already in progress")
            series.start()
            self._series = series
            self._callback_error = None
        return None

    def start(self, msg, legacy_busy, camera_metadata):
        """Validate a start_series request and start the series. Return a response dict."""
        metadata, error = self._prepare_start(
            msg, legacy_busy, camera_metadata,
            ("image_count", "nframes", "file_path", "filename"),
        )
        if error:
            return error

        try:
            series = cc.ContinuousSeries(
                image_count=msg["image_count"],
                nframes=msg["nframes"],
                output_dir=msg["file_path"],
                base_filename=msg["filename"],
                metadata=metadata,
                queue_capacity=msg.get("queue_capacity", cc.DEFAULT_QUEUE_CAPACITY),
                width=msg.get("width", cc.DEFAULT_ACTIVE_WIDTH),
                logger=self.logger,
            )
        except (ValueError, TypeError) as e:
            return _error_response(ERROR_INVALID_REQUEST, str(e))

        error = self._install(series)
        if error:
            return error

        return {
            "ok": True,
            "series_id": series.series_id,
            "state": series.state,
            "requested_images": series.image_count,
            "nframes": series.nframes,
            "queue_capacity": series.queue_capacity,
        }

    def start_batch(self, msg, legacy_busy, camera_metadata):
        """Validate a start_series_batch request and start the batch.

        Runs ``series_count`` series of ``image_count`` images, with the camera
        enforcing at least ``inter_series_pause_us`` (sensor time) between the
        last frame of one series and the first frame of the next.
        """
        metadata, error = self._prepare_start(
            msg, legacy_busy, camera_metadata,
            ("image_count", "series_count", "nframes", "file_path", "filename"),
        )
        if error:
            return error

        try:
            batch = cc.ContinuousSeriesBatch(
                image_count=msg["image_count"],
                series_count=msg["series_count"],
                nframes=msg["nframes"],
                output_dir=msg["file_path"],
                base_filename=msg["filename"],
                metadata=metadata,
                inter_series_pause_us=msg.get("inter_series_pause_us", 0),
                queue_capacity=msg.get("queue_capacity", cc.DEFAULT_QUEUE_CAPACITY),
                width=msg.get("width", cc.DEFAULT_ACTIVE_WIDTH),
                logger=self.logger,
            )
        except (ValueError, TypeError) as e:
            return _error_response(ERROR_INVALID_REQUEST, str(e))

        error = self._install(batch)
        if error:
            return error

        return {
            "ok": True,
            "batch_id": batch.batch_id,
            "series_ids": list(batch.child_series_ids),
            "state": batch.state,
            "series_count": batch.series_count,
            "images_per_series": batch.images_per_series,
            "requested_images": batch.image_count,
            "nframes": batch.nframes,
            "inter_series_pause_us": batch.inter_series_pause_us,
            "queue_capacity": batch.queue_capacity,
        }

    def _find(self, msg, key="series_id", batch=False):
        series_id = msg.get(key)
        if not series_id:
            return None, _error_response(ERROR_INVALID_REQUEST, f"Missing required field: {key}")
        with self._lock:
            series = self._series
            callback_error = self._callback_error
        is_batch = isinstance(series, cc.ContinuousSeriesBatch)
        if series is None or series.series_id != series_id or (batch and not is_batch):
            noun = "batch" if batch else "series"
            return None, _error_response(
                ERROR_UNKNOWN_SERIES, f"No {noun} with id {series_id!r}", **{key: series_id}
            )
        return (series, callback_error), None

    def batch_status(self, msg):
        """Return the batch's status snapshot, or an error response."""
        return self.status(msg, key="batch_id", batch=True)

    def cancel_batch(self, msg):
        """Cancel the batch: no further series start; queued images are written."""
        found, error = self._find(msg, key="batch_id", batch=True)
        if error:
            return error
        batch, _ = found
        batch.cancel()
        return {"ok": True, "batch_id": batch.batch_id, "state": batch.state}

    def status(self, msg, key="series_id", batch=False):
        """Return the series' status snapshot, or an error response."""
        found, error = self._find(msg, key=key, batch=batch)
        if error:
            return error
        series, callback_error = found
        snapshot = series.status_snapshot()
        if callback_error is not None:
            # An unexpected callback exception must never read as success.
            if snapshot["error"] is None:
                snapshot["error"] = callback_error
            if snapshot["is_terminal"]:
                snapshot["state"] = cc.STATE_FAILED
        return snapshot

    def cancel(self, msg):
        """Cancel the series. Queued images are still written by the core."""
        found, error = self._find(msg)
        if error:
            return error
        series, _ = found
        series.cancel()
        return {"ok": True, "series_id": series.series_id, "state": series.state}

    def on_frame(self, frame, timestamp_us):
        """Forward one callback frame to the current series, if any.

        Called from the camera callback. The series consumes the frame before
        returning. Exceptions are caught so the shared camera callback, which the
        legacy path also uses, keeps running.
        """
        with self._lock:
            series = self._series
        if series is None:
            return
        try:
            series.add_frame(frame, timestamp_us)
        except Exception as e:
            self.logger(f"Continuous series frame error:\n{traceback.format_exc()}")
            with self._lock:
                if self._callback_error is None and self._series is series:
                    self._callback_error = {
                        "code": ERROR_CALLBACK,
                        "message": f"Frame handling failed: {type(e).__name__}: {e}",
                        "image_index": None,
                        "filename": None,
                    }
            series.cancel()

    def shutdown(self, timeout=30):
        """Stop capture and let the writer finish queued images before exit."""
        with self._lock:
            series = self._series
        if series is not None and not series.is_terminal():
            series.cancel()
            series.wait(timeout)


def handle_series_command(msg, output, series_manager, picam2):
    """Handle one continuous-series ZeroMQ command. Return the response dict."""
    command = msg["command"]
    if command == "start_series":
        # Exposure and gain actually in use, as the legacy accumulate path records.
        camera_metadata = picam2.capture_metadata()
        # The legacy capture is busy from accumulate() until its save finishes,
        # so check accT rather than accumulating(), which turns False early.
        return series_manager.start(msg, legacy_busy=output.accT is not None,
                                    camera_metadata=camera_metadata)
    if command == "series_status":
        return series_manager.status(msg)
    if command == "cancel_series":
        return series_manager.cancel(msg)
    if command == "start_series_batch":
        camera_metadata = picam2.capture_metadata()
        return series_manager.start_batch(msg, legacy_busy=output.accT is not None,
                                          camera_metadata=camera_metadata)
    if command == "series_batch_status":
        return series_manager.batch_status(msg)
    if command == "cancel_series_batch":
        return series_manager.cancel_batch(msg)
    raise ValueError(f"Not a series command: {command}")


SERIES_COMMANDS = frozenset({
    "start_series", "series_status", "cancel_series",
    "start_series_batch", "series_batch_status", "cancel_series_batch",
})

DIAGNOSTICS_COMMANDS = frozenset({"start_frame_diagnostics", "frame_diagnostics_status",
                                  "stop_frame_diagnostics"})
DEFAULT_DIAGNOSTICS_DIR = os.path.join(c.PI_IMAGE_DIR, "frame_diagnostics")


def _diagnostics_camera_info(picam2):
    """Camera settings worth recording with a diagnostics session."""
    info = {}
    try:
        metadata = picam2.capture_metadata()
        for key in ("ExposureTime", "AnalogueGain", "FrameDuration", "SensorTimestamp"):
            if key in metadata:
                info[key] = metadata[key]
    except Exception as e:
        info["metadata_error"] = f"{type(e).__name__}: {e}"
    try:
        config = picam2.camera_configuration() or {}
        info["buffer_count"] = config.get("buffer_count")
        info["configured_controls"] = {
            k: v for k, v in (config.get("controls") or {}).items()
            if k in ("FrameDurationLimits", "FrameRate", "NoiseReductionMode")}
    except Exception as e:
        info["configuration_error"] = f"{type(e).__name__}: {e}"
    return json.loads(json.dumps(info, default=str))


def handle_diagnostics_command(msg, diagnostics, picam2):
    """Handle one frame-diagnostics ZeroMQ command. Return the response dict."""
    command = msg["command"]
    if command == "start_frame_diagnostics":
        try:
            session_id = diagnostics.start(label=msg.get("label"),
                                           camera_info=_diagnostics_camera_info(picam2))
        except RuntimeError as e:
            return _error_response(ERROR_BUSY, str(e))
        return {"ok": True, "session_id": session_id, "camera_info": diagnostics.camera_info}
    if command == "frame_diagnostics_status":
        return {"ok": True, **diagnostics.status()}
    if command == "stop_frame_diagnostics":
        if diagnostics.session_id is None:
            return _error_response(ERROR_INVALID_REQUEST, "No diagnostics session has been started")
        output_dir = msg.get("output_dir") or DEFAULT_DIAGNOSTICS_DIR
        status, path = diagnostics.stop(output_dir=output_dir)
        return {"ok": True, "path": path, **status}
    raise ValueError(f"Not a diagnostics command: {command}")


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
    series_manager = SeriesManager()
    output.series_manager = series_manager

    # Frame diagnostics: post_callback runs on the camera thread for every
    # completed request, just before the encoder calls output.outputframe().
    diagnostics = fd.FrameDiagnostics()
    output.frame_diagnostics = diagnostics
    picam2.post_callback = diagnostics.on_request

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

                    if series_manager.is_active():
                        # A continuous series owns the camera; reject rather than
                        # silently ignoring the request.
                        socket.send_string(json.dumps({"accumulating": "series running"}))
                    elif (not output.accumulating()):
                        metadata = picam2.capture_metadata()
                        output.accumulate(int(msg["nframes"]), filename_base=filename_from_client, folder_path=folder_path_from_client,
                                          exposure_time=metadata.get("ExposureTime"), analogue_gain=metadata.get("AnalogueGain"),
                                          z_height=msg.get("z_height"), magnification=msg.get("magnification"))
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

                elif msg["command"] in SERIES_COMMANDS:
                    try:
                        response = handle_series_command(msg, output, series_manager, picam2)
                    except Exception as e:
                        # Always reply once, even on an unexpected failure.
                        print(f"Series command error:\n{traceback.format_exc()}")
                        response = _error_response(ERROR_SERVER, f"{type(e).__name__}: {e}")
                    socket.send_string(json.dumps(response))

                elif msg["command"] in DIAGNOSTICS_COMMANDS:
                    try:
                        response = handle_diagnostics_command(msg, diagnostics, picam2)
                    except Exception as e:
                        print(f"Diagnostics command error:\n{traceback.format_exc()}")
                        response = _error_response(ERROR_SERVER, f"{type(e).__name__}: {e}")
                    socket.send_string(json.dumps(response))

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

    # Stop any running series; its writer finishes already-queued images.
    series_manager.shutdown()

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
    

