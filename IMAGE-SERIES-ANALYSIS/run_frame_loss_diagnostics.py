"""Run capture conditions on the Pi with frame diagnostics on, to find where frames are lost.

Needs the camera service restarted with the frame-diagnostics code. Each
condition is one diagnostics session; the camera service writes a per-frame
JSON file (default <PI_IMAGE_DIR>/frame_diagnostics).

Conditions (all write to --output-root on the SD card; nothing goes to /dev/shm):
  idle     no capture: baseline for the camera and driver
  legacy   legacy accumulate (--legacy-nframes)
  series   start_series (--series-images images of --series-nframes frames)

Frame loss is "missing_frames", computed from sensor-timestamp gaps. Buffer
sequence numbers do not show drops on this Pi. For series conditions, the
series end state (complete/overloaded/...), image counts and error are recorded too.

Memory safety (the Pi crashed on 2026-10-05 when a run used up RAM and swap):
  - The run refuses to start below --min-start-mib of MemAvailable.
  - A guard reads MemAvailable before each condition and every poll (0.5 s)
    while one runs. Below --guard-mib (default 400 MiB) it cancels the running
    series, stops the diagnostics session (keeping what was recorded), skips
    the remaining conditions, and writes the index. A legacy accumulate cannot
    be cancelled through the protocol; the guard waits for it, then stops.
  - Defaults keep RAM small: queue capacity 8 (at most ~380 MiB of queued
    images) and 10 images per series, repeated 3 times.
  - /dev/shm/frame_diag_images (left by older versions of this script) is
    deleted at start and at exit. Test images in --output-root are kept.

How to read the results:
  idle shows gaps          -> camera/driver/system, not our code
  legacy shows gaps        -> legacy callback cost (the save stalls the camera)
  only series shows gaps   -> series per-frame work

Example:
  python IMAGE-SERIES-ANALYSIS/run_frame_loss_diagnostics.py
  python IMAGE-SERIES-ANALYSIS/run_frame_loss_diagnostics.py --conditions series --repeats 5
"""

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import camera  # noqa: E402
import config as c  # noqa: E402

ALL_CONDITIONS = ("idle", "legacy", "series")
DEFAULT_CONDITIONS = ("idle", "legacy", "series")

# Left in RAM by older versions of this script (tmpfs conditions). Always removed.
LEFTOVER_TMPFS_DIR = "/dev/shm/frame_diag_images"

DEFAULT_GUARD_MIB = 400
DEFAULT_MIN_START_MIB = 1500


class MemoryLow(Exception):
    """MemAvailable fell below the guard threshold."""


def mem_available_mib(meminfo_path="/proc/meminfo"):
    """MemAvailable in MiB, or None if it cannot be read."""
    try:
        with open(meminfo_path) as fp:
            for line in fp:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


class MemoryGuard:
    """Tracks MemAvailable during a condition and trips below ``threshold_mib``.

    ``read`` is injectable for tests. An unreadable value never trips the guard.
    """

    def __init__(self, threshold_mib, read=mem_available_mib):
        self.threshold_mib = threshold_mib
        self._read = read
        self.min_mib = None
        self.tripped_mib = None

    def reset(self):
        self.min_mib = None
        self.tripped_mib = None

    def check(self):
        """Raise MemoryLow if MemAvailable is below the threshold."""
        mib = self._read()
        if mib is None:
            return
        self.min_mib = mib if self.min_mib is None else min(self.min_mib, mib)
        if mib < self.threshold_mib:
            self.tripped_mib = mib
            raise MemoryLow(f"MemAvailable {mib:.0f} MiB is below the "
                            f"{self.threshold_mib} MiB guard")


def remove_leftover_tmpfs(path=LEFTOVER_TMPFS_DIR):
    """Delete RAM-backed test images left by older runs. Returns True if removed."""
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
        return True
    return False


def wait_for(predicate, timeout_s, poll_s=0.5, guard=None):
    """Poll ``predicate`` until True or timeout. Checks ``guard`` on every poll."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if guard is not None:
            guard.check()
        if predicate():
            return True
        time.sleep(poll_s)
    return False


SERIES_RESULT_KEYS = ("series_id", "state", "requested_images", "captured_images",
                      "written_images", "discarded_images", "queue_capacity",
                      "queue_high_watermark", "error")


def run_legacy(cam, folder, name, nframes, timeout_s, guard=None):
    """Run one legacy accumulate. Return (finished, None).

    The protocol has no way to cancel a legacy accumulate, so if the guard
    trips, this keeps waiting (without the guard) for the capture and its save
    to finish, then re-raises MemoryLow so no further condition starts.
    """
    os.makedirs(folder, exist_ok=True)
    cam._send_series_command({"command": "accumulate", "nframes": nframes,
                              "filename": name, "file_path": folder})
    time.sleep(1.0)

    def finished():
        status = cam._send_series_command({"command": "status"}) or {}
        return status.get("accumulatingT") is None and status.get("saving") is False

    try:
        return wait_for(finished, timeout_s, guard=guard), None
    except MemoryLow:
        print("  memory guard tripped during legacy accumulate (cannot cancel); "
              "waiting for it to finish")
        wait_for(finished, timeout_s)
        raise


def series_result(status):
    """The parts of a series status reply that explain how it ended."""
    return {k: status.get(k) for k in SERIES_RESULT_KEYS}


def run_series(cam, folder, name, image_count, nframes, queue_capacity, timeout_s,
               guard=None):
    """Run one series. Return (finished, series result dict).

    If the guard trips, the series is cancelled (queued images are still
    written), the end state is collected, and MemoryLow is raised with the
    result attached as ``exc.result``.
    """
    os.makedirs(folder, exist_ok=True)
    reply = cam.start_series(image_count, nframes, name, folder,
                             metadata={"purpose": "frame_loss_diagnostics"},
                             queue_capacity=queue_capacity)
    if not reply or not reply.get("ok"):
        print(f"  start_series rejected: {reply}")
        return False, {"state": "rejected", "error": (reply or {}).get("error")}
    series_id = reply["series_id"]
    last = {}

    def terminal():
        last.update(cam.series_status(series_id) or {})
        return bool(last.get("is_terminal"))

    try:
        finished = wait_for(terminal, timeout_s, guard=guard)
    except MemoryLow as exc:
        print(f"  {exc}; cancelling series {series_id}")
        cam.cancel_series(series_id)
        wait_for(terminal, timeout_s)  # let queued images drain; frees the queue
        result = series_result(last)
        result["series_id"] = series_id
        exc.result = result
        raise
    result = series_result(last)
    result["series_id"] = series_id
    return finished, result


def print_summary(label, reply, series=None):
    s = reply.get("summary") or {}
    seq = s.get("sequence") or {}
    print(f"  [{label}] frames={s.get('frames_recorded')} fps={s.get('frames_per_second')} "
          f"missing_frames={s.get('missing_frames')} gaps={s.get('gap_count')} "
          f"slow_callbacks={s.get('callbacks_slower_than_frame_period')} "
          f"(sequence gaps={seq.get('gap_count')}, not a loss measure)")
    for mode, st in (s.get("callback_ms_by_mode") or {}).items():
        print(f"      callback {mode:12s} median={st['median']} p95={st['p95']} max={st['max']} ms")
    if series:
        print(f"      series: state={series.get('state')} "
              f"captured={series.get('captured_images')}/{series.get('requested_images')} "
              f"written={series.get('written_images')} "
              f"queue high-water={series.get('queue_high_watermark')}/{series.get('queue_capacity')} "
              f"error={series.get('error')}")
    print(f"      file: {reply.get('path')}")


def guarded_sleep(seconds, guard=None, poll_s=0.5):
    """Sleep ``seconds``, checking the guard every ``poll_s``."""
    deadline = time.monotonic() + seconds
    while True:
        if guard is not None:
            guard.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(poll_s, remaining))


def run_condition(cam, args, condition, label, folder, guard=None):
    """Return (finished, series result dict or None). May raise MemoryLow."""
    if condition == "idle":
        guarded_sleep(args.idle_seconds, guard)
        return True, None
    if condition == "legacy":
        return run_legacy(cam, folder, label, args.legacy_nframes, args.timeout, guard)
    return run_series(cam, folder, label, args.series_images, args.series_nframes,
                      args.queue_capacity, args.timeout, guard)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--conditions", nargs="+", choices=ALL_CONDITIONS,
                        default=list(DEFAULT_CONDITIONS))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--legacy-nframes", type=int, default=100)
    parser.add_argument("--series-images", type=int, default=10)
    parser.add_argument("--series-nframes", type=int, default=1)
    parser.add_argument("--idle-seconds", type=float, default=10.0)
    parser.add_argument("--settle-seconds", type=float, default=2.0,
                        help="extra time recorded after each capture (catches slow saves)")
    parser.add_argument("--output-root", default=os.path.join(c.PI_IMAGE_DIR, "FRAMEDIAG"))
    parser.add_argument("--diagnostics-dir",
                        default=os.path.join(c.PI_IMAGE_DIR, "frame_diagnostics"))
    parser.add_argument("--queue-capacity", type=int, default=8,
                        help="writer-queue capacity for series conditions (images; "
                             "each is about 47 MiB of RAM)")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--guard-mib", type=float, default=DEFAULT_GUARD_MIB,
                        help="stop the run if MemAvailable falls below this (MiB)")
    parser.add_argument("--min-start-mib", type=float, default=DEFAULT_MIN_START_MIB,
                        help="refuse to start below this MemAvailable (MiB)")
    args = parser.parse_args()
    return run(args)


def run(args, cam=None, guard=None):
    """Run every condition in ``args``. Returns the process exit code.

    0: all done; 1: diagnostics could not start; 2: camera stalled;
    3: not enough memory to start; 4: stopped early by the memory guard.
    """
    removed = remove_leftover_tmpfs()
    if removed:
        print(f"Removed leftover {LEFTOVER_TMPFS_DIR}")
    guard = guard or MemoryGuard(args.guard_mib)
    start_mib = guard._read()
    if start_mib is not None and start_mib < args.min_start_mib:
        print(f"Not starting: MemAvailable is {start_mib:.0f} MiB, below "
              f"--min-start-mib {args.min_start_mib:.0f}. Free memory (close VS Code "
              f"windows, stop transfers) and retry.")
        return 3
    if start_mib is not None:
        print(f"MemAvailable at start: {start_mib:.0f} MiB (guard {args.guard_mib:.0f} MiB)")

    cam = cam or camera.Camera()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(args.diagnostics_dir, exist_ok=True)
    index_path = os.path.join(args.diagnostics_dir, f"{stamp}_index.json")
    results = []

    def write_index():
        # Rewritten after every condition so an interrupted run keeps its index.
        with open(index_path, "w") as fp:
            json.dump(results, fp, indent=2)

    plan = [(r, cond) for r in range(1, args.repeats + 1) for cond in args.conditions]
    exit_code = 0
    try:
        for position, (repeat, condition) in enumerate(plan):
            label = f"{condition}_r{repeat}"
            folder = os.path.join(args.output_root, f"{stamp}_{label}")
            guard.reset()
            try:
                guard.check()
            except MemoryLow as exc:
                print(f"Stopping before {label}: {exc}")
                results.append({"label": label, "skipped": True, "reason": str(exc)})
                exit_code = 4
                break
            print(f"Running {label} ...")
            started = cam.start_frame_diagnostics(label=label) or {}
            if not started.get("ok"):
                print(f"  could not start diagnostics (camera service restarted?): {started}")
                exit_code = 1
                break
            ok, series, memory_low = False, None, None
            try:
                ok, series = run_condition(cam, args, condition, label, folder, guard)
                guarded_sleep(args.settle_seconds, guard)
            except MemoryLow as exc:
                memory_low = str(exc)
                series = getattr(exc, "result", series)
            finally:
                reply = cam.stop_frame_diagnostics(args.diagnostics_dir) or {}
            if not ok and memory_low is None:
                print(f"  WARNING: {label} did not finish within {args.timeout} s")
            print_summary(label, reply, series)
            if guard.min_mib is not None:
                print(f"      lowest MemAvailable during {label}: {guard.min_mib:.0f} MiB")
            results.append({"label": label, "finished": ok, "path": reply.get("path"),
                            "series": series, "summary": reply.get("summary"),
                            "min_mem_available_mib": guard.min_mib,
                            "memory_guard_tripped": memory_low})
            write_index()
            if memory_low is not None:
                remaining = [f"{c}_r{r}" for r, c in plan[position + 1:]]
                print(f"  STOPPED by memory guard: {memory_low}. "
                      f"Skipped: {', '.join(remaining) or 'none'}")
                for name in remaining:
                    results.append({"label": name, "skipped": True, "reason": memory_low})
                exit_code = 4
                break
            if (reply.get("summary") or {}).get("frames_recorded") == 0:
                print("  ERROR: no frames were delivered; the camera appears stalled. "
                      "Check the camera service log, restart it, then rerun.")
                exit_code = 2
                break
    finally:
        write_index()
        remove_leftover_tmpfs()
        print(f"Index written to {index_path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
