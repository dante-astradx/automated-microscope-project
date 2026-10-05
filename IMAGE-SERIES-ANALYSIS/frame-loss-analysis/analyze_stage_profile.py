"""Per-mode callback stage table from frame-diagnostics JSON files (read-only).

Reads *_frames.json files written by FrameDiagnostics.stop() with stage timing
(records have a "stages_ms" field) and recomputes the stage summary from the
per-frame records with frame_diagnostics.summarize(), so files from older code
versions are handled the same way. Older files without stage timing are listed
and skipped.

Usage:
  python analyze_stage_profile.py [PATH_OR_GLOB ...] [--csv OUT.csv]

Default input: the newest *_frames.json files in
/home/microscope_auto/Images/frame_diagnostics that contain stage timing.
"""

import argparse
import csv
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))  # project root

import frame_diagnostics as fd  # noqa: E402

DEFAULT_DIR = "/home/microscope_auto/Images/frame_diagnostics"


def load(path):
    with open(path) as fp:
        data = json.load(fp)
    fields = data.get("fields") or []
    if "stages_ms" not in fields:
        return data, None
    index = fields.index("stages_ms")
    # Put records into the current FIELDS layout expected by summarize().
    records = []
    for r in data.get("records") or []:
        base = list(r[:6]) + [None] * (6 - len(r[:6]))
        records.append(base + [r[index] if index < len(r) else None])
    period = (data.get("camera_info") or {}).get("FrameDuration")
    period = period if isinstance(period, (int, float)) and period > 0 else None
    return data, fd.summarize(records, period)


def fmt(value, width=8):
    return f"{value:{width}.2f}" if isinstance(value, (int, float)) else f"{'-':>{width}}"


def print_session(label, summary, rows):
    print(f"\n=== {label}: frames={summary['frames_recorded']} "
          f"missing_frames={summary['missing_frames']} gaps={summary['gap_count']} "
          f"slow_callbacks={summary['callbacks_slower_than_frame_period']}")
    for mode, entry in summary["stages_ms_by_mode"].items():
        cb = entry["callback_ms"]
        print(f"  mode {mode}: {entry['frames_timed']} frames timed, callback median "
              f"{fmt(cb['median'])} p95 {fmt(cb['p95'])} max {fmt(cb['max'])} ms")
        print(f"    {'stage':18s} {'frames':>6s} {'median':>8s} {'p95':>8s} {'max':>8s} "
              f"{'share':>6s}")
        stage_items = list(entry["stages"].items())
        stage_items.append(("(unattributed)", entry["unattributed_ms"]))
        for name, st in stage_items:
            share = entry["median_share_of_callback"].get(name)
            share_text = f"{share * 100:5.1f}%" if share is not None else "     -"
            print(f"    {name:18s} {st['count']:6d} {fmt(st['median'])} {fmt(st['p95'])} "
                  f"{fmt(st['max'])} {share_text}")
            rows.append({"session": label, "mode": mode, "stage": name, "frames": st["count"],
                         "median_ms": st["median"], "p95_ms": st["p95"], "max_ms": st["max"],
                         "share_of_callback_median": share})
        print(f"    sum of stage medians {fmt(entry['sum_of_stage_medians_ms'])} ms")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="files or glob patterns")
    parser.add_argument("--csv", default=None, help="also write the table as CSV")
    args = parser.parse_args()

    patterns = args.paths or [os.path.join(DEFAULT_DIR, "*_frames.json")]
    files = sorted({p for pattern in patterns for p in glob.glob(pattern)})
    if not files:
        print(f"No diagnostics files match {patterns}")
        return 1

    rows, skipped = [], []
    for path in files:
        data, summary = load(path)
        if summary is None or not summary["stages_ms_by_mode"]:
            skipped.append(os.path.basename(path))
            continue
        print_session(data.get("label") or os.path.basename(path), summary, rows)

    if skipped:
        print(f"\nSkipped {len(skipped)} file(s) without stage timing: {', '.join(skipped)}")
    if args.csv and rows:
        with open(args.csv, "w", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"CSV written to {args.csv}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
