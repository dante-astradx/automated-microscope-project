"""Run capture conditions on the Pi with frame diagnostics on, to find where frames are lost.

Needs the camera service restarted with the frame-diagnostics code. Each
condition is one diagnostics session; the camera service writes a per-frame
JSON file (default <PI_IMAGE_DIR>/frame_diagnostics).

Conditions:
  idle          no capture: baseline for the camera and driver
  legacy        legacy accumulate (nframes) to --output-root
  legacy_tmpfs  legacy accumulate to /dev/shm (no SD-card writes)
  series        start_series to --output-root
  series_tmpfs  start_series to /dev/shm

How to read the results:
  idle shows gaps               -> camera/driver/system, not our code
  legacy shows gaps             -> upstream of the series code (callback cost, buffers)
  only series shows gaps        -> series per-frame work
  only non-tmpfs runs show gaps -> storage writes are stalling the camera thread

Example:
  python IMAGE-SERIES-ANALYSIS/run_frame_loss_diagnostics.py --conditions idle legacy series
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import camera  # noqa: E402
import config as c  # noqa: E402

ALL_CONDITIONS = ("idle", "legacy", "legacy_tmpfs", "series", "series_tmpfs")
TMPFS_DIR = "/dev/shm/frame_diag_images"


def wait_for(predicate, timeout_s, poll_s=0.5):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return False


def run_legacy(cam, folder, name, nframes, timeout_s):
    os.makedirs(folder, exist_ok=True)
    cam._send_series_command({"command": "accumulate", "nframes": nframes,
                              "filename": name, "file_path": folder})
    time.sleep(1.0)

    def finished():
        status = cam._send_series_command({"command": "status"}) or {}
        return status.get("accumulatingT") is None and status.get("saving") is False

    return wait_for(finished, timeout_s)


def run_series(cam, folder, name, image_count, nframes, timeout_s):
    os.makedirs(folder, exist_ok=True)
    reply = cam.start_series(image_count, nframes, name, folder,
                             metadata={"purpose": "frame_loss_diagnostics"})
    if not reply or not reply.get("ok"):
        print(f"  start_series rejected: {reply}")
        return False
    series_id = reply["series_id"]
    return wait_for(lambda: (cam.series_status(series_id) or {}).get("is_terminal"), timeout_s)


def print_summary(label, reply):
    s = reply.get("summary") or {}
    seq = s.get("sequence") or {}
    gaps = s.get("timestamp_gaps") or {}
    print(f"  [{label}] frames={s.get('frames_recorded')} fps={s.get('frames_per_second')} "
          f"missing(seq)={seq.get('missing_frames')} "
          f"missing(ts)={gaps.get('estimated_missing_frames')} "
          f"slow_callbacks={s.get('callbacks_slower_than_frame_period')}")
    for mode, st in (s.get("callback_ms_by_mode") or {}).items():
        print(f"      callback {mode:12s} median={st['median']} p95={st['p95']} max={st['max']} ms")
    print(f"      file: {reply.get('path')}")


def run_condition(cam, args, condition, label, folder):
    if condition == "idle":
        time.sleep(args.idle_seconds)
        return True
    if condition.startswith("legacy"):
        return run_legacy(cam, folder, label, args.legacy_nframes, args.timeout)
    return run_series(cam, folder, label, args.series_images, args.series_nframes, args.timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--conditions", nargs="+", choices=ALL_CONDITIONS,
                        default=list(ALL_CONDITIONS))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--legacy-nframes", type=int, default=100)
    parser.add_argument("--series-images", type=int, default=15)
    parser.add_argument("--series-nframes", type=int, default=1)
    parser.add_argument("--idle-seconds", type=float, default=10.0)
    parser.add_argument("--settle-seconds", type=float, default=2.0,
                        help="extra time recorded after each capture (catches slow saves)")
    parser.add_argument("--output-root", default=os.path.join(c.PI_IMAGE_DIR, "FRAMEDIAG"))
    parser.add_argument("--diagnostics-dir",
                        default=os.path.join(c.PI_IMAGE_DIR, "frame_diagnostics"))
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    cam = camera.Camera()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []
    for repeat in range(1, args.repeats + 1):
        for condition in args.conditions:
            label = f"{condition}_r{repeat}"
            root = TMPFS_DIR if condition.endswith("_tmpfs") else args.output_root
            folder = os.path.join(root, f"{stamp}_{label}")
            print(f"Running {label} ...")
            started = cam.start_frame_diagnostics(label=label) or {}
            if not started.get("ok"):
                print(f"  could not start diagnostics (camera service restarted?): {started}")
                return 1
            try:
                ok = run_condition(cam, args, condition, label, folder)
                time.sleep(args.settle_seconds)
            finally:
                reply = cam.stop_frame_diagnostics(args.diagnostics_dir) or {}
            if not ok:
                print(f"  WARNING: {label} did not finish within {args.timeout} s")
            print_summary(label, reply)
            results.append({"label": label, "finished": ok, "path": reply.get("path"),
                            "summary": reply.get("summary")})

    os.makedirs(args.diagnostics_dir, exist_ok=True)
    index_path = os.path.join(args.diagnostics_dir, f"{stamp}_index.json")
    with open(index_path, "w") as fp:
        json.dump(results, fp, indent=2)
    print(f"Index written to {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
