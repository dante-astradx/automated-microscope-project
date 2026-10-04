#!/usr/bin/env python3
"""Sensor-interval analysis for continuous series and repeated-series captures.

Implements SENSOR-INTERVAL-ANALYSIS-PLAN.md (in this directory). Datasets and
their settings are discovered from the per-image JSON sidecars rather than from
folder names, so new experiments (other image counts, series counts, pauses,
exposures, ...) need no script changes.

For every run folder it reports:
  - within-series intervals: sensor-timestamp differences between consecutive
    images of the same series, with the 95%-within-5%-of-median check per series;
  - inter-series gaps (repeated-series batches only): last frame of series i to
    first frame of series i+1, compared with the requested minimum pause.
Gaps are never mixed into the within-series statistics.

Outputs go to IMAGE-SERIES-ANALYSIS/plots/ (one sub-directory per dataset plus
cross-dataset summaries). The raw image folders are only read, never written.

Usage:
    python analyze_sensor_intervals.py [--images-root DIR] [--dataset PATTERN ...]
                                       [--output-dir DIR] [--magnification N]
"""

import argparse
import csv
import fnmatch
import json
import os
import re
from dataclasses import dataclass, field

import numpy as np
import matplotlib

matplotlib.use("Agg")  # write files without a display
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402

DEFAULT_IMAGES_ROOT = "/home/microscope_auto/Images"
DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plots")

# Acceptance: at least 95% of within-series intervals within 5% of that
# series' own median.
TOLERANCE = 0.05
REQUIRED_FRACTION = 0.95

# Within-series interval plots show values rounded to this many ms, so
# sub-millisecond jitter does not dominate the y-axis. Statistics, CSVs and
# PASS/FAIL always use the full-precision values.
PLOT_ROUND_MS = 1
# Minimum y-axis span (ms) for those plots, so a flat trace is not zoomed in.
PLOT_MIN_SPAN_MS = 10

# Per-image sidecars only: "..._image<N>.json" or "..._series<S>_image<N>.json".
# The z-stack JSON named after the folder does not match.
SIDECAR_RE = re.compile(r"(?:_series(\d+))?_image(\d+)\.json$")
# Run folders end in _<magnification>x_<n>, e.g. ..._SM1_20x_3.
RUN_DIR_RE = re.compile(r"_(\d+)x_(\d+)$")
# Optional: a dataset named QUEUE<capacity>_... records the queue capacity used
# (older single-series tests did not save it in the sidecars).
QUEUE_DIR_RE = re.compile(r"^QUEUE(\d+)_")

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_INCOMPLETE = "INCOMPLETE"
RESULT_REVIEW = "REVIEW"
RESULT_NO_DATA = "NO DATA"

KIND_VALID = "valid"
KIND_GAP = "gap"
KIND_NON_INCREASING = "non_increasing"


@dataclass
class Image:
    index: int              # image index within its series (1-based)
    timestamp_us: int       # first contributing frame
    last_timestamp_us: int  # last contributing frame (same as first if nframes=1)
    filename: str
    recorded_gap_us: object = None  # inter_series_gap_us from the sidecar, if any


@dataclass
class Interval:
    end_index: int  # image index at the end of the interval (i + 1)
    ms: float
    kind: str       # valid, gap, or non_increasing


@dataclass
class Series:
    """One series: a single-series run, or one series of a batch."""
    index: int                # 1 for single-series runs
    requested: object = None  # images requested in this series
    images: list = field(default_factory=list)
    intervals: list = field(default_factory=list)
    missing_indices: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    complete: bool = False
    result: str = RESULT_NO_DATA


@dataclass
class Boundary:
    """Gap between the last image of one series and the first of the next."""
    before_series: int
    requested_us: object
    measured_us: object  # from timestamps; None if a needed image is missing
    recorded_us: object  # inter_series_gap_us written by the camera, if any
    note: str = ""

    @property
    def gap_us(self):
        return self.measured_us if self.measured_us is not None else self.recorded_us


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _common_value(records, key, flags):
    """The single value of ``key`` across records; flags disagreement."""
    values = {json.dumps(r.get(key), sort_keys=True) for r in records if key in r}
    if len(values) > 1:
        flags.append(f"sidecars disagree on {key}: {sorted(values)}")
        return None
    return json.loads(values.pop()) if values else None


@dataclass
class Run:
    """One run folder: a single series, or a batch of repeated series."""
    dataset: str
    folder: str
    path: str
    suffix: object
    magnification: object = None
    settings: dict = field(default_factory=dict)  # values read from the sidecars
    batch_id: object = None
    series_ids: set = field(default_factory=set)
    series: list = field(default_factory=list)
    boundaries: list = field(default_factory=list)
    flags: list = field(default_factory=list)

    @property
    def is_batch(self):
        return self.batch_id is not None

    @property
    def series_count(self):
        return self.settings.get("series_count") or max(len(self.series), 1)

    @property
    def images_saved(self):
        return sum(len(s.images) for s in self.series)

    @property
    def requested_total(self):
        per = self.settings.get("image_count")
        return None if per is None else per * self.series_count

    @property
    def complete(self):
        indices = {s.index for s in self.series}
        return (indices == set(range(1, self.series_count + 1))
                and all(s.complete for s in self.series))

    @property
    def outcome(self):
        return "complete" if self.complete else "incomplete"

    @property
    def result(self):
        """Worst series result; a run is never PASS unless it is complete."""
        if not self.series:
            return RESULT_NO_DATA
        results = {s.result for s in self.series}
        for result in (RESULT_REVIEW, RESULT_INCOMPLETE, RESULT_FAIL, RESULT_NO_DATA):
            if result in results:
                return result
        return RESULT_PASS if self.complete else RESULT_INCOMPLETE

    @property
    def pause_s(self):
        pause = self.settings.get("inter_series_pause_us")
        return None if pause is None else pause / 1e6

    @property
    def queue_capacity(self):
        match = QUEUE_DIR_RE.match(self.dataset)
        return int(match.group(1)) if match else None

    @property
    def slug(self):
        """Short, unique name used in plot filenames."""
        return f"{self.dataset}_run{self.suffix}" if self.suffix is not None else self.folder

    @property
    def label(self):
        return f"run {self.suffix}" if self.suffix is not None else self.folder

    def describe(self):
        """Human-readable settings, built only from values that were recorded."""
        per = self.settings.get("image_count")
        if self.is_batch:
            parts = [f"{self.series_count} series x {per} images"]
            if self.pause_s is not None:
                parts.append(f"pause >= {self.pause_s:g} s")
        else:
            parts = [f"{per} images"]
        if self.queue_capacity is not None:
            parts.append(f"queue {self.queue_capacity}")
        if self.settings.get("frames") is not None:
            parts.append(f"nframes {self.settings['frames']}")
        if self.settings.get("exposure_time") is not None:
            parts.append(f"exposure {self.settings['exposure_time'] / 1000:g} ms")
        if self.magnification is not None:
            parts.append(f"{self.magnification}x")
        return ", ".join(parts)


def read_sidecars(run_path):
    """Return (records, flags). Each record is the sidecar dict plus '_name'
    and '_series_from_name'/'_image_from_name' parsed from the filename."""
    records, flags = [], []
    for name in sorted(os.listdir(run_path)):
        match = SIDECAR_RE.search(name)
        if not match:
            continue
        try:
            with open(os.path.join(run_path, name)) as fp:
                data = json.load(fp)
        except (OSError, ValueError) as e:
            flags.append(f"{name}: unreadable JSON ({type(e).__name__})")
            continue
        if not isinstance(data, dict):
            flags.append(f"{name}: JSON is not an object")
            continue
        data["_name"] = name
        data["_series_from_name"] = int(match.group(1)) if match.group(1) else None
        data["_image_from_name"] = int(match.group(2))
        records.append(data)
    return records, flags


def _to_image(record, flags):
    """Convert one sidecar record to an Image, or None (flagged) if unusable."""
    name = record["_name"]
    index = record.get("image_index")
    if not _is_int(index):
        flags.append(f"{name}: image_index missing; used the filename number")
        index = record["_image_from_name"]
    elif index != record["_image_from_name"]:
        flags.append(f"{name}: image_index {index} differs from filename "
                     f"number {record['_image_from_name']}")
    timestamps = record.get("sensor_timestamps_us")
    if (not isinstance(timestamps, list) or not timestamps
            or not all(_is_int(t) for t in timestamps)):
        flags.append(f"{name}: missing or malformed sensor_timestamps_us")
        return None
    frames = record.get("frames")
    if _is_int(frames) and len(timestamps) != frames:
        flags.append(f"{name}: {len(timestamps)} timestamp(s) for frames={frames}")
    gap = record.get("inter_series_gap_us")
    return Image(index, timestamps[0], timestamps[-1], name,
                 gap if _is_int(gap) else None)


def compute_intervals(images):
    """Intervals (ms) between consecutive saved images of ONE series.

    An interval that skips missing images is a gap; a zero or negative one is
    non-increasing. Only valid intervals are used for statistics.
    """
    intervals = []
    for prev, cur in zip(images, images[1:]):
        ms = (cur.timestamp_us - prev.timestamp_us) / 1000.0
        if cur.index - prev.index > 1:
            kind = KIND_GAP
        elif ms <= 0:
            kind = KIND_NON_INCREASING
        else:
            kind = KIND_VALID
        intervals.append(Interval(cur.index, ms, kind))
    return intervals


def compute_stats(values):
    """Summary statistics for a list of valid intervals (ms)."""
    stats = {
        "interval_count": len(values),
        "mean_ms": None, "median_ms": None, "std_ms": None,
        "min_ms": None, "max_ms": None,
        "first_half_median_ms": None, "second_half_median_ms": None,
        "p5_ms": None, "p95_ms": None,
        "within_count": None, "fraction_within": None,
    }
    if not values:
        return stats
    arr = np.asarray(values, dtype=float)
    median = float(np.median(arr))
    within = np.abs(arr - median) <= TOLERANCE * median
    stats.update({
        "mean_ms": float(np.mean(arr)),
        "median_ms": median,
        # Sample standard deviation; undefined for a single interval.
        "std_ms": float(np.std(arr, ddof=1)) if arr.size >= 2 else None,
        "min_ms": float(np.min(arr)),
        "max_ms": float(np.max(arr)),
        "p5_ms": float(np.percentile(arr, 5)),
        "p95_ms": float(np.percentile(arr, 95)),
        "within_count": int(np.count_nonzero(within)),
        "fraction_within": float(np.count_nonzero(within)) / arr.size,
    })
    if arr.size >= 2:
        half = arr.size // 2
        stats["first_half_median_ms"] = float(np.median(arr[:half]))
        stats["second_half_median_ms"] = float(np.median(arr[half:]))
    return stats


def build_series(index, images, requested, flags):
    """Sort, de-duplicate and evaluate one series."""
    series = Series(index=index, requested=requested)
    label = f"series {index}: " if index is not None else ""
    for image in sorted(images, key=lambda img: img.index):
        if series.images and series.images[-1].index == image.index:
            flags.append(f"{label}duplicate image_index {image.index} in {image.filename}; "
                         f"kept {series.images[-1].filename}")
            continue
        series.images.append(image)
    if series.images:
        # Internal gaps only. Images absent after the last saved one (e.g.
        # after an overload) are covered by the incomplete check instead.
        present = {img.index for img in series.images}
        series.missing_indices = [
            i for i in range(1, series.images[-1].index + 1) if i not in present]
        if series.missing_indices:
            flags.append(f"{label}missing image indices: {series.missing_indices}")
    series.intervals = compute_intervals(series.images)
    bad = [iv.end_index for iv in series.intervals if iv.kind == KIND_NON_INCREASING]
    if bad:
        flags.append(f"{label}non-increasing timestamps ending at image(s) {bad}")
    series.stats = compute_stats([iv.ms for iv in series.intervals if iv.kind == KIND_VALID])
    series.complete = (requested is not None and len(series.images) == requested
                       and not series.missing_indices)
    if series.stats["interval_count"] == 0:
        series.result = RESULT_NO_DATA
    elif not series.complete:
        series.result = RESULT_INCOMPLETE
    elif series.stats["fraction_within"] >= REQUIRED_FRACTION:
        series.result = RESULT_PASS
    else:
        series.result = RESULT_FAIL
    return series


def build_boundaries(run, by_index):
    """Measure the gap before each series 2..N of a batch."""
    requested = run.settings.get("inter_series_pause_us")
    for index in range(2, run.series_count + 1):
        prev, cur = by_index.get(index - 1), by_index.get(index)
        first = next((img for img in cur.images if img.index == 1), None) if cur else None
        recorded = first.recorded_gap_us if first else None
        measured, note = None, ""
        prev_last = None
        if prev and prev.images and prev.requested is not None:
            prev_last = next((img for img in prev.images if img.index == prev.requested), None)
        if prev_last and first:
            # The camera schedules the pause from the LAST frame of the previous
            # series' last image to the FIRST frame of the next series.
            measured = first.timestamp_us - prev_last.last_timestamp_us
            if recorded is not None and recorded != measured:
                note = f"recorded {recorded} us differs from timestamps ({measured} us)"
                run.flags.append(f"gap before series {index}: {note}")
        elif cur is None and prev is None:
            continue  # neither side was captured; nothing to report
        else:
            note = "image needed to measure this gap is missing"
        run.boundaries.append(Boundary(index, requested, measured, recorded, note))


def analyze_run(dataset, run_path, magnification=None):
    folder = os.path.basename(run_path)
    match = RUN_DIR_RE.search(folder)
    run = Run(dataset=dataset, folder=folder, path=run_path,
              suffix=int(match.group(2)) if match else None,
              magnification=magnification)
    records, run.flags = read_sidecars(run_path)

    for key in ("image_count", "series_count", "inter_series_pause_us",
                "frames", "exposure_time", "analogue_gain", "batch_id"):
        run.settings[key] = _common_value(records, key, run.flags)
    run.batch_id = run.settings.pop("batch_id")
    run.series_ids = {r["series_id"] for r in records if r.get("series_id") is not None}
    if run.is_batch and run.settings.get("series_count") is None:
        run.flags.append("batch sidecars have no series_count; using the series found")
    if not run.is_batch and len(run.series_ids) > 1:
        # Older single-series data: more than one series in a folder is a mistake.
        run.flags.append(f"folder mixes {len(run.series_ids)} series IDs: {sorted(run.series_ids)}")

    # Group images by series. Batches record series_index; single series are series 1.
    grouped = {}
    for record in records:
        image = _to_image(record, run.flags)
        if image is None:
            continue
        if run.is_batch:
            index = record.get("series_index")
            if not _is_int(index):
                index = record["_series_from_name"]
                run.flags.append(f"{record['_name']}: series_index missing; used the filename")
            elif record["_series_from_name"] not in (None, index):
                run.flags.append(f"{record['_name']}: series_index {index} differs from filename")
        else:
            index = 1
        grouped.setdefault(index, []).append(image)

    requested = run.settings.get("image_count")
    for index in sorted(grouped):
        if run.is_batch and not 1 <= index <= run.series_count:
            run.flags.append(f"series index {index} is outside 1..{run.series_count}")
        run.series.append(build_series(index, grouped[index], requested, run.flags))
    if run.is_batch:
        missing = sorted(set(range(1, run.series_count + 1)) - set(grouped))
        if missing:
            run.flags.append(f"no images saved for series {missing}")
        build_boundaries(run, {s.index: s for s in run.series})

    if not run.is_batch and len(run.series_ids) > 1:
        for series in run.series:
            series.result = RESULT_REVIEW  # mixed runs are not analyzed as one series
    return run


def _sidecar_names(path):
    try:
        return [n for n in os.listdir(path) if SIDECAR_RE.search(n)]
    except OSError:
        return []


def _has_timestamps(path, names):
    """True if a sidecar records sensor timestamps (legacy captures do not)."""
    for name in names[:3]:
        try:
            with open(os.path.join(path, name)) as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "sensor_timestamps_us" in data:
            return True
    return False


def discover_runs(images_root, datasets=None, magnification=None):
    """Find run folders with timestamped sidecars under ``images_root``.

    A dataset is any directory directly under ``images_root``; its run folders
    are the sub-directories that contain per-image sidecars. ``datasets`` is an
    optional list of shell-style patterns (e.g. "SERIES*") to select datasets;
    ``magnification`` keeps only ..._<N>x_<n> folders for that objective.

    Returns (runs, skipped) where skipped is a list of (folder, reason).
    """
    runs, skipped = [], []
    for dataset in sorted(os.listdir(images_root)):
        dataset_path = os.path.join(images_root, dataset)
        if not os.path.isdir(dataset_path):
            continue
        if datasets and not any(fnmatch.fnmatch(dataset, p) for p in datasets):
            continue
        try:
            entries = sorted(os.listdir(dataset_path))
        except OSError:
            continue
        for name in entries:
            run_path = os.path.join(dataset_path, name)
            if not os.path.isdir(run_path):
                continue
            label = f"{dataset}/{name}"
            match = RUN_DIR_RE.search(name)
            run_mag = int(match.group(1)) if match else None
            names = _sidecar_names(run_path)
            if magnification is not None and run_mag != magnification:
                if names:  # empty folders for unused objectives are not worth listing
                    skipped.append((label, f"{run_mag}x, not {magnification}x"))
                continue
            if not names:
                if datasets:  # only report empty folders in datasets asked for
                    skipped.append((label, "no image sidecars"))
                continue
            if not _has_timestamps(run_path, names):
                skipped.append((label, "sidecars have no sensor_timestamps_us (legacy capture)"))
                continue
            runs.append(analyze_run(dataset, run_path, run_mag))
    runs.sort(key=lambda r: (r.dataset, r.suffix if r.suffix is not None else 0, r.folder))
    return runs, skipped


# --- Plots -------------------------------------------------------------------

def _fmt(value, digits=2):
    return "n/a" if value is None else f"{value:.{digits}f}"


def _series_text(series):
    s = series.stats
    if s["fraction_within"] is None:
        return series.result
    pct = f"{100 * s['fraction_within']:.0f}% within ±{TOLERANCE:.0%} ({s['within_count']}/{s['interval_count']})"
    return f"{series.result}: {pct}"


def _save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def _colors(n):
    return plt.cm.viridis(np.linspace(0, 0.85, max(n, 1)))


def _plot_ms(value):
    """Round an interval for display only (see PLOT_ROUND_MS)."""
    return round(value / PLOT_ROUND_MS) * PLOT_ROUND_MS


def plot_run_intervals(run, output_dir):
    """Within-series intervals for one run; each series separately.

    Plotted values are rounded to PLOT_ROUND_MS. Which points count as outside
    the tolerance is still decided from the full-precision values.
    """
    fig, ax = plt.subplots(figsize=(9, 5))
    plotted = []
    for color, series in zip(_colors(len(run.series)), run.series):
        valid = [iv for iv in series.intervals if iv.kind == KIND_VALID]
        median = series.stats["median_ms"]
        name = f"series {series.index}" if run.is_batch else "intervals"
        label = f"{name} ({_series_text(series)})"
        ys = [_plot_ms(iv.ms) for iv in valid]
        plotted.extend(ys)
        ax.plot([iv.end_index for iv in valid], ys,
                color=color, linewidth=1.0, alpha=0.8, marker="o", markersize=5,
                linestyle="-" if series.complete else "--", label=label)
        if median is not None:
            lo, hi = median * (1 - TOLERANCE), median * (1 + TOLERANCE)
            if not run.is_batch:
                ax.axhspan(lo, hi, color="tab:green", alpha=0.15, label=f"±{TOLERANCE:.0%} of median")
                ax.axhline(_plot_ms(median), color="tab:green", linewidth=1.2,
                           label=f"median {_plot_ms(median):g} ms")
            outside = [iv for iv in valid if not lo <= iv.ms <= hi]
            if outside:
                ax.scatter([iv.end_index for iv in outside], [_plot_ms(iv.ms) for iv in outside],
                           facecolors="none", edgecolors="tab:red", s=90, zorder=5)
        for kind, marker, mcolor in ((KIND_GAP, "x", "tab:orange"),
                                     (KIND_NON_INCREASING, "v", "black")):
            pts = [iv for iv in series.intervals if iv.kind == kind]
            if pts:
                ys = [_plot_ms(iv.ms) for iv in pts]
                plotted.extend(ys)
                ax.scatter([iv.end_index for iv in pts], ys,
                           marker=marker, color=mcolor, s=60, zorder=6,
                           label=f"{name}: {kind.replace('_', '-')} (excluded)")
    ax.scatter([], [], facecolors="none", edgecolors="tab:red", s=90,
               label=f"outside ±{TOLERANCE:.0%} of its series median")
    if plotted:
        # Keep at least PLOT_MIN_SPAN_MS of y range so a flat 100 ms trace is
        # shown as flat, not stretched over sub-millisecond ticks.
        low, high = min(plotted), max(plotted)
        if high - low < PLOT_MIN_SPAN_MS:
            middle = (high + low) / 2
            low, high = middle - PLOT_MIN_SPAN_MS / 2, middle + PLOT_MIN_SPAN_MS / 2
        pad = 0.05 * (high - low)
        ax.set_ylim(low - pad, high + pad)
    ax.ticklabel_format(axis="y", useOffset=False, style="plain")  # no "+1e2" offset
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    ax.set_xlabel("Image index within series (interval from image i-1 to i)")
    ax.set_ylabel(f"Within-series sensor interval (ms, rounded to {PLOT_ROUND_MS:g} ms)")
    ax.set_title(f"{run.dataset} {run.label}: {run.describe()}\n"
                 f"saved {run.images_saved}/{run.requested_total} images ({run.outcome}) - {run.result}",
                 fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, loc="best")
    return _save(fig, os.path.join(output_dir, f"{run.slug}_within_series_intervals.png"))


def plot_run_gaps(run, output_dir):
    """Inter-series gaps for one batch run against the requested pause."""
    fig, ax = plt.subplots(figsize=(7, 4.5))
    measured = [b for b in run.boundaries if b.gap_us is not None]
    xs = [b.before_series for b in measured]
    ax.bar(xs, [b.gap_us / 1000 for b in measured], color="tab:blue", width=0.5,
           label="measured gap")
    for b in measured:
        ax.annotate(f"{b.gap_us / 1000:.1f} ms", (b.before_series, b.gap_us / 1000),
                    ha="center", va="bottom", fontsize=8)
    for b in run.boundaries:
        if b.gap_us is None:
            ax.annotate("not measurable", (b.before_series, 0), ha="center", va="bottom",
                        fontsize=8, color="tab:red")
    requested = run.settings.get("inter_series_pause_us")
    if requested is not None:
        ax.axhline(requested / 1000, color="tab:red", linestyle="--",
                   label=f"requested minimum {requested / 1000:g} ms")
    ax.set_xticks(range(2, run.series_count + 1))
    ax.set_xticklabels([f"{i - 1} → {i}" for i in range(2, run.series_count + 1)])
    ax.set_xlabel("Series boundary")
    ax.set_ylabel("Gap: last frame of series i to first frame of series i+1 (ms)")
    ax.set_title(f"{run.dataset} {run.label}: inter-series gaps\n{run.describe()}", fontsize=10)
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(fontsize=8, loc="lower right")
    return _save(fig, os.path.join(output_dir, f"{run.slug}_inter_series_gaps.png"))


# Settings that may differ between runs of a dataset, in label-preference order.
VARIABLES = (
    ("inter_series_pause_us", "pause", lambda v: f"{v / 1e6:g} s"),
    ("series_count", "series", str),
    ("image_count", "images", str),
    ("frames", "nframes", str),
    ("exposure_time", "exposure", lambda v: f"{v / 1000:g} ms"),
)


def run_variable_label(run, runs):
    """Label a run by whichever recorded settings vary across ``runs``."""
    parts = []
    for key, name, fmt in VARIABLES:
        values = {r.settings.get(key) for r in runs}
        value = run.settings.get(key)
        if len(values) > 1 and value is not None:
            parts.append(f"{name} {fmt(value)}")
    if len({r.queue_capacity for r in runs}) > 1 and run.queue_capacity is not None:
        parts.append(f"queue {run.queue_capacity}")
    return ", ".join(parts) or run.label


def plot_dataset_overview(dataset, runs, output_dir):
    """Within-series medians (and range) for every series of every run."""
    fig, ax = plt.subplots(figsize=(10, 5.5))
    series_max = max((r.series_count for r in runs), default=1)
    colors = _colors(series_max)
    width = 0.8 / series_max
    labels = []
    for i, run in enumerate(runs):
        labels.append(f"{run.label}\n{run_variable_label(run, runs)}")
        for series in run.series:
            s = series.stats
            if s["median_ms"] is None or not 1 <= series.index <= series_max:
                continue
            color = colors[series.index - 1]
            x = i + (series.index - 1 - (series_max - 1) / 2) * width
            ax.errorbar(x, s["median_ms"],
                        yerr=[[s["median_ms"] - s["min_ms"]], [s["max_ms"] - s["median_ms"]]],
                        fmt="o", color=color, capsize=3, markersize=7,
                        markerfacecolor=color if series.result == RESULT_PASS else "white")
    for index in range(1, series_max + 1):
        name = f"series {index}" if series_max > 1 else "median"
        ax.errorbar([], [], fmt="o", color=colors[index - 1], label=name)
    ax.errorbar([], [], fmt="o", color="gray", markerfacecolor="white",
                label="hollow = not PASS (fail/incomplete)")
    ax.set_xticks(range(len(runs)))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("Within-series interval (ms): median, bars = min to max")
    ax.set_title(f"{dataset}: within-series sensor intervals per run and series", fontsize=10)
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(fontsize=8, loc="best")
    return _save(fig, os.path.join(output_dir, f"{dataset}_within_series_overview.png"))


def plot_gap_summary(runs, output_dir):
    """Every measured inter-series gap against its requested pause, all datasets."""
    batch_runs = [r for r in runs if r.is_batch and r.boundaries
                  and r.settings.get("inter_series_pause_us") is not None]
    if not batch_runs:
        return None
    fig, (ax, ax_err) = plt.subplots(1, 2, figsize=(12, 5))
    datasets = sorted({r.dataset for r in batch_runs})
    top = 0
    for dataset, color, marker in zip(datasets, _colors(len(datasets)), "osD^v<>p*h" * 10):
        points = [(r.settings["inter_series_pause_us"] / 1000, b.gap_us / 1000)
                  for r in batch_runs if r.dataset == dataset
                  for b in r.boundaries if b.gap_us is not None]
        if not points:
            continue
        req, meas = zip(*points)
        top = max(top, max(req), max(meas))
        ax.scatter(req, meas, color=color, marker=marker, s=50, label=dataset, zorder=3)
        ax_err.scatter(req, [m - r for r, m in points], color=color, marker=marker,
                       s=50, label=dataset, zorder=3)
    ax.plot([0, top * 1.05], [0, top * 1.05], color="tab:red", linestyle="--",
            label="measured = requested")
    ax.set_xlabel("Requested minimum pause (ms)")
    ax.set_ylabel("Measured inter-series gap (ms)")
    ax.set_title("Inter-series gap vs requested pause", fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    ax_err.axhline(0, color="tab:red", linestyle="--")
    ax_err.set_xlabel("Requested minimum pause (ms)")
    ax_err.set_ylabel("Measured - requested (ms)")
    ax_err.set_title("Gap beyond the requested pause\n"
                     "(0 to about one frame interval is expected)", fontsize=10)
    ax_err.grid(True, alpha=0.3)
    return _save(fig, os.path.join(output_dir, "summary_inter_series_gaps.png"))


def plot_queue_summary(runs, output_dir):
    """Median interval per run for QUEUE<capacity> single-series datasets."""
    queue_runs = [r for r in runs if r.queue_capacity is not None and not r.is_batch]
    if not queue_runs:
        return None
    capacities = sorted({r.queue_capacity for r in queue_runs})
    counts = sorted({r.settings.get("image_count") for r in queue_runs
                     if r.settings.get("image_count") is not None})
    fig, ax = plt.subplots(figsize=(10, 5.5))
    width = 0.8 / max(len(counts), 1)
    by_key = {(r.queue_capacity, r.settings.get("image_count")): r for r in queue_runs}
    for j, (count, color) in enumerate(zip(counts, _colors(len(counts)))):
        offset = (j - (len(counts) - 1) / 2) * width
        for i, capacity in enumerate(capacities):
            run = by_key.get((capacity, count))
            if run is None or not run.series or run.series[0].stats["median_ms"] is None:
                continue
            s = run.series[0].stats
            # 5th-95th percentile: intervals are skewed, median ± SD can go below zero.
            ax.errorbar(i + offset, s["median_ms"],
                        yerr=[[s["median_ms"] - s["p5_ms"]], [s["p95_ms"] - s["median_ms"]]],
                        fmt="o", color=color, capsize=3, markersize=7,
                        markerfacecolor=color if run.complete else "white")
        ax.errorbar([], [], fmt="o", color=color, label=f"count {count}")
    ax.errorbar([], [], fmt="o", color="gray", markerfacecolor="white",
                label="hollow = incomplete (overloaded)")
    ax.set_xticks(range(len(capacities)))
    ax.set_xticklabels([f"QUEUE{c}" for c in capacities])
    ax.set_xlabel("Queue capacity")
    ax.set_ylabel("Median sensor interval (ms)")
    ax.set_title("Median sensor interval per run "
                 "(error bars = 5th to 95th percentile; SD is in the CSV)", fontsize=10)
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(fontsize=8, loc="best")
    return _save(fig, os.path.join(output_dir, "summary_queue_median_interval.png"))


# --- Summary tables and entry point ---------------------------------------------

SERIES_FIELDS = [
    "dataset", "run_folder", "batch_id", "series_index", "series_id",
    "series_count", "inter_series_pause_s", "queue_capacity",
    "frames", "exposure_time_us", "magnification",
    "requested_images", "images_saved", "outcome",
    "interval_count", "excluded_intervals",
    "mean_ms", "median_ms", "std_ms", "min_ms", "max_ms",
    "first_half_median_ms", "second_half_median_ms", "p5_ms", "p95_ms",
    "percent_within_5pct", "result", "flags",
]

GAP_FIELDS = [
    "dataset", "run_folder", "batch_id", "boundary", "requested_pause_ms",
    "measured_gap_ms", "recorded_gap_ms", "gap_minus_requested_ms",
    "meets_minimum", "note",
]


def _round(value, digits=3):
    return "" if value is None else round(value, digits)


def _blank(value):
    return "" if value is None else value


def write_series_csv(runs, output_dir):
    """One row per series (single-series runs have one row)."""
    path = os.path.join(output_dir, "within_series_summary.csv")
    with open(path, "w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=SERIES_FIELDS)
        writer.writeheader()
        for run in runs:
            for series in run.series:
                s = series.stats
                prefix = f"series {series.index}: "
                flags = [f for f in run.flags
                         if not run.is_batch or not f.startswith("series ") or f.startswith(prefix)]
                series_id = (f"{run.batch_id}-s{series.index}" if run.is_batch
                             else ";".join(sorted(run.series_ids)))
                writer.writerow({
                    "dataset": run.dataset,
                    "run_folder": run.folder,
                    "batch_id": _blank(run.batch_id),
                    "series_index": series.index,
                    "series_id": series_id,
                    "series_count": run.series_count,
                    "inter_series_pause_s": _blank(run.pause_s),
                    "queue_capacity": _blank(run.queue_capacity),
                    "frames": _blank(run.settings.get("frames")),
                    "exposure_time_us": _blank(run.settings.get("exposure_time")),
                    "magnification": _blank(run.magnification),
                    "requested_images": _blank(series.requested),
                    "images_saved": len(series.images),
                    "outcome": "complete" if series.complete else "incomplete",
                    "interval_count": s["interval_count"],
                    "excluded_intervals": sum(1 for iv in series.intervals if iv.kind != KIND_VALID),
                    "mean_ms": _round(s["mean_ms"]),
                    "median_ms": _round(s["median_ms"]),
                    "std_ms": _round(s["std_ms"]),
                    "min_ms": _round(s["min_ms"]),
                    "max_ms": _round(s["max_ms"]),
                    "first_half_median_ms": _round(s["first_half_median_ms"]),
                    "second_half_median_ms": _round(s["second_half_median_ms"]),
                    "p5_ms": _round(s["p5_ms"]),
                    "p95_ms": _round(s["p95_ms"]),
                    "percent_within_5pct": _round(
                        None if s["fraction_within"] is None else 100 * s["fraction_within"], 1),
                    "result": series.result,
                    "flags": " | ".join(flags),
                })
    return path


def write_gap_csv(runs, output_dir):
    """One row per inter-series boundary of every batch run."""
    path = os.path.join(output_dir, "inter_series_gaps.csv")
    with open(path, "w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=GAP_FIELDS)
        writer.writeheader()
        for run in runs:
            for b in run.boundaries:
                gap, req = b.gap_us, b.requested_us
                writer.writerow({
                    "dataset": run.dataset,
                    "run_folder": run.folder,
                    "batch_id": run.batch_id,
                    "boundary": f"{b.before_series - 1}->{b.before_series}",
                    "requested_pause_ms": _round(None if req is None else req / 1000),
                    "measured_gap_ms": _round(None if b.measured_us is None else b.measured_us / 1000),
                    "recorded_gap_ms": _round(None if b.recorded_us is None else b.recorded_us / 1000),
                    "gap_minus_requested_ms": _round(
                        None if gap is None or req is None else (gap - req) / 1000),
                    "meets_minimum": "" if gap is None or req is None else gap >= req,
                    "note": b.note,
                })
    return path


def print_summary(runs, skipped):
    datasets = sorted({r.dataset for r in runs})
    print(f"Datasets analyzed: {', '.join(datasets) or 'none'}")
    if skipped:
        print(f"Skipped {len(skipped)} folder(s):")
        for folder, reason in skipped:
            print(f"  {folder}: {reason}")
    for dataset in datasets:
        print(f"\n== {dataset}")
        for run in (r for r in runs if r.dataset == dataset):
            print(f"{run.label}: {run.describe()} | saved {run.images_saved}/"
                  f"{run.requested_total} ({run.outcome}) | {run.result}")
            for series in run.series:
                s = series.stats
                within = "" if s["fraction_within"] is None else f"{100 * s['fraction_within']:.0f}%"
                name = f"series {series.index}" if run.is_batch else "intervals"
                print(f"    {name:<10} saved {len(series.images):>3}  n={s['interval_count']:<3} "
                      f"median {_fmt(s['median_ms']):>8} ms  min {_fmt(s['min_ms']):>8}  "
                      f"max {_fmt(s['max_ms']):>8}  within {within:>4}  {series.result}")
            for b in run.boundaries:
                gap = "n/a" if b.gap_us is None else f"{b.gap_us / 1000:.1f} ms"
                req = "n/a" if b.requested_us is None else f"{b.requested_us / 1000:g} ms"
                print(f"    gap {b.before_series - 1}->{b.before_series}: {gap} (requested >= {req})"
                      f"{'  ' + b.note if b.note else ''}")
            for flag in run.flags:
                print(f"    flag: {flag}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images-root", default=DEFAULT_IMAGES_ROOT,
                        help=f"folder containing dataset directories (default {DEFAULT_IMAGES_ROOT})")
    parser.add_argument("--dataset", action="append", metavar="PATTERN",
                        help="dataset directory name or shell pattern, e.g. 'SERIES*'. "
                             "Repeatable. Default: every dataset with timestamped sidecars")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help="where plots and CSVs are written (default: plots/ next to this script)")
    parser.add_argument("--magnification", type=int, default=None,
                        help="only analyze ..._<N>x_<n> run folders for this objective "
                             "(default: all; empty folders are ignored)")
    args = parser.parse_args(argv)

    images_root = os.path.abspath(args.images_root)
    output_dir = os.path.abspath(args.output_dir)
    if os.path.commonpath([images_root, output_dir]) == images_root:
        parser.error("--output-dir must not be inside --images-root; raw data is read-only")
    if not os.path.isdir(images_root):
        parser.error(f"--images-root {images_root} does not exist")

    runs, skipped = discover_runs(images_root, args.dataset, args.magnification)
    if not runs:
        wanted = f" matching {args.dataset}" if args.dataset else ""
        parser.error(f"no run folders with timestamped image sidecars found{wanted} "
                     f"under {images_root}")
    os.makedirs(output_dir, exist_ok=True)

    written = []
    for dataset in sorted({r.dataset for r in runs}):
        dataset_runs = [r for r in runs if r.dataset == dataset]
        dataset_dir = os.path.join(output_dir, dataset)
        os.makedirs(dataset_dir, exist_ok=True)
        for run in dataset_runs:
            written.append(plot_run_intervals(run, dataset_dir))
            if run.is_batch and run.series_count > 1:
                written.append(plot_run_gaps(run, dataset_dir))
        written.append(plot_dataset_overview(dataset, dataset_runs, dataset_dir))
    written.append(plot_gap_summary(runs, output_dir))
    written.append(plot_queue_summary(runs, output_dir))
    written.append(write_series_csv(runs, output_dir))
    written.append(write_gap_csv(runs, output_dir))
    written = [p for p in written if p]

    print_summary(runs, skipped)
    print(f"\nWrote {len(written)} files to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
