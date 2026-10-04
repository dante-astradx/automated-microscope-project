"""
Frame-delivery diagnostics for the camera service.

Answers one question: are camera frames being lost, and if so, where?

Picamera2 runs a single camera thread. For each completed request it calls
``picam2.post_callback(request)``, then the encoder, which calls our
``OutputZMQ.outputframe()`` synchronously, and only then releases the buffer
back to libcamera. With a small buffer pool (6 by default for video
configurations), a callback that is slower than the frame period makes
libcamera run out of buffers, and sensor frames are dropped before any of our
code sees them.

Per delivered frame this records:

- ``sequence``: libcamera's frame sequence number. Consecutive sensor frames
  have consecutive numbers, so a jump is direct evidence that frames were
  dropped before reaching Python.
- ``sensor_ts_us``: SensorTimestamp (CLOCK_BOOTTIME, microseconds).
- ``latency_us``: CLOCK_BOOTTIME when the request reached Python, minus
  sensor_ts_us. Growing latency means completed requests are queueing.
- ``encoder_ts_us``: the timestamp passed to outputframe (fallback if the
  request hook is unavailable).
- ``callback_ms``: how long ``outputframe()`` took for this frame.
- ``mode``: which capture path was active ("idle", "legacy", "legacy_save",
  "series").

Recording is off by default and costs one attribute check per frame when off.
This module has no camera or ZeroMQ code, so it can be tested on its own.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime

import numpy as np

MODE_IDLE = "idle"
MODE_LEGACY = "legacy"
MODE_LEGACY_SAVE = "legacy_save"  # the callback in which the legacy TIFF is written
MODE_SERIES = "series"

# Upper bound on stored frames per session (about 5.5 hours at 10 fps).
DEFAULT_MAX_RECORDS = 200_000

# A timestamp interval longer than this multiple of the frame period is a gap.
GAP_FACTOR = 1.5

# Record field order (records are stored as compact lists).
FIELDS = ("sequence", "sensor_ts_us", "latency_us", "encoder_ts_us", "callback_ms", "mode")


def _boottime_us():
    """CLOCK_BOOTTIME in microseconds (the clock libcamera uses for SensorTimestamp)."""
    try:
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1000
    except (AttributeError, OSError):  # not Linux
        return None


def _stats(values):
    if not values:
        return {"count": 0, "mean": None, "median": None, "p95": None, "max": None}
    arr = np.asarray(values, dtype=float)
    return {
        "count": int(arr.size),
        "mean": round(float(arr.mean()), 3),
        "median": round(float(np.median(arr)), 3),
        "p95": round(float(np.percentile(arr, 95)), 3),
        "max": round(float(arr.max()), 3),
    }


def _sequence_summary(records, max_listed):
    """Frames missing according to libcamera sequence numbers."""
    seq_records = [r for r in records if r[0] is not None]
    if len(seq_records) < 2:
        return None
    seqs = np.asarray([r[0] for r in seq_records], dtype=np.int64)
    diffs = np.diff(seqs)
    gap_idx = np.nonzero(diffs > 1)[0]
    return {
        "first": int(seqs[0]),
        "last": int(seqs[-1]),
        "expected_frames": int(seqs[-1] - seqs[0] + 1),
        "missing_frames": int(np.sum(diffs[diffs > 1] - 1)),
        "gap_count": int(gap_idx.size),
        "non_increasing": int(np.count_nonzero(diffs <= 0)),
        "gaps": [
            {"after_sequence": int(seqs[i]), "missing": int(diffs[i] - 1),
             "mode_before": seq_records[i][5], "mode_after": seq_records[i + 1][5]}
            for i in gap_idx[:max_listed]
        ],
    }


def summarize(records, frame_period_us=None, max_listed=50):
    """Summarize frame records (lists in FIELDS order). Returns a JSON-safe dict.

    ``frame_period_us`` is the expected frame period. If not given, it is
    estimated as the median positive sensor-timestamp interval.
    """
    summary = {
        "frames_recorded": len(records),
        "frame_period_ms": None,
        "frames_per_second": None,
        "sequence": _sequence_summary(records, max_listed),
        "timestamp_gaps": None,
        "latency_ms": _stats([r[2] / 1000 for r in records if r[2] is not None]),
        "callback_ms_by_mode": {},
        "callbacks_slower_than_frame_period": None,
    }

    stamps = [r[1] if r[1] is not None else r[3] for r in records]
    if len(stamps) >= 2 and all(s is not None for s in stamps):
        intervals = np.diff(np.asarray(stamps, dtype=np.int64))
        positive = intervals[intervals > 0]
        period = frame_period_us or (float(np.median(positive)) if positive.size else None)
        if period:
            summary["frame_period_ms"] = round(period / 1000, 3)
            summary["frames_per_second"] = round(1e6 / period, 3)
            long_idx = np.nonzero(intervals > GAP_FACTOR * period)[0]
            missing = [int(round(intervals[i] / period)) - 1 for i in long_idx]
            summary["timestamp_gaps"] = {
                "gap_count": int(long_idx.size),
                "estimated_missing_frames": int(sum(missing)),
                "interval_ms": _stats((intervals / 1000).tolist()),
                "gaps": [
                    {"frame_index": int(i + 1),
                     "interval_ms": round(float(intervals[i]) / 1000, 3),
                     "estimated_missing": m, "mode_before": records[i][5],
                     "mode_after": records[i + 1][5]}
                    for i, m in list(zip(long_idx, missing))[:max_listed]
                ],
            }
            summary["callbacks_slower_than_frame_period"] = sum(
                1 for r in records if r[4] is not None and r[4] * 1000 > period)

    by_mode = {}
    for r in records:
        if r[4] is not None:
            by_mode.setdefault(r[5], []).append(r[4])
    summary["callback_ms_by_mode"] = {m: _stats(v) for m, v in sorted(by_mode.items())}
    return summary


def request_frame_info(request):
    """Return (sequence, sensor_timestamp_us) from a Picamera2 CompletedRequest.

    Either value is None if it cannot be read.
    """
    raw = getattr(request, "request", None)
    if raw is None:
        return None, None
    sequence = sensor_ts_us = None
    buffer = None
    try:
        buffer = next(iter(raw.buffers.values()))
        sequence = int(buffer.metadata.sequence)
    except Exception:
        try:
            sequence = int(raw.sequence)
        except Exception:
            pass
    try:
        from libcamera import controls  # only available on the Pi
        sensor_ts_us = int(raw.metadata[controls.SensorTimestamp]) // 1000
    except Exception:
        try:
            sensor_ts_us = int(buffer.metadata.timestamp) // 1000
        except Exception:
            pass
    return sequence, sensor_ts_us



class FrameDiagnostics:
    """Thread-safe per-frame recorder.

    The camera thread calls ``on_request()`` (from picam2.post_callback) and
    then ``record_callback()`` (at the end of outputframe) for each frame. The
    ZeroMQ thread calls start/status/stop.
    """

    def __init__(self, max_records=DEFAULT_MAX_RECORDS):
        self.max_records = max_records
        self._lock = threading.Lock()
        self.active = False
        self._reset(label=None)

    def _reset(self, label):
        self.session_id = None
        self.label = label
        self.started_at = None
        self.started_monotonic = None
        self.camera_info = {}
        self._records = []
        self._pending = None
        self.truncated = 0
        self.unpaired_requests = 0

    # --- Camera-thread hooks: must never raise into the camera loop ----------

    def on_request(self, request):
        """picam2.post_callback hook: capture sequence and sensor timestamp."""
        if not self.active:
            return
        try:
            sequence, sensor_ts_us = request_frame_info(request)
            now_us = _boottime_us()
            latency = None if sensor_ts_us is None or now_us is None else now_us - sensor_ts_us
            with self._lock:
                if self._pending is not None:
                    self.unpaired_requests += 1  # previous request never reached outputframe
                self._pending = (sequence, sensor_ts_us, latency)
        except Exception:  # diagnostics must not disturb capture
            pass

    def record_callback(self, encoder_ts_us, callback_s, mode):
        """Called at the end of outputframe() with its duration in seconds."""
        if not self.active:
            return
        try:
            with self._lock:
                pending, self._pending = self._pending, None
                if len(self._records) >= self.max_records:
                    self.truncated += 1
                    return
                seq, sensor_ts, latency = pending if pending else (None, None, None)
                self._records.append([seq, sensor_ts, latency, encoder_ts_us,
                                      round(callback_s * 1000, 3), mode])
        except Exception:
            pass

    # --- Control (ZeroMQ thread) ----------------------------------------------

    def start(self, label=None, camera_info=None):
        """Start a new session. Raises RuntimeError if one is already running."""
        with self._lock:
            if self.active:
                raise RuntimeError(f"diagnostics session {self.session_id} is already running")
            self._reset(label)
            self.session_id = uuid.uuid4().hex[:12]
            self.started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.started_monotonic = time.monotonic()
            self.camera_info = dict(camera_info or {})
            self.active = True
        return self.session_id

    def _snapshot(self):
        with self._lock:
            return {
                "session_id": self.session_id,
                "label": self.label,
                "active": self.active,
                "started_at": self.started_at,
                "elapsed_s": (None if self.started_monotonic is None
                              else round(time.monotonic() - self.started_monotonic, 3)),
                "camera_info": dict(self.camera_info),
                "truncated_records": self.truncated,
                "unpaired_requests": self.unpaired_requests,
                "records": [list(r) for r in self._records],
            }

    def _period_hint(self):
        """Expected frame period (us) from camera metadata FrameDuration, if known."""
        duration = self.camera_info.get("FrameDuration")
        return duration if isinstance(duration, (int, float)) and duration > 0 else None

    def status(self):
        """Session details plus a summary (no per-frame records)."""
        snap = self._snapshot()
        snap["summary"] = summarize(snap.pop("records"), self._period_hint())
        return snap

    def stop(self, output_dir=None):
        """Stop recording; write the full per-frame record to ``output_dir``.

        Returns (status dict without records, path or None).
        """
        with self._lock:
            self.active = False
            self._pending = None
        snap = self._snapshot()
        snap["fields"] = list(FIELDS)
        snap["summary"] = summarize(snap["records"], self._period_hint())
        path = None
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            label = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                            for ch in (snap["label"] or "session"))
            path = os.path.join(
                output_dir, f"{snap['started_at']}_{label}_{snap['session_id']}_frames.json")
            with open(path, "w") as fp:
                json.dump(snap, fp)
        snap.pop("records")
        return snap, path
