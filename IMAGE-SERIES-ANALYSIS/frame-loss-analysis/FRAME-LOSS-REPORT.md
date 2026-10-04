# Frame-loss diagnostics report (run of 2026-10-04 17:20)

Data: `/home/microscope_auto/Images/frame_diagnostics/*_frames.json` (11 sessions).
Produced by `analyze_frame_loss.py` (in this folder), which writes `summary_table.csv`,
`gaps.csv`, `analysis_results.json` and `per_session_timelines.png`.
Hardware and settings: Pi 4 Model B, IMX477 at 4056x3040 SRGGB12, FrameDuration 100 ms, buffer_count 6.

## Summary table (times in ms; "slow cb" = callbacks longer than 100 ms)

| session | frames | missing (seq) | missing (ts) | gaps | cb median | cb p95 | cb max | slow cb | latency median |
|---|---|---|---|---|---|---|---|---|---|
| idle_r1 | 120 | 0 | 0 | 0 | 0.10 | 0.11 | 0.6 | 0 | 157 |
| legacy_r1 | 126 | 0 | 9 | 1 | 74.8 | 82.6 | 1153 | 2 | 133 |
| legacy_tmpfs_r1 | 127 | 0 | 8 | 1 | 66.9 | 75.3 | 1105 | 2 | 132 |
| series_r1 | 80 | 0 | 8 | 3 | 0.07 | 176 | 211 | 10 | 129 |
| series_tmpfs_r1 | 44 | 0 | 18 | 6 | 0.07 | 214 | 410 | 15 | 129 |
| idle_r2 | 120 | 0 | 0 | 0 | 0.11 | 0.12 | 0.15 | 0 | 156 |
| legacy_r2 | 129 | 0 | 16 | 1 | 76.1 | 89.0 | 1921 | 3 | 133 |
| legacy_tmpfs_r2 | 126 | 0 | 10 | 1 | 86.6 | 94.1 | 1251 | 5 | 143 |
| series_r2 | 75 | 0 | 12 | 3 | 0.08 | 182 | 800 | 8 | 129 |
| series_tmpfs_r2 | 12 | 0 | 6 | 2 | 191 | 2717 | 5777 | 11 | 511 |
| idle_r3 | 0 | - | - | - | - | - | - | - | - |

Callback time by mode:
- **legacy accumulate:** median 67-87 ms, p95 75-95 ms. This is under one frame period.
- **legacy_save:** 1105-1921 ms.
- **series, while producing images:** 155-215 ms, with one 800 ms and one 5777 ms.

Totals: 87 frames lost in 18 gaps. Every one of those gaps falls in a legacy or series session.

## Findings

1. **Sequence numbers did not detect any loss in this setup.** They are
   contiguous in every session, but sensor timestamps jump by exact multiples
   of 100 ms. So this sequence number counts delivered buffers, not sensor
   frames. Use timestamp gaps as the loss measure and ignore
   `sequence.missing_frames`. This is a flaw in the diagnostics design.
2. **There was no loss when idle.** idle_r1 and idle_r2 have no gaps, and
   intervals are 100.000 ms ± 0.013 ms. The sensor, driver and OS deliver every
   frame when the callback is cheap. Loss is caused by work in our callback.
3. **Legacy images are intact, but the save step loses frames.** The 100
   accumulated frames are contiguous in all four legacy runs. Loss happens only
   in the save callback (1.1–1.9 s), which writes the TIFF inside the camera
   callback. Afterwards, about 4 frames that were already queued arrive with
   latency of up to 1.9 s, then 8–16 frames are missing. Any capture that
   overlapped with, or started just after, a save would lose frames.
4. **Series capture loses frames inside the series.** With nframes=1, each
   output image costs 155–215 ms of callback time, about 2× the frame period.
   The first ~6 frames arrive with rising latency (the 6 buffers absorb the
   backlog), then frames drop (gaps of 300, 500 and 600 ms). The series
   sidecars confirm this independently: their within-series intervals of 300,
   500 and 600 ms match the diagnostics gaps exactly. This explains the earlier
   300 ms interval in SERIES3 run 1.
5. **Storage is not the main cause.** Legacy save time is similar on tmpfs
   (1105 and 1251 ms) and on the SD card (1153 and 1921 ms). The cost is mostly
   CPU work (mean and variance, float32 conversion, TIFF encoding), with the SD
   card adding some variance. Series callback cost is the same on both. The SD
   card series lost fewer frames only because they ended early: 9 and 7 of 15
   images were written, versus 15 and 11 on tmpfs. *Suggested, not proven:* the
   SD card writer could not keep up with the 3-image queue, and those series
   failed with "queue full". The runner did not record the series end state.
6. **Slow callbacks come before every gap.** All 18 gaps follow a callback
   longer than 100 ms within the previous 5 frames (`gaps.csv`, column
   `max_cb_prev5_ms`). Drops start about 6 frames after the slow callbacks
   begin, which matches a 6-buffer pool filling up.
7. **Latency rises during slow callbacks, then recovers.** The baseline is
   about 129 ms. There is also a step to about 157 ms in both idle sessions,
   unrelated to loss. Latency grows to 0.6–0.95 s during slow series callbacks
   and to 1–2 s after a legacy save, then returns to baseline. This is a
   temporary backlog, not drift.
8. **The frame rate does not degrade.** Intervals between consecutive delivered
   frames stay at 100.0 ms, and every long interval is an exact multiple of
   100 ms. Frames are dropped; the sensor never slows down.
9. **The camera stalled in series_tmpfs_r2.** One callback took 5.8 s. At
   17:22:52 the journal shows `Dequeue timer ... expired` and
   `Camera frontend has timed out!`. The session ran for 302 s (the runner
   timeout) and recorded only 12 frames; idle_r3 then recorded 0. When I
   checked, `/dev/shm` held 1.4 GB (79 % full), available RAM was 472 MB and
   272 MB of swap was in use. *Likely, not proven:* image data in tmpfs used up
   RAM, the capture process swapped, and the callback stalled long enough to
   trip the libcamera frontend timeout. This is why r3 did not complete.

## Diagnosis

**Proven by the data:** frames are lost because callback work takes longer than
the 100 ms frame period and exhausts the 6-buffer pool. There are two sources:

- the series per-image completion inside the callback (about 155–215 ms per
  image at nframes=1)
- the synchronous legacy TIFF save (about 1–2 s per image)

The sensor and driver are not the cause (idle sessions are clean). Storage I/O
is not the main cause of callback cost (tmpfs and SD card perform similarly).

**Suggested, not proven:**

- the early-ended SD card series failed with a full writer queue
- memory pressure from tmpfs caused the camera timeout

## Next steps (ranked)

1. **Recover the system:** restart the camera service and delete
   `/dev/shm/frame_diag_images` (about 1.4 GB of RAM). Do this before any
   new run.
2. **Profile and slim the series image-completion path** in
   `continuous_capture.py`. This is the main cause inside the series.
   Time each step: the per-image `np.zeros` uint32 sum, the add, the
   `np.divide` into a new float32 array, the trimming copy, and the queue
   hand-off. For nframes=1, a full-frame copy should take about 10–20 ms on a
   Pi 4, not 180 ms. Likely fixes:
   - reuse preallocated sum buffers
   - pass the uint32 sum plus nframes to the writer and divide there
   - avoid repeated full-frame allocations
   Confirm by re-running `series` and checking that callback p95 is under
   100 ms and timestamp gaps are 0.
3. **Move the legacy save out of the camera callback** (snapshot X/XX and
   save in a worker thread). This matters for back-to-back legacy captures
   and for the `legacy_save` stall of 1–2 s.
4. **Raise `buffer_count`** (e.g. 8–12, if memory allows; each raw buffer is
   about 24 MB) to absorb occasional spikes. This cannot fix a sustained
   per-frame cost above 100 ms.
5. **Fix the diagnostics:**
   - report timestamp gaps as the loss measure instead of
     `sequence.missing_frames`
   - make the runner record the series end state and error (for example
     "writer queue full"), so finding 5 can be confirmed
6. **Re-run on the SD card only** (`--conditions idle legacy series`), or with
   fewer tmpfs images, to avoid memory pressure. Use `--repeats 3` once the
   fixes from step 2 are in.
