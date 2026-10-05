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

- ``sequence``: the buffer sequence number. On this Pi (unicam) it counts
  delivered buffers, not sensor frames: the 2026-10-04 hardware run lost 87
  frames with no sequence gaps. It only checks delivery order.
  Frame loss is measured from sensor-timestamp gaps instead (summary
  ``missing_frames``), because the sensor runs at a fixed frame period.
- ``sensor_ts_us``: SensorTimestamp (CLOCK_BOOTTIME, microseconds).
- ``latency_us``: CLOCK_BOOTTIME when the request reached Python, minus
  sensor_ts_us. Growing latency means completed requests are queueing.
- ``encoder_ts_us``: the timestamp passed to outputframe (fallback if the
  request hook is unavailable).
- ``callback_ms``: how long ``outputframe()`` took for this frame.
- ``mode``: which capture path was active ("idle", "legacy", "legacy_save",
  "series").
- ``stages_ms``: time spent in named stages of the callback (see STAGES), as
  a dict of stage name -> ms, or None if nothing was timed. Stages are timed
  with a StageTimer that the callback creates only while a session is active.
  Time in the callback outside every stage (locks, mode checks, ...) is
  reported in the summary as ``unattributed_ms``.

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

# Record field order (records are stored as compact lists). Records written
# before stage timing existed have only the first six fields.
FIELDS = ("sequence", "sensor_ts_us", "latency_us", "encoder_ts_us", "callback_ms", "mode",
          "stages_ms")

# Callback stages (profiling). Each stage times only its own block; stages never
# nest in the capture code, so their sum is at most callback_ms.
STAGE_DECODE = "decode"                        # frombuffer + reshape of the camera buffer
STAGE_ACCUMULATE = "accumulate"                # series: validate + crop (+ add into the uint32
                                               # sum when nframes > 1)
STAGE_COMPLETE = "complete"                    # series: nframes == 1, convert the frame into a
                                               # pooled float32 mean; nframes > 1, hand the sum
                                               # to the writer (which divides it)
# Not emitted since the sum is no longer zeroed in the callback; kept so older
# diagnostics files (2026-10-04) still summarize in pipeline order.
STAGE_RESET = "reset"                          # series: zero the sum after a completed group
STAGE_ENQUEUE = "enqueue"                      # series: sidecar + writer-queue put + counters
STAGE_LEGACY_ACCUMULATE = "legacy_accumulate"  # legacy: X/XX update (and first allocation)
STAGE_LEGACY_SAVE = "legacy_save"              # legacy: synchronous TIFF + JSON write
STAGES = (STAGE_DECODE, STAGE_ACCUMULATE, STAGE_COMPLETE, STAGE_RESET, STAGE_ENQUEUE,
          STAGE_LEGACY_ACCUMULATE, STAGE_LEGACY_SAVE)


class _StageContext:
    """Times one stage into a StageTimer. Never raises and never hides errors."""

    __slots__ = ("_timer", "_name", "_start")

    def __init__(self, timer, name):
        self._timer = timer
        self._name = name
        self._start = None

    def __enter__(self):
        try:
            self._start = time.perf_counter()
        except Exception:
            self._start = None
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._start is not None:
                self._timer._add(self._name, time.perf_counter() - self._start)
                self._start = None
        except Exception:
            pass
        return False  # exceptions from the timed capture code still propagate


class _NullStage:
    """Shared no-op stage, used when no timer is active."""

    __slots__ = ()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


_NULL_STAGE = _NullStage()


class StageTimer:
    """Per-frame stage durations, in seconds, keyed by stage name.

    Created by the camera callback for one frame only while a diagnostics
    session is active. A stage timed more than once in a frame accumulates.
    Nested stages are each timed inclusively (the capture code does not nest
    them). Nothing here raises into the camera thread.
    """

    __slots__ = ("stages",)

    def __init__(self):
        self.stages = {}

    def stage(self, name):
        """Context manager that adds the time spent in its block to ``name``."""
        return _StageContext(self, name)

    def _add(self, name, seconds):
        self.stages[name] = self.stages.get(name, 0.0) + seconds

    def stages_ms(self):
        """Stage durations in ms (rounded to 0.1 us), or None if nothing was timed."""
        try:
            if not self.stages:
                return None
            return {str(name): round(seconds * 1000, 4) for name, seconds in self.stages.items()}
        except Exception:
            return None


def stage(timer, name):
    """Return a context manager timing ``name`` on ``timer``, or a no-op if timer is None.

    Capture code uses this so that, with diagnostics off, a stage costs one
    comparison and no clock reads.
    """
    if timer is None:
        return _NULL_STAGE
    try:
        return timer.stage(name)
    except Exception:
        return _NULL_STAGE


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
    """Gaps in buffer sequence numbers.

    Not a frame-loss measure on this hardware (see module docstring); kept to
    detect out-of-order or repeated buffers.
    """
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

    ``missing_frames`` and ``gap_count`` are the frame-loss measure. They come
    from sensor-timestamp gaps (None if there are too few timestamps).
    ``sequence`` is reported separately and does not detect dropped frames.
    """
    summary = {
        "frames_recorded": len(records),
        "missing_frames": None,
        "gap_count": None,
        "loss_measure": None,
        "frame_period_ms": None,
        "frames_per_second": None,
        "sequence": _sequence_summary(records, max_listed),
        "timestamp_gaps": None,
        "latency_ms": _stats([r[2] / 1000 for r in records if r[2] is not None]),
        "callback_ms_by_mode": {},
        "callbacks_slower_than_frame_period": None,
    }

    stamps = [r[1] if r[1] is not None else r[3] for r in records]
    sensor_stamps = all(r[1] is not None for r in records)
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
            summary["missing_frames"] = int(sum(missing))
            summary["gap_count"] = int(long_idx.size)
            summary["loss_measure"] = ("sensor_timestamp" if sensor_stamps
                                       else "encoder_timestamp")
            summary["callbacks_slower_than_frame_period"] = sum(
                1 for r in records if r[4] is not None and r[4] * 1000 > period)

    by_mode = {}
    for r in records:
        if r[4] is not None:
            by_mode.setdefault(r[5], []).append(r[4])
    summary["callback_ms_by_mode"] = {m: _stats(v) for m, v in sorted(by_mode.items())}
    summary["stages_ms_by_mode"] = _stage_summary(records)
    return summary


def _record_stages(record):
    """The stages_ms dict of a record, or None (also for older 6-field records)."""
    stages = record[6] if len(record) > 6 else None
    return stages if isinstance(stages, dict) and stages else None


def _stage_summary(records):
    """Per mode: stats for each stage, the timed total and the unattributed rest.

    For each mode, ``stages`` maps stage name -> stats over the frames where that
    stage ran (``count`` says how many). ``timed_ms`` is the per-frame sum of all
    stages, ``unattributed_ms`` is callback_ms minus that sum, and
    ``median_share_of_callback`` is each stage's median divided by the median
    callback_ms of the frames in which that stage ran. Modes with no timed
    frames are omitted.
    """
    per_mode = {}
    for r in records:
        stages = _record_stages(r)
        if stages is None:
            continue
        entry = per_mode.setdefault(r[5], {"stages": {}, "timed": [], "unattributed": [],
                                           "callback": []})
        for name, ms in stages.items():
            if isinstance(ms, (int, float)):
                entry["stages"].setdefault(name, []).append(float(ms))
                if r[4] is not None:
                    entry.setdefault("stage_callbacks", {}).setdefault(name, []).append(r[4])
        timed = sum(ms for ms in stages.values() if isinstance(ms, (int, float)))
        entry["timed"].append(timed)
        if r[4] is not None:
            entry["callback"].append(r[4])
            entry["unattributed"].append(r[4] - timed)

    result = {}
    for mode in sorted(per_mode):
        entry = per_mode[mode]
        stage_stats = {name: _stats(values) for name, values in _ordered_stages(entry["stages"])}
        # Share relative to the callbacks in which the stage ran: in a series at
        # nframes=1 only the frames that complete an image run "complete", so
        # dividing by the median of every series frame would be meaningless.
        share = {}
        stage_callbacks = entry.get("stage_callbacks", {})
        for name, stats in stage_stats.items():
            callbacks = stage_callbacks.get(name)
            median_cb = float(np.median(callbacks)) if callbacks else None
            if median_cb:
                share[name] = round(stats["median"] / median_cb, 3)
        result[mode] = {
            "frames_timed": len(entry["timed"]),
            "stages": stage_stats,
            "sum_of_stage_medians_ms": round(sum(s["median"] for s in stage_stats.values()), 3),
            "timed_ms": _stats(entry["timed"]),
            "unattributed_ms": _stats(entry["unattributed"]),
            "callback_ms": _stats(entry["callback"]),
            "median_share_of_callback": share,
        }
    return result


def _ordered_stages(stages):
    """Known stages in pipeline order first, then any others alphabetically."""
    known = [(name, stages[name]) for name in STAGES if name in stages]
    other = sorted((name, values) for name, values in stages.items() if name not in STAGES)
    return known + other


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

    def record_callback(self, encoder_ts_us, callback_s, mode, timer=None):
        """Called at the end of outputframe() with its duration in seconds.

        ``timer`` is the frame's StageTimer, if stages were timed.
        """
        if not self.active:
            return
        try:
            stages_ms = timer.stages_ms() if timer is not None else None
            with self._lock:
                pending, self._pending = self._pending, None
                if len(self._records) >= self.max_records:
                    self.truncated += 1
                    return
                seq, sensor_ts, latency = pending if pending else (None, None, None)
                self._records.append([seq, sensor_ts, latency, encoder_ts_us,
                                      round(callback_s * 1000, 3), mode, stages_ms])
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
