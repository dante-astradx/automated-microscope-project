"""
Hardware-independent core for continuous (series) image capture.

This module contains no camera or ZeroMQ code. It is designed to be driven by
the camera service's encoder output callback in a later integration phase:

- FrameGroupAccumulator groups incoming raw frames into consecutive,
  non-overlapping groups of ``nframes`` and produces one float32 mean image per
  group.
- ContinuousSeries wraps an accumulator with a bounded writer queue and a single
  writer thread that saves one single-page float32 TIFF plus one JSON sidecar per
  output image, and tracks thread-safe progress/status for status polling.

See CONTINUOUS-IMAGING-PLAN/ARCHITECTURE.md for the design decisions this
implements. The legacy ``accumulate`` path in camera_zmq.py is not used or
changed by this module.
"""

import json
import os
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import tifffile

import config as c

# Active sensor width. Raw buffer rows are wider (4064 samples) because of row
# padding; frames are cropped to this width, as the live preview already does.
DEFAULT_ACTIVE_WIDTH = 4056

# Conservative default writer-queue capacity, in images, for initial hardware
# testing. Each full-resolution float32 mean is about 47 MiB.
DEFAULT_QUEUE_CAPACITY = 3

# Series states. Values are lowercase to match the planned ZeroMQ status replies.
STATE_ACCEPTED = "accepted"
STATE_CAPTURING = "capturing"
STATE_DRAINING = "draining"
STATE_COMPLETE = "complete"
STATE_FAILED = "failed"
STATE_OVERLOADED = "overloaded"
STATE_CANCELLED = "cancelled"
TERMINAL_STATES = frozenset({STATE_COMPLETE, STATE_FAILED, STATE_OVERLOADED, STATE_CANCELLED})

# Stable error codes reported in status snapshots.
ERROR_WRITE_FAILED = "WRITE_FAILED"
ERROR_QUEUE_OVERLOADED = "QUEUE_OVERLOADED"
ERROR_INVALID_FRAME = "INVALID_FRAME"
ERROR_WRITER_ERROR = "WRITER_ERROR"

# Sidecar fields owned by this module; callers cannot override them through
# metadata. "time" is reserved so the legacy field name is never reused with a
# different meaning.
RESERVED_SIDECAR_KEYS = frozenset({
    "series_id",
    "image_index",
    "image_count",
    "frames",
    "image_time",
    "save_time",
    "sensor_timestamps_us",
    "image_filename",
    "image_filepath",
    "data_timestamp",
    "time",
})

_WRITER_POLL_SECONDS = 0.05


def _require_positive_int(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return int(value)


def _write_tiff(path, image):
    """Write one uncompressed single-page TIFF. Module-level so tests can patch it."""
    tifffile.imwrite(path, image)


def _write_json(path, data):
    """Write one JSON sidecar. Module-level so tests can patch it."""
    with open(path, "w") as fp:
        json.dump(data, fp)


def _read_proc_kib(path, key):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def memory_snapshot():
    """Return process RSS and system available memory in bytes (None if unavailable)."""
    return {
        "process_rss_bytes": _read_proc_kib("/proc/self/status", "VmRSS:"),
        "system_available_bytes": _read_proc_kib("/proc/meminfo", "MemAvailable:"),
    }


@dataclass(frozen=True)
class CompletedGroup:
    """One finished group of ``nframes`` input frames."""

    mean: np.ndarray
    start_monotonic: float
    sensor_timestamps_us: tuple


class FrameGroupAccumulator:
    """Accumulate consecutive raw frames into float32 mean images.

    add_frame() consumes each frame immediately: the active columns are added
    into a uint32 running sum and no reference to the input frame is kept. This
    matters because the camera callback's buffer is only valid during the
    callback.

    After exactly ``nframes`` frames, add_frame() returns a CompletedGroup whose
    ``mean`` is a new, read-only float32 array independent of the running sum.
    The sum buffer is then zeroed and reused. The frame that completes a group is
    never counted in the next group, and no extra frame is needed to trigger
    completion.

    There is no configured cap on ``nframes``. The running sum is uint32: with
    12-bit samples (maximum 4095) it cannot overflow below about 1,048,000
    frames per group; with full 16-bit samples, below 65,537 frames. Verify the
    real sample range on hardware before relying on very large groups.
    """

    def __init__(self, nframes, width=DEFAULT_ACTIVE_WIDTH):
        self.nframes = _require_positive_int("nframes", nframes)
        self.width = _require_positive_int("width", width)
        self._sum = None
        self._count = 0
        self._start = None
        self._timestamps = []

    @property
    def frames_in_group(self):
        """Number of frames in the current, incomplete group."""
        return self._count

    def add_frame(self, frame, timestamp_us=None):
        """Add one decoded uint16 frame. Return a CompletedGroup or None.

        Raises ValueError for a frame that is not a 2D uint16 array, is narrower
        than the active width, or changes shape mid-series. A rejected frame
        leaves the current group unchanged.
        """
        if not isinstance(frame, np.ndarray) or frame.ndim != 2:
            raise ValueError("frame must be a 2D numpy array")
        if frame.dtype != np.uint16:
            raise ValueError(f"frame dtype must be uint16, got {frame.dtype}")
        if frame.shape[1] < self.width:
            raise ValueError(
                f"frame is {frame.shape[1]} columns wide, narrower than the "
                f"active width {self.width}"
            )

        # Crop row padding. This is a view; np.add consumes it and it is not kept.
        active = frame[:, :self.width]
        if self._sum is None:
            self._sum = np.zeros(active.shape, dtype=np.uint32)
        elif self._sum.shape != active.shape:
            raise ValueError(
                f"frame active shape {active.shape} does not match the series "
                f"shape {self._sum.shape}"
            )

        if self._count == 0:
            self._start = time.monotonic()
            self._timestamps = []

        np.add(self._sum, active, out=self._sum)
        self._count += 1
        self._timestamps.append(None if timestamp_us is None else int(timestamp_us))

        if self._count < self.nframes:
            return None

        # Same arithmetic as the legacy path (divide, store as float32), written
        # straight into a new float32 array so no full-size temporary is kept.
        mean = np.empty(self._sum.shape, dtype=np.float32)
        np.divide(self._sum, self.nframes, out=mean)
        mean.flags.writeable = False
        group = CompletedGroup(mean, self._start, tuple(self._timestamps))
        self.reset()
        return group

    def reset(self):
        """Discard any incomplete group and zero the sum buffer for reuse."""
        if self._sum is not None:
            self._sum.fill(0)
        self._count = 0
        self._start = None
        self._timestamps = []


@dataclass(frozen=True)
class QueuedImage:
    """A completed output image waiting for the writer."""

    image_index: int
    filename: str
    mean: np.ndarray
    sidecar: dict
    enqueue_monotonic: float


class ContinuousSeries:
    """One finite series of ``image_count`` output images.

    Intended use by the camera service:

        series = ContinuousSeries(image_count, nframes, output_dir, base_filename,
                                  metadata={...})
        series.start()
        series.add_frame(frame, timestamp_us)   # from the encoder callback
        series.status_snapshot()                # from the ZeroMQ server
        series.cancel()

    add_frame() never waits on disk I/O: completed images go onto a bounded queue
    that a single writer thread drains in sequence order. If the queue is full
    when an image completes, the series becomes OVERLOADED: that image is not
    queued, not counted as captured or written, and capture stops. Images that
    were already queued are still written.

    The series reaches COMPLETE only after every requested image has been written
    and the queue has drained. The first error is latched and reported in every
    later status snapshot.
    """

    def __init__(
        self,
        image_count,
        nframes,
        output_dir,
        base_filename,
        metadata=None,
        queue_capacity=DEFAULT_QUEUE_CAPACITY,
        width=DEFAULT_ACTIVE_WIDTH,
        series_id=None,
        logger=print,
    ):
        self.image_count = _require_positive_int("image_count", image_count)
        self.nframes = _require_positive_int("nframes", nframes)
        self.queue_capacity = _require_positive_int("queue_capacity", queue_capacity)
        if not isinstance(base_filename, str) or not base_filename:
            raise ValueError("base_filename must be a non-empty string")
        if not os.path.isdir(output_dir):
            raise ValueError(f"output_dir does not exist: {output_dir}")

        metadata = dict(metadata or {})
        reserved = RESERVED_SIDECAR_KEYS.intersection(metadata)
        if reserved:
            raise ValueError(f"metadata cannot set reserved sidecar keys: {sorted(reserved)}")

        self.output_dir = output_dir
        self.base_filename = base_filename
        self.series_id = series_id or uuid.uuid4().hex[:12]
        self.logger = logger

        # Base sidecar fields, matching the legacy sidecar where applicable.
        self._static_metadata = {
            "camera": c.CAMERA,
            "sensor": c.SENSOR,
            "lens_adapter": c.LENS_ADAPTER,
            "light_source": c.LIGHT_SOURCE,
            "exposure_time": None,
            "analogue_gain": None,
            "magnification": None,
            "z_height": None,
        }
        self._static_metadata.update(metadata)

        self._accumulator = FrameGroupAccumulator(self.nframes, width=width)
        self._queue = queue.Queue(maxsize=self.queue_capacity)

        # Lock order: _capture_lock, then _lock. Code holding _lock never
        # acquires _capture_lock.
        self._capture_lock = threading.Lock()  # serializes accumulator access
        self._lock = threading.Lock()  # protects the status fields below

        self._state = STATE_ACCEPTED
        self._capture_active = False
        self._pending_terminal = None
        self._error = None
        self._write_failed = False
        self._captured = 0
        self._written = 0
        self._discarded_images = 0
        self._discarded_partial_frames = 0
        self._queue_high_watermark = 0
        self._writing_index = None

        self._capture_finished = threading.Event()
        self._writer_thread = None

    def start(self):
        """Begin accepting frames and start the writer thread."""
        with self._lock:
            if self._state != STATE_ACCEPTED:
                raise RuntimeError(f"series cannot start from state {self._state!r}")
            self._state = STATE_CAPTURING
            self._capture_active = True
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name=f"series-writer-{self.series_id}",
            daemon=True,
        )
        self._writer_thread.start()

    def add_frame(self, frame, timestamp_us=None):
        """Consume one frame; safe to call from the camera callback.

        Returns True if the frame was accumulated without error, and False if the
        series is not capturing, the frame was rejected, or its completed image
        could not be queued.
        """
        with self._capture_lock:
            with self._lock:
                if not self._capture_active:
                    return False
                next_index = self._captured + 1

            try:
                group = self._accumulator.add_frame(frame, timestamp_us)
            except ValueError as e:
                self._end_capture(
                    STATE_FAILED,
                    self._make_error(ERROR_INVALID_FRAME, str(e), image_index=next_index),
                )
                return False

            if group is None:
                return True

            filename = f"{self.base_filename}_image{next_index}"
            enqueue_monotonic = time.monotonic()
            item = QueuedImage(
                image_index=next_index,
                filename=filename,
                mean=group.mean,
                sidecar=self._build_sidecar(next_index, filename, group, enqueue_monotonic),
                enqueue_monotonic=enqueue_monotonic,
            )

            # Queue and count together under the status lock so a status snapshot
            # never shows more images written than captured. put_nowait() does not
            # block, and the writer never holds _lock while waiting on the queue.
            overloaded = False
            capture_done = False
            with self._lock:
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    overloaded = True
                else:
                    self._captured += 1
                    self._queue_high_watermark = max(self._queue_high_watermark, self._queue.qsize())
                    if self._captured == self.image_count:
                        self._capture_active = False
                        self._state = STATE_DRAINING
                        capture_done = True

            if overloaded:
                self._end_capture(
                    STATE_OVERLOADED,
                    self._make_error(
                        ERROR_QUEUE_OVERLOADED,
                        f"Writer queue full ({self.queue_capacity} images); image "
                        f"{next_index} was not queued or saved and capture stopped",
                        image_index=next_index,
                        filename=filename,
                    ),
                )
                return False
            if capture_done:
                self._capture_finished.set()
            return True

    def cancel(self):
        """Stop capture. Images already queued are still written.

        Frames in an incomplete group are discarded and reported as
        ``discarded_partial_frames``. Cancelling after every image has been
        captured does not change the outcome: queued images are still written and
        the series can reach COMPLETE, because no data was lost.
        """
        with self._capture_lock:
            with self._lock:
                if self._state == STATE_ACCEPTED:
                    # Never started, so there is no writer thread to finalize.
                    self._state = STATE_CANCELLED
                    self._pending_terminal = STATE_CANCELLED
                    self._capture_finished.set()
                    return
                if not self._capture_active:
                    return
            self._end_capture(STATE_CANCELLED, None)

    def wait(self, timeout=None):
        """Wait for the writer to finish. Return True if the series is terminal."""
        if self._writer_thread is not None:
            self._writer_thread.join(timeout)
        return self.is_terminal()

    def is_terminal(self):
        with self._lock:
            return self._state in TERMINAL_STATES

    @property
    def state(self):
        with self._lock:
            return self._state

    def status_snapshot(self):
        """Return a JSON-serializable progress/status dict for status polling."""
        with self._lock:
            snapshot = {
                "ok": True,
                "series_id": self.series_id,
                "state": self._state,
                "is_terminal": self._state in TERMINAL_STATES,
                "requested_images": self.image_count,
                "captured_images": self._captured,
                "written_images": self._written,
                "discarded_images": self._discarded_images,
                "discarded_partial_frames": self._discarded_partial_frames,
                "nframes": self.nframes,
                # Read without the capture lock; a momentarily stale value is fine.
                "frames_in_current_group": self._accumulator.frames_in_group,
                "queue_depth": self._queue.qsize(),
                "queue_high_watermark": self._queue_high_watermark,
                "queue_capacity": self.queue_capacity,
                "writing_image_index": self._writing_index,
                "error": dict(self._error) if self._error else None,
            }
        snapshot.update(memory_snapshot())
        return snapshot

    def _make_error(self, code, message, image_index=None, filename=None):
        return {
            "code": code,
            "message": message,
            "image_index": image_index,
            "filename": filename,
        }

    def _end_capture(self, terminal_state, error):
        """Stop capture and record why. Caller must hold _capture_lock.

        The first error is kept. An error outcome replaces a pending
        cancellation, so a later write failure is not hidden by a cancel.
        """
        with self._lock:
            if error is not None and self._error is None:
                self._error = error
            if self._pending_terminal is None or (
                self._pending_terminal == STATE_CANCELLED and terminal_state != STATE_CANCELLED
            ):
                self._pending_terminal = terminal_state
            was_active = self._capture_active
            self._capture_active = False
            if self._state not in TERMINAL_STATES:
                self._state = STATE_DRAINING
            partial = self._accumulator.frames_in_group if was_active else 0
            self._discarded_partial_frames += partial
        if partial:
            self._accumulator.reset()
        self._capture_finished.set()

    def _build_sidecar(self, image_index, filename, group, enqueue_monotonic):
        sidecar = dict(self._static_metadata)
        tif_path = os.path.join(self.output_dir, f"{filename}.tif")
        sidecar.update({
            "series_id": self.series_id,
            "image_index": image_index,
            "image_count": self.image_count,
            "frames": self.nframes,
            "image_time": enqueue_monotonic - group.start_monotonic,
            "sensor_timestamps_us": list(group.sensor_timestamps_us),
            "image_filename": f"{filename}.tif",
            # Relative to the slide-case folder, as in the legacy sidecar.
            "image_filepath": os.path.relpath(tif_path, os.path.dirname(self.output_dir)),
        })
        return sidecar

    def _writer_loop(self):
        try:
            while True:
                try:
                    item = self._queue.get(timeout=_WRITER_POLL_SECONDS)
                except queue.Empty:
                    # Every put happens before _capture_finished is set, so once it
                    # is set an empty queue means nothing is left to write.
                    if self._capture_finished.is_set() and self._queue.empty():
                        break
                    continue

                if self._write_failed:
                    # After a write failure, stop writing but account for the
                    # images still queued.
                    with self._lock:
                        self._discarded_images += 1
                    continue
                self._write_item(item)
        except Exception as e:  # defensive: never leave the series non-terminal
            self.logger(f"Series {self.series_id} writer error:\n{traceback.format_exc()}")
            with self._capture_lock:
                self._end_capture(
                    STATE_FAILED,
                    self._make_error(ERROR_WRITER_ERROR, f"Writer stopped: {type(e).__name__}: {e}"),
                )
        finally:
            self._finalize()

    def _write_item(self, item):
        with self._lock:
            self._writing_index = item.image_index
        tif_path = os.path.join(self.output_dir, f"{item.filename}.tif")
        json_path = os.path.join(self.output_dir, f"{item.filename}.json")
        try:
            _write_tiff(tif_path, item.mean)
            sidecar = dict(item.sidecar)
            # Measured after the TIFF is written, just before the sidecar itself
            # is written (a file cannot record its own write duration).
            sidecar["save_time"] = time.monotonic() - item.enqueue_monotonic
            sidecar["data_timestamp"] = datetime.now().strftime("%Y%m%d_%H%M%S")
            _write_json(json_path, sidecar)
        except Exception as e:
            self.logger(
                f"Series {self.series_id}: failed to write image {item.image_index}:\n"
                f"{traceback.format_exc()}"
            )
            self._write_failed = True
            with self._capture_lock:
                self._end_capture(
                    STATE_FAILED,
                    self._make_error(
                        ERROR_WRITE_FAILED,
                        f"Could not write image {item.image_index}: {type(e).__name__}: {e}",
                        image_index=item.image_index,
                        filename=item.filename,
                    ),
                )
            with self._lock:
                self._writing_index = None
            return

        with self._lock:
            self._written += 1
            self._writing_index = None

    def _finalize(self):
        with self._lock:
            if self._pending_terminal is not None:
                self._state = self._pending_terminal
            elif self._written == self.image_count:
                self._state = STATE_COMPLETE
            else:
                # Should be unreachable; never report success with missing images.
                if self._error is None:
                    self._error = self._make_error(
                        ERROR_WRITER_ERROR,
                        f"Writer finished with {self._written} of {self.image_count} images written",
                    )
                self._state = STATE_FAILED
            self._writing_index = None

