"""Analyze per-frame diagnostics JSON files (read-only) and print a report.

Usage: python analyze_frame_loss.py [DIAG_DIR]
Writes summary_table.csv, gaps.csv and plots next to this script.
"""
import csv
import glob
import json
import os
import sys

import numpy as np

DIAG_DIR = sys.argv[1] if len(sys.argv) > 1 else "/home/microscope_auto/Images/frame_diagnostics"
OUT = os.path.dirname(os.path.abspath(__file__))


def stats(a):
    a = np.asarray(a, dtype=float)
    if a.size == 0:
        return dict(n=0, median=None, p95=None, max=None)
    return dict(n=int(a.size), median=round(float(np.median(a)), 3),
                p95=round(float(np.percentile(a, 95)), 3), max=round(float(a.max()), 3))


def analyze(path):
    d = json.load(open(path))
    fields = d["fields"]
    recs = d["records"]
    col = {f: [r[i] for r in recs] for i, f in enumerate(fields)}
    seq = np.array([s for s in col["sequence"]], dtype=float)
    ts = np.array([t for t in col["sensor_ts_us"]], dtype=float)
    lat = np.array([x for x in col["latency_us"] if x is not None], dtype=float) / 1000
    cb = np.array(col["callback_ms"], dtype=float)
    modes = col["mode"]
    period_ms = d["camera_info"].get("FrameDuration", 100000) / 1000
    out = dict(label=d["label"], file=os.path.basename(path), elapsed_s=d["elapsed_s"],
               frames=len(recs), period_ms=period_ms, camera_info=d["camera_info"],
               unpaired=d.get("unpaired_requests"), truncated=d.get("truncated_records"))
    gaps = []
    if len(recs) > 1:
        dseq = np.diff(seq)
        dts = np.diff(ts) / 1000
        for i in np.where((dseq != 1) | (dts > 1.5 * period_ms))[0]:
            gaps.append(dict(label=d["label"], index=int(i + 1), seq_before=int(seq[i]),
                             seq_after=int(seq[i + 1]), seq_missing=int(dseq[i] - 1),
                             interval_ms=round(float(dts[i]), 3),
                             ts_missing=int(round(dts[i] / period_ms)) - 1,
                             mode_before=modes[i], mode_after=modes[i + 1],
                             cb_before_ms=float(cb[i]),
                             max_cb_prev5_ms=float(cb[max(0, i - 4):i + 1].max()),
                             lat_before_ms=float(lat[i]) if i < len(lat) else None,
                             lat_after_ms=float(lat[i + 1]) if i + 1 < len(lat) else None))
        out["seq_missing"] = int(sum(max(0, x - 1) for x in dseq))
        out["ts_missing"] = int(sum(max(0, round(x / period_ms) - 1) for x in dts))
        good = dts[dseq == 1]
        out["interval_good"] = stats(good)
        # drift: first vs last third of intervals between consecutive sequences
        if good.size >= 6:
            k = good.size // 3
            out["interval_first_third"] = round(float(np.mean(good[:k])), 4)
            out["interval_last_third"] = round(float(np.mean(good[-k:])), 4)
        out["interval_multiples"] = sorted(set(int(round(x / period_ms)) for x in dts))
    else:
        out["seq_missing"] = out["ts_missing"] = None
    out["gaps"] = len(gaps)
    out["latency"] = stats(lat)
    if lat.size >= 6:
        k = lat.size // 3
        out["lat_first_third"] = round(float(lat[:k].mean()), 2)
        out["lat_last_third"] = round(float(lat[-k:].mean()), 2)
    out["callback"] = stats(cb)
    out["callback_by_mode"] = {m: stats(cb[[x == m for x in modes]]) for m in sorted(set(modes))}
    out["slow_callbacks"] = int((cb > period_ms).sum())
    out["mode_counts"] = {m: modes.count(m) for m in sorted(set(modes))}
    out["summary_file"] = d.get("summary")
    return out, gaps, dict(seq=seq, ts=ts, lat=lat, cb=cb, modes=modes)


def plot(results, raw):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available; skipping plots")
        return
    n = len(results)
    fig, axes = plt.subplots(n, 3, figsize=(15, 2.4 * n), squeeze=False)
    for row, (res, r) in enumerate(zip(results, raw)):
        if len(r["ts"]) < 2:
            axes[row][0].set_title(f"{res['label']}: {res['frames']} frames")
            continue
        t = (r["ts"] - r["ts"][0]) / 1e6
        axes[row][0].plot(t[1:], np.diff(r["ts"]) / 1000, ".")
        axes[row][0].set_ylabel(f"{res['label']}\ninterval ms")
        axes[row][1].plot(t[:len(r["lat"])], r["lat"], ".")
        axes[row][1].set_ylabel("latency ms")
        colors = {"idle": "grey", "legacy": "tab:blue", "legacy_save": "tab:red", "series": "tab:green"}
        for m in set(r["modes"]):
            idx = [i for i, x in enumerate(r["modes"]) if x == m]
            axes[row][2].plot(t[idx], r["cb"][idx], ".", color=colors.get(m), label=m)
        axes[row][2].axhline(res["period_ms"], ls="--", color="k", lw=0.8)
        axes[row][2].set_yscale("symlog", linthresh=1)
        axes[row][2].set_ylabel("callback ms")
        axes[row][2].legend(fontsize=7)
        for ax in axes[row]:
            ax.ticklabel_format(useOffset=False, axis="y") if ax.get_yscale() == "linear" else None
    for ax in axes[-1]:
        ax.set_xlabel("seconds from first frame")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "per_session_timelines.png"), dpi=90)
    print("plot: per_session_timelines.png")


def main():
    files = sorted(glob.glob(os.path.join(DIAG_DIR, "*_frames.json")))
    results, all_gaps, raw = [], [], []
    for f in files:
        res, gaps, r = analyze(f)
        results.append(res)
        all_gaps.extend(gaps)
        raw.append(r)
    cols = ["label", "frames", "elapsed_s", "seq_missing", "ts_missing", "gaps", "cb_median",
            "cb_p95", "cb_max", "slow_callbacks", "lat_median", "lat_first_third",
            "lat_last_third", "unpaired", "truncated"]
    with open(os.path.join(OUT, "summary_table.csv"), "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(cols)
        for r in results:
            w.writerow([r["label"], r["frames"], r["elapsed_s"], r["seq_missing"], r["ts_missing"],
                        r["gaps"], r["callback"]["median"], r["callback"]["p95"],
                        r["callback"]["max"], r["slow_callbacks"], r["latency"]["median"],
                        r.get("lat_first_third"), r.get("lat_last_third"),
                        r["unpaired"], r["truncated"]])
    if all_gaps:
        with open(os.path.join(OUT, "gaps.csv"), "w", newline="") as fp:
            w = csv.DictWriter(fp, fieldnames=list(all_gaps[0].keys()))
            w.writeheader()
            w.writerows(all_gaps)
    for r in results:
        summary = r.pop("summary_file") or {}
        summary["timestamp_gaps"] = summary.get("timestamp_gaps") or {}
        print(json.dumps({k: v for k, v in r.items() if k != "camera_info"}))
        print("   camera_info:", json.dumps(r["camera_info"]))
        print("   file summary seq/ts:", json.dumps((summary or {}).get("sequence")),
              json.dumps((summary or {}).get("timestamp_gaps", {}).get("estimated_missing_frames")))
    print("GAPS")
    for g in all_gaps:
        print(json.dumps(g))
    with open(os.path.join(OUT, "analysis_results.json"), "w") as fp:
        json.dump({"sessions": results, "gaps": all_gaps}, fp, indent=2)
    plot(results, raw)


if __name__ == "__main__":
    main()
