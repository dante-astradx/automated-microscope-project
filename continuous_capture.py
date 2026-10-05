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
import frame_diagnostics as fd

# Active sensor width. Raw buffer rows are wider (4064 samples) because of row
# padding; frames are cropped to this width, as the live preview already does.
DEFAULT_ACTIVE_WIDTH = 4056

# Default writer-queue capacity, in images. Each full-resolution float32 mean
# is about 47 MiB, so a full queue holds about 705 MiB. Hardware tests showed 3
# was too small: the SD-card writer fell behind and series ended as overloaded.
DEFAULT_QUEUE_CAPACITY = 15

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

# Returned by ContinuousSeries._gate_frame() to skip a frame without error.
_GATE_SKIP = "skip"

# Batch error: a frame arrived without the sensor timestamp needed to schedule
# the inter-series pause.
ERROR_MISSING_TIMESTAMP = "MISSING_TIMESTAMP"


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


# Free buffers kept per BufferPool. A few are enough for the camera callback to
# find a recycled, already-faulted array in steady state; the writer queue, not
# the pool, bounds how many images are in flight.
DEFAULT_POOL_BUFFERS = 4


class BufferPool:
    """Thread-safe pool of full-size arrays, recycled between the callback and writer.

    Its purpose is speed: a fresh 47 MiB array costs about 46 ms of page faults
    the first time it is written, which is too slow for the camera callback. A
    recycled array was already touched, so writing it again costs only memory
    bandwidth.

    ``acquire(shape, dtype)`` never blocks: it pops a free array of that shape
    and dtype, or allocates a fresh one when none is free (the fallback under
    load). With ``zero=True`` every acquired array is all zeros: fresh arrays
    come from ``np.zeros`` and released arrays are zeroed by ``release()``, in
    the releasing thread (the writer), not in the callback. With ``zero=False``
    a recycled array holds stale data, for callers that overwrite every element.

    ``release(array)`` makes the array writeable again and keeps it if fewer
    than ``max_free`` arrays of its kind are free; otherwise it is dropped and
    freed normally. Only release an array that nothing else will read or write.
    """

    def __init__(self, zero=True, max_free=DEFAULT_POOL_BUFFERS):
        self.zero = bool(zero)
        self.max_free = max(0, int(max_free))
        self._lock = threading.Lock()  # leaf lock: never held while taking another
        self._free = {}
        self._reused = 0
        self._allocated = 0

    @staticmethod
    def _key(shape, dtype):
        return (tuple(shape), np.dtype(dtype).str)

    def acquire(self, shape, dtype):
        """Return an array of ``shape`` and ``dtype`` (zeroed if ``zero``). Never blocks."""
        key = self._key(shape, dtype)
        with self._lock:
            free = self._free.get(key)
            if free:
                self._reused += 1
                return free.pop()
            self._allocated += 1
        return np.zeros(shape, dtype=dtype) if self.zero else np.empty(shape, dtype=dtype)

    def release(self, array):
        """Return ``array`` to the pool (zeroing it first if ``zero``)."""
        if not isinstance(array, np.ndarray) or not array.flags.owndata:
            return  # views and foreign buffers are never pooled
        key = self._key(array.shape, array.dtype)
        with self._lock:
            if len(self._free.get(key, ())) >= self.max_free:
                return
        array.flags.writeable = True
        if self.zero:
            array.fill(0)
        with self._lock:
            free = self._free.setdefault(key, [])
            if len(free) < self.max_free:
                free.append(array)

    def clear(self):
        """Drop every free array (frees the memory). Later acquires allocate."""
        with self._lock:
            self._free = {}

    def stats(self):
        """Free arrays and how acquires were served, for status reporting."""
        with self._lock:
            return {
                "free": sum(len(v) for v in self._free.values()),
                "reused": self._reused,
                "allocated": self._allocated,
            }


class _ReadOnlyFlags:
    """The part of ndarray.flags that callers check on a completed mean."""

    __slots__ = ()
    writeable = False


class LazyMean:
    """The mean of a completed group, computed when the writer first reads it.

    Holds the group's uint32 running sum and ``nframes``. ``.mean`` computes the
    float32 mean once, with the same arithmetic as before (a new float32 array,
    ``np.divide(sum, nframes, out=arr)``), marks it read-only, caches it and
    returns it. This moves the divide off the camera thread: only the writer
    thread reads ``.mean``.

    The sum buffer is owned by the LazyMean. Nothing may reuse or modify it until
    ``.mean`` has been computed; after that, ``release_sum()`` hands it back.

    It also behaves enough like the array (``shape``, ``dtype``, ``flags``,
    indexing and ``np.asarray``) for code that inspects a completed group; any
    of those computes the mean.
    """

    flags = _ReadOnlyFlags()

    def __init__(self, sum_array, nframes):
        self._sum = sum_array
        self.nframes = nframes
        self._mean = None
        self._lock = threading.Lock()

    @property
    def mean(self):
        with self._lock:
            if self._mean is None:
                mean = np.empty(self._sum.shape, dtype=np.float32)
                np.divide(self._sum, self.nframes, out=mean)
                mean.flags.writeable = False
                self._mean = mean
            return self._mean

    @property
    def computed(self):
        return self._mean is not None

    def release_sum(self, pool):
        """After ``.mean`` exists, give the sum buffer to ``pool``. Returns True if released."""
        with self._lock:
            if self._mean is None or self._sum is None:
                return False
            sum_array, self._sum = self._sum, None
        pool.release(sum_array)
        return True

    @property
    def shape(self):
        return self.mean.shape

    @property
    def dtype(self):
        return self.mean.dtype

    def __array__(self, dtype=None, copy=None):
        mean = self.mean
        if dtype is not None and np.dtype(dtype) != mean.dtype:
            return mean.astype(dtype)
        return mean.copy() if copy else mean

    def __getitem__(self, index):
        return self.mean[index]


def resolve_mean(mean):
    """The float32 mean array behind a CompletedGroup/QueuedImage ``mean``."""
    return mean.mean if isinstance(mean, LazyMean) else mean


@dataclass(frozen=True)
class CompletedGroup:
    """One finished group of ``nframes`` input frames.

    ``mean`` is a read-only float32 array (``nframes == 1``, possibly a pooled
    buffer) or a LazyMean (``nframes > 1``). Use ``resolve_mean()`` for the array.
    """

    mean: object
    start_monotonic: float
    sensor_timestamps_us: tuple


class FrameGroupAccumulator:
    """Accumulate consecutive raw frames into float32 mean images.

    add_frame() consumes each frame immediately and keeps no reference to the
    input frame. This matters because the camera callback's buffer is only valid
    during the callback. The frame that completes a group is never counted in
    the next group, and no extra frame is needed to trigger completion.

    Two paths, both cheap enough for the camera callback:

    - ``nframes == 1``: the mean of one frame is that frame. The cropped frame
      is converted straight into a float32 buffer from ``mean_pool`` (one pass;
      no running sum, no divide by the group size, no zeroing). The returned
      ``mean`` is that read-only array, and its values are exactly the frame's
      values as float32.
    - ``nframes > 1``: the active columns are added into a uint32 running sum
      taken from ``sum_pool``. On a group's last frame the sum itself is handed
      over as a LazyMean, and a fresh zeroed sum is taken from the pool for the
      next group. The divide happens later, in the writer thread, with the same
      arithmetic as before (``np.divide(sum, nframes)`` into a new float32 array).

    The caller owns the returned buffers: once a completed mean has been written,
    give the mean array back to ``mean_pool`` (``nframes == 1``) or the sum back
    to ``sum_pool`` with ``LazyMean.release_sum`` (``nframes > 1``). Buffers that
    are never released are simply garbage collected; the pool then allocates.

    There is no configured cap on ``nframes``. The running sum is uint32: with
    12-bit samples (maximum 4095) it cannot overflow below about 1,048,000
    frames per group; with full 16-bit samples, below 65,537 frames. Verify the
    real sample range on hardware before relying on very large groups.
    """

    def __init__(self, nframes, width=DEFAULT_ACTIVE_WIDTH, mean_pool=None, sum_pool=None):
        self.nframes = _require_positive_int("nframes", nframes)
        self.width = _require_positive_int("width", width)
        # Means are fully overwritten, so they need no zeroing; sums must start at 0.
        self.mean_pool = mean_pool if mean_pool is not None else BufferPool(zero=False)
        self.sum_pool = sum_pool if sum_pool is not None else BufferPool(zero=True)
        self._shape = None
        self._sum = None
        self._count = 0
        self._start = None
        self._timestamps = []

    @property
    def frames_in_group(self):
        """Number of frames in the current, incomplete group."""
        return self._count

    def add_frame(self, frame, timestamp_us=None, timer=None):
        """Add one decoded uint16 frame. Return a CompletedGroup or None.

        Raises ValueError for a frame that is not a 2D uint16 array, is narrower
        than the active width, or changes shape mid-series. A rejected frame
        leaves the current group unchanged.

        ``timer`` is an optional frame_diagnostics.StageTimer. When given, the
        "accumulate" and "complete" stages are timed; it does not change what
        this method does. At ``nframes == 1``, "accumulate" is only validation
        and the crop view, and "complete" is the single conversion pass.
        """
        with fd.stage(timer, fd.STAGE_ACCUMULATE):
            active = self._validate(frame)
            if self.nframes > 1:
                if self._sum is None:
                    self._sum = self.sum_pool.acquire(active.shape, np.uint32)
                if self._count == 0:
                    self._start = time.monotonic()
                    self._timestamps = []
                np.add(self._sum, active, out=self._sum)
                self._count += 1
                self._timestamps.append(None if timestamp_us is None else int(timestamp_us))

        if self.nframes == 1:
            with fd.stage(timer, fd.STAGE_COMPLETE):
                start = time.monotonic()
                mean = self.mean_pool.acquire(active.shape, np.float32)
                # Exact: every uint16 value is representable in float32 and the
                # divisor is 1, so this equals the old sum-then-divide result
                # bit for bit, in one pass and with float32 arithmetic.
                np.divide(active, np.float32(1), out=mean, dtype=np.float32)
                mean.flags.writeable = False
                return CompletedGroup(
                    mean, start, (None if timestamp_us is None else int(timestamp_us),))

        if self._count < self.nframes:
            return None

        with fd.stage(timer, fd.STAGE_COMPLETE):
            # Hand the sum to the writer as a LazyMean (the divide happens there).
            # The next group's first frame takes a zeroed sum from the pool, so
            # nothing is zeroed here and the last group holds no spare buffer.
            group = CompletedGroup(LazyMean(self._sum, self.nframes), self._start,
                                   tuple(self._timestamps))
            self._sum = None
            self._count = 0
            self._start = None
            self._timestamps = []
        return group

    def _validate(self, frame):
        """Check one frame and return its cropped active view (never copied or kept)."""
        if not isinstance(frame, np.ndarray) or frame.ndim != 2:
            raise ValueError("frame must be a 2D numpy array")
        if frame.dtype != np.uint16:
            raise ValueError(f"frame dtype must be uint16, got {frame.dtype}")
        if frame.shape[1] < self.width:
            raise ValueError(
                f"frame is {frame.shape[1]} columns wide, narrower than the "
                f"active width {self.width}"
            )
        # Crop row padding. This is a view; it is consumed and not kept.
        active = frame[:, :self.width]
        if self._shape is None:
            self._shape = active.shape
        elif self._shape != active.shape:
            raise ValueError(
                f"frame active shape {active.shape} does not match the series "
                f"shape {self._shape}"
            )
        return active

    def reset(self):
        """Discard any incomplete group.

        Nothing is zeroed here: zeroing a full frame was a 16 ms pass in the
        camera callback. A partly filled sum is simply dropped (and freed); the
        next group takes an already-zeroed sum from the pool.
        """
        if self._count > 0:
            self._sum = None
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

        # Recycled image buffers, so the camera callback rarely has to fault in a
        # fresh 47 MiB array. nframes == 1 uses mean buffers (fully overwritten,
        # never zeroed); nframes > 1 uses sum buffers, which the writer zeroes
        # when it releases them. Emptied when the series finishes.
        self._mean_pool = BufferPool(zero=False)
        self._sum_pool = BufferPool(zero=True)
        self._accumulator = FrameGroupAccumulator(self.nframes, width=width,
                                                  mean_pool=self._mean_pool,
                                                  sum_pool=self._sum_pool)
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

    def add_frame(self, frame, timestamp_us=None, timer=None):
        """Consume one frame; safe to call from the camera callback.

        Returns True if the frame was accumulated without error (or was
        deliberately skipped by a subclass, such as during a batch's
        inter-series pause), and False if the series is not capturing, the frame
        was rejected, or its completed image could not be queued.

        ``timer`` is an optional frame_diagnostics.StageTimer (diagnostics
        sessions only). It times the accumulator stages and "enqueue"; lock
        waits and gating are left unattributed.
        """
        with self._capture_lock:
            with self._lock:
                if not self._capture_active:
                    return False
                next_index = self._captured + 1
                gate = self._gate_frame(timestamp_us)

            if gate == _GATE_SKIP:
                return True
            if gate is not None:  # an error dict: stop capture
                self._end_capture(STATE_FAILED, gate)
                return False

            try:
                if timer is None:
                    group = self._accumulator.add_frame(frame, timestamp_us)
                else:
                    group = self._accumulator.add_frame(frame, timestamp_us, timer=timer)
            except ValueError as e:
                self._end_capture(
                    STATE_FAILED,
                    self._make_error(ERROR_INVALID_FRAME, str(e), image_index=next_index),
                )
                return False

            if group is None:
                return True

            # "enqueue" stage: sidecar build, waiting for _lock, the queue put and
            # the counters. The overload handling below is not part of it.
            with fd.stage(timer, fd.STAGE_ENQUEUE):
                filename = self._filename(next_index)
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
                        self._queue_high_watermark = max(self._queue_high_watermark,
                                                         self._queue.qsize())
                        self._on_image_queued(next_index, group)
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
        # Buffer recycling. "free" buffers are ready for the callback; "fresh"
        # counts callback allocations made because none was free (each costs
        # about 46 ms of page faults on hardware), which means the writer is behind.
        means, sums = self._mean_pool.stats(), self._sum_pool.stats()
        snapshot.update({
            "pool_free_means": means["free"],
            "pool_free_sums": sums["free"],
            "pool_reused_buffers": means["reused"] + sums["reused"],
            "pool_fresh_allocations": means["allocated"] + sums["allocated"],
        })
        snapshot.update(memory_snapshot())
        return snapshot

    def _filename(self, image_index):
        """Output basename (no extension) for one image."""
        return f"{self.base_filename}_image{image_index}"

    # Subclass hooks. Both are called with _capture_lock and _lock held, so they
    # must not acquire either lock or block. A single series uses no-op versions.

    def _gate_frame(self, timestamp_us):
        """Decide whether to use a frame: None (use it), _GATE_SKIP, or an error dict."""
        return None

    def _on_image_queued(self, image_index, group):
        """Called after an image is queued and counted as captured."""

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
                self._recycle(item)
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
            # For nframes > 1 this computes the mean (the divide) here, in the
            # writer thread, rather than in the camera callback.
            _write_tiff(tif_path, resolve_mean(item.mean))
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

    def _recycle(self, item):
        """Give an image's buffers back to the pools once nothing will read them.

        Runs in the writer thread after the TIFF and sidecar were written (or
        the write failed). The queued item is dropped right after, so the
        array is not read again. Never raises: a buffer that cannot be
        recycled is simply freed.
        """
        try:
            mean = item.mean
            if isinstance(mean, LazyMean):
                mean.release_sum(self._sum_pool)
            else:
                self._mean_pool.release(mean)
        except Exception:
            pass

    def _finalize(self):
        # The series is over: free the recycled buffers rather than keep up to
        # a few hundred MiB alive while the manager holds the finished series.
        self._mean_pool.clear()
        self._sum_pool.clear()
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



# --- Repeated series (batch) --------------------------------------------------

# Extra sidecar fields owned by a batch; caller metadata cannot set these
# either. Kept separate so single-series validation is unchanged.
BATCH_RESERVED_SIDECAR_KEYS = RESERVED_SIDECAR_KEYS | frozenset({
    "batch_id",
    "series_index",
    "series_count",
    "batch_image_index",
    "inter_series_pause_us",
    "inter_series_gap_us",
})


class ContinuousSeriesBatch(ContinuousSeries):
    """``series_count`` back-to-back series of ``image_count`` images each.

    The camera service schedules the series itself, so the gap between series
    does not depend on client polling or on the writer draining. After the last
    image of series *i* is queued, frames are ignored until one arrives whose
    sensor timestamp is at least ``inter_series_pause_us`` after the last
    contributing frame of series *i*; that frame is the first frame of series
    *i + 1*. The pause is therefore a minimum: the observed gap is rounded up to
    the camera's frame cadence and is reported per boundary.

    Frames are only ignored during the gap. The callback never sleeps or
    blocks, and the writer keeps draining earlier images while later series are
    captured.

    All series share one bounded writer queue and one writer, so
    ``queue_capacity`` bounds the backlog for the whole batch. A full queue
    overloads the batch exactly as it does a single series.

    Each image is saved as ``<base>_series<S>_image<I>`` with a sidecar that
    holds the per-series ``image_index``/``image_count`` plus ``batch_id``,
    ``series_index``, ``series_count``, ``inter_series_pause_us`` and a
    per-series ``series_id`` (``<batch_id>-s<S>``).

    In status and error dicts, ``image_index`` is the batch-wide image number
    (1 .. image_count * series_count). Errors also carry ``series_index`` and
    ``series_image_index``.
    """

    def __init__(
        self,
        image_count,
        series_count,
        nframes,
        output_dir,
        base_filename,
        metadata=None,
        inter_series_pause_us=0,
        queue_capacity=DEFAULT_QUEUE_CAPACITY,
        width=DEFAULT_ACTIVE_WIDTH,
        batch_id=None,
        logger=print,
    ):
        self.images_per_series = _require_positive_int("image_count", image_count)
        self.series_count = _require_positive_int("series_count", series_count)
        if (isinstance(inter_series_pause_us, bool)
                or not isinstance(inter_series_pause_us, (int, np.integer))
                or inter_series_pause_us < 0):
            raise ValueError(
                "inter_series_pause_us must be a non-negative integer, "
                f"got {inter_series_pause_us!r}"
            )
        reserved = BATCH_RESERVED_SIDECAR_KEYS.intersection(metadata or {})
        if reserved:
            raise ValueError(f"metadata cannot set reserved sidecar keys: {sorted(reserved)}")

        super().__init__(
            image_count=self.images_per_series * self.series_count,
            nframes=nframes,
            output_dir=output_dir,
            base_filename=base_filename,
            metadata=metadata,
            queue_capacity=queue_capacity,
            width=width,
            series_id=batch_id,
            logger=logger,
        )
        self.batch_id = self.series_id
        self.inter_series_pause_us = int(inter_series_pause_us)
        self.child_series_ids = [
            f"{self.batch_id}-s{n}" for n in range(1, self.series_count + 1)
        ]

        # Inter-series gap state, protected by _lock.
        self._awaiting_next_series = False
        self._gap_threshold_us = None      # first acceptable timestamp, or None
        self._gap_last_timestamp_us = None  # last frame of the previous series
        self._gap_skipped_current = 0
        self._gap_frames_skipped = 0
        self._boundaries = []

    def _position(self, image_index):
        """Return (series_index, image index within that series) for a batch index."""
        zero_based = image_index - 1
        return (zero_based // self.images_per_series + 1,
                zero_based % self.images_per_series + 1)

    def _filename(self, image_index):
        series_index, local_index = self._position(image_index)
        return f"{self.base_filename}_series{series_index}_image{local_index}"

    # Hooks. Called with _capture_lock and _lock held; they do not lock or block.

    def _on_image_queued(self, image_index, group):
        series_index, local_index = self._position(image_index)
        if local_index == self.images_per_series and series_index < self.series_count:
            timestamps = group.sensor_timestamps_us
            self._awaiting_next_series = True
            self._gap_last_timestamp_us = timestamps[-1] if timestamps else None
            self._gap_skipped_current = 0

    def _gate_frame(self, timestamp_us):
        if not self._awaiting_next_series:
            return None
        next_index = self._captured + 1
        series_index, _ = self._position(next_index)
        last = self._gap_last_timestamp_us

        if self.inter_series_pause_us > 0 and (timestamp_us is None or last is None):
            return self._make_error(
                ERROR_MISSING_TIMESTAMP,
                f"Cannot schedule the {self.inter_series_pause_us} us pause before series "
                f"{series_index}: a sensor timestamp is missing",
                image_index=next_index,
            )
        if timestamp_us is not None and last is not None:
            timestamp_us = int(timestamp_us)
            if timestamp_us < last:
                return self._make_error(
                    ERROR_INVALID_FRAME,
                    f"Sensor timestamp went backwards before series {series_index} "
                    f"({timestamp_us} us after {last} us)",
                    image_index=next_index,
                )
            if timestamp_us - last < self.inter_series_pause_us:
                self._gap_skipped_current += 1
                self._gap_frames_skipped += 1
                return _GATE_SKIP

        # This frame starts the next series.
        self._boundaries.append({
            "before_series_index": series_index,
            "previous_last_timestamp_us": last,
            "first_timestamp_us": None if timestamp_us is None else int(timestamp_us),
            "observed_gap_us": (
                None if timestamp_us is None or last is None else int(timestamp_us) - last
            ),
            "frames_skipped": self._gap_skipped_current,
        })
        self._awaiting_next_series = False
        self._gap_last_timestamp_us = None
        self._gap_skipped_current = 0
        return None

    def _boundary_before(self, series_index):
        for boundary in self._boundaries:
            if boundary["before_series_index"] == series_index:
                return boundary
        return None

    def _build_sidecar(self, image_index, filename, group, enqueue_monotonic):
        # Called with _capture_lock held; boundaries are only changed under it.
        sidecar = super()._build_sidecar(image_index, filename, group, enqueue_monotonic)
        series_index, local_index = self._position(image_index)
        boundary = self._boundary_before(series_index) if local_index == 1 else None
        sidecar.update({
            "series_id": self.child_series_ids[series_index - 1],
            "image_index": local_index,
            "image_count": self.images_per_series,
            "batch_id": self.batch_id,
            "batch_image_index": image_index,
            "series_index": series_index,
            "series_count": self.series_count,
            "inter_series_pause_us": self.inter_series_pause_us,
            # Only on the first image of series 2+: the measured gap from the
            # previous series' last frame to this image's first frame.
            "inter_series_gap_us": boundary["observed_gap_us"] if boundary else None,
        })
        return sidecar

    def _make_error(self, code, message, image_index=None, filename=None):
        error = super()._make_error(code, message, image_index=image_index, filename=filename)
        if image_index is not None:
            series_index, local_index = self._position(image_index)
        else:
            series_index = local_index = None
        error["series_index"] = series_index
        error["series_image_index"] = local_index
        return error

    def status_snapshot(self):
        """Single-series status fields plus batch progress.

        ``requested_images``, ``captured_images`` and ``written_images`` are
        batch totals. ``current_series_index`` and ``series_captured_images``
        describe the series being captured (or the next one during a pause).
        """
        snapshot = super().status_snapshot()
        with self._lock:
            captured = self._captured
            awaiting = self._awaiting_next_series
            skipped = self._gap_frames_skipped
            boundaries = [dict(b) for b in self._boundaries]
        current = min(captured // self.images_per_series + 1, self.series_count)
        snapshot.update({
            "batch_id": self.batch_id,
            "series_ids": list(self.child_series_ids),
            "series_count": self.series_count,
            "images_per_series": self.images_per_series,
            "inter_series_pause_us": self.inter_series_pause_us,
            "current_series_index": current,
            "series_captured_images": captured - (current - 1) * self.images_per_series,
            "completed_series": captured // self.images_per_series,
            "awaiting_next_series": awaiting,
            "frames_skipped_in_pauses": skipped,
            "boundaries": boundaries,
        })
        return snapshot

