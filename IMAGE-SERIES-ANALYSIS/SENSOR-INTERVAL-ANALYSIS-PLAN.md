# Image Series Analysis: Sensor Interval Plotting Plan

> **Update: the script is now generalized.** The sections below describe the
> original QUEUE-capacity analysis, which the script still reproduces exactly.
> What changed:
>
> - **Discovery comes from the sidecars, not folder names.** Every directory
>   under `--images-root` is a dataset, and every sub-folder holding
>   `_image<N>.json` / `_series<S>_image<N>.json` sidecars with
>   `sensor_timestamps_us` is a run. Image count, series count, requested
>   pause, nframes and exposure are read from the sidecars. The folder-suffix
>   table below is no longer used. Legacy captures without timestamps, and
>   empty folders, are skipped and listed.
> - **Choosing data:** `--dataset PATTERN` (repeatable, shell patterns such as
>   `'SERIES*'`) and `--magnification N`. With no options, every dataset is
>   analyzed.
> - **Repeated-series batches:** images are grouped by `series_index`.
>   Within-series intervals and the 95%-within-5% check are computed per
>   series. The gap from the last frame of series i to the first frame of
>   series i+1 is measured from timestamps, cross-checked against the
>   recorded `inter_series_gap_us`, and compared with the requested pause.
>   Gaps are never mixed into the within-series statistics.
> - **Outputs (in `plots/`):**
>   - `<dataset>/<dataset>_run<n>_within_series_intervals.png`, one per run
>   - `<dataset>/<dataset>_run<n>_inter_series_gaps.png`, batches only
>   - `<dataset>/<dataset>_within_series_overview.png`, labelled by whichever
>     settings vary between runs
>   - `summary_inter_series_gaps.png`, every gap against its requested pause
>   - `summary_queue_median_interval.png`, `QUEUE<capacity>` datasets only
>   - `within_series_summary.csv`, one row per series (replaces
>     `interval_summary.csv`)
>   - `inter_series_gaps.csv`, one row per boundary
> - **Queue capacity** is still taken from a `QUEUE<capacity>_` dataset name,
>   because those older captures did not record it in the sidecars.
> - **Rounding:** the per-run within-series plots show intervals rounded to
>   1 ms (`PLOT_ROUND_MS`), with no axis offset and a y-range of at least 10 ms
>   (`PLOT_MIN_SPAN_MS`), so sub-millisecond jitter looks flat. The CSVs,
>   statistics and PASS/FAIL use full-precision values.

## Purpose

Measure how consistently the camera captured consecutive images in each
continuous series test, using the `sensor_timestamps_us` field in the per-image
JSON sidecars. For each run, plot the interval between consecutive images and
report the mean, median and standard deviation. Check the result against the
acceptance criterion below.

This plan covers sensor timing only. Queue peak, process RSS maximum, available
RAM minimum and overload points were recorded on paper during the tests. They
are not in the saved files and are not part of these plots. They can be
transcribed into a table later if they are needed for comparison.

## Test conditions

Constant for every run: `nframes = 1`, magnification 20x, exposure time
30000 µs. The variables were queue capacity and requested image count.

## Input data

Root: `/home/microscope_auto/Images`

Capacity directories are named `QUEUE<capacity>_20260930_M1`. On disk at the
time of writing: `QUEUE5`, `QUEUE10`, `QUEUE15`, `QUEUE25`, `QUEUE50`.
Capacity 35 was not tested, so there is no `QUEUE35` directory; it is
disregarded for now. The analysis must report which capacities it found
rather than assuming a fixed list, so a later `QUEUE35` run is picked up
automatically.

Each capacity directory has six run folders,
`QUEUE<capacity>_20260930_M1_unstained_SM1_20x_<n>`, one per requested image
count:

| Suffix `<n>` | Requested image count |
|---:|---:|
| 1 | 5 |
| 2 | 10 |
| 3 | 15 |
| 4 | 25 |
| 5 | 35 |
| 6 | 50 |

Each capacity directory also holds `manifest.json` and a `*_quality.csv`. These
are not series data and are ignored. The folder generator also created six
empty `..._SM1_40x_<n>` folders per capacity. Only 20x folders are analyzed
(`--magnification` changes this). Skipped folders are listed in the output,
not silently ignored.

### Files inside a run folder

- **Image sidecars to use:** `..._NAx_NAy_NAz_image<N>.json`, one per written
  TIFF. Relevant fields: `series_id`, `image_index`, `image_count` (the
  requested count), `frames`, `exposure_time`, `sensor_timestamps_us` (a list
  with one value per contributing frame, in microseconds).
- **Exclude:** the z-stack JSON named after the folder itself (for example
  `QUEUE5_20260930_M1_unstained_SM1_20x_1.json`). It has no timestamps. Only
  files matching `_image<N>.json` are image sidecars.

## Analysis steps

### 1. Discover and identify runs

1. Find every `QUEUE<capacity>_*` directory. Read the capacity from its name.
2. In each, find the run folders and read the suffix `<n>`. Map it to the
   requested image count using the table above.
3. Cross-check the mapping against the sidecars' `image_count` field. If they
   disagree, flag the run and trust the sidecar value.
4. Check that every sidecar in a run has the same `series_id`. More than one
   ID means the folder mixes runs, so flag it and do not analyze it silently.

### 2. Load and validate timestamps

1. Read every `_image<N>.json` sidecar in the run.
2. Sort by numeric `image_index`, not by filename; alphabetically, `image10`
   would sort before `image2`. Fall back to the filename number only if
   `image_index` is missing, and flag that.
3. Validate and flag (never silently drop):
   - malformed JSON or a missing `sensor_timestamps_us`
   - `frames` not equal to 1, or a timestamp list whose length is not `frames`
   - duplicate `image_index` values
   - missing image numbers (gaps) between 1 and the highest index found
   - timestamps that do not increase
4. Keep a count of how many images were loaded for each run.

### 3. Classify each run's outcome

The terminal status for each run is not available, so infer the outcome from
the saved files:

- **Complete:** the number of valid sidecars equals the requested
  `image_count`, with no gaps.
- **Overloaded / incomplete:** fewer sidecars than requested. From the paper
  notes this is expected to be a queue overload. The analysis labels it
  "incomplete" and marks it as overload-related where the paper record
  confirms that.

Snapshot of saved image counts taken when this plan was written (used only
as a sanity check):

| Capacity | 5 | 10 | 15 | 25 | 35 | 50 |
|---:|---:|---:|---:|---:|---:|---:|
| 5 | 5 | 10 | 13 | 12 | 11 | 12 |
| 10 | 5 | 10 | 15 | 18 | 17 | 18 |
| 15 | 5 | 10 | 15 | 23 | 25 | 23 |
| 25 | 5 | 10 | 15 | 25 | 34 | 34 |
| 50 | 5 | 10 | 15 | 25 | 35 | 50 |

Column headings are requested image counts; the cells are the image sidecars
found. Any cell smaller than its heading is an incomplete run.

### 4. Calculate intervals

Calculate intervals only within a single run. Never take a difference
between timestamps from different runs: each series has its own timestamp
origin, so absolute values are not comparable. Intervals are.

For consecutive images `i` and `i + 1` (by `image_index`):

```text
interval_ms[i] = (timestamp[i + 1] - timestamp[i]) / 1000
```

- A run with `N` valid images gives `N - 1` intervals.
- **Gaps:** if images between two saved images are missing, the interval
  across the gap spans more than one image step. Mark it as a gap interval,
  show it separately on the plot, and exclude it from the statistics and the
  acceptance check. Report how many intervals were excluded.
- For incomplete (overloaded) runs, use the intervals up to the last saved
  image. They describe only the part of the run that was captured.

### 5. Summary statistics (per run)

From the valid, non-gap intervals of each run:

- interval count
- mean (ms)
- median (ms)
- standard deviation (ms): sample standard deviation (`ddof=1`). This
  choice is stated in the output.
- minimum and maximum interval (ms)
- first-half vs second-half median, to show drift within a run
- 5th and 95th percentile interval (ms)

A run with fewer than 2 intervals gets no standard deviation (reported as
not available).

### 6. Acceptance criterion

For each run, at least 95% of intervals must be within 5% of that run's own
median:

```text
within = abs(interval_ms - median_ms) <= 0.05 * median_ms
fraction_within = count(within) / interval_count
acceptable = fraction_within >= 0.95
```

- The check uses each run's own median. It is never pooled across runs or
  capacities.
- **Complete runs** get a PASS or FAIL.
- **Incomplete (overloaded) runs** report `fraction_within` as partial-run
  information only. They are labelled "incomplete", never PASS, because the
  requested series was not captured.
- Small runs have little data. A 5-image run has only 4 intervals, so
  meeting 95% means all 4 must be within tolerance. Always report the
  interval count next to the percentage.

## Outputs

All outputs go in a directory called `plots` inside `IMAGE-SERIES-ANALYSIS`:

```text
/home/microscope_auto/project_files/IMAGE-SERIES-ANALYSIS/plots/
```

Create `plots` if it does not exist. Never write outputs into the raw
image directories under `/home/microscope_auto/Images`. The analysis only
reads those.

### Plot 1: interval vs image index (one plot per run)

- x-axis: interval number, labelled by the ending image index (`i + 1`)
- y-axis: interval (ms)
- horizontal line at the run's median
- shaded band or dashed lines at ±5% of the median
- intervals outside the band highlighted
- gap intervals, if any, drawn with a distinct marker
- title: capacity, requested image count, images saved, outcome
  (complete / incomplete) and PASS / FAIL / partial fraction within
  tolerance
- filename example: `plots/QUEUE5_count25_intervals.png`

### Plot 2: all image counts for one capacity (one plot per capacity)

- the interval traces for all six image counts, overlaid on shared axes
- complete runs drawn with solid lines, incomplete (overloaded) runs with
  dashed lines and a distinct marker, and labelled as incomplete in the legend
- filename example: `plots/QUEUE5_all_counts_intervals.png`

### Plot 3: run summary comparison

- median interval for every run, grouped by queue capacity, one series per
  requested image count
- error bars for the 5th to 95th percentile interval range. Standard
  deviation was tried first, but the intervals are strongly skewed (mostly
  ~100 ms with occasional 200 to 4000 ms gaps), so median ± SD went below
  zero, which is impossible for a time interval. SD stays in the CSV.
- incomplete runs visually distinguished (for example, hollow markers)
- filename: `plots/summary_median_interval.png`

### Summary table

`plots/interval_summary.csv`, one row per run:

- capacity, run folder, requested image count, images saved, series ID
- outcome (complete / incomplete)
- interval count, excluded gap intervals
- mean, median, standard deviation, minimum, maximum (ms)
- first-half median, second-half median (ms)
- percentage of intervals within ±5% of the median
- result: PASS, FAIL, or INCOMPLETE
- data-quality flags

## Implementation notes

- Use Python with numpy and matplotlib. Both are available in
  `/home/microscope_auto/pyenv/rpitest_v2` (matplotlib 3.6.3,
  numpy 1.24.2). Use the non-interactive `Agg` matplotlib backend so
  plots can be written without a display.
- Put the analysis script in `IMAGE-SERIES-ANALYSIS/`. It reads from
  `/home/microscope_auto/Images` and writes only to
  `IMAGE-SERIES-ANALYSIS/plots/`.
- Make the input root and output directory arguments, with these paths
  as defaults.

## Verification

Before drawing conclusions:

1. Hand-check several calculated intervals against timestamps read
   directly from their JSON sidecars, including at least one incomplete run.
2. Confirm the images-saved counts in the summary match the files on disk.
3. Confirm the z-stack JSON and the capacity-level files were not treated
   as image sidecars.
4. Review each per-run plot for gaps, outliers or drift that the summary
   statistics could hide.

## Limitations

- Sensor timestamps measure when each frame's exposure started. They do not
  measure file-write time or queue delay.
- Memory, queue and overload measurements are on paper only, so the plots
  cannot show them.
- Each capacity and image count was run once. A single run cannot show
  run-to-run variation, so repeat runs are needed before treating a result as
  a reliable operating limit.

