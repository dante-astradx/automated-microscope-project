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

## Follow-up status (2026-10-04, after this report)

- Step 1 done: camera service restarted, `/dev/shm/frame_diag_images` deleted.
- Default writer-queue capacity raised from 3 to 15 (`cc.DEFAULT_QUEUE_CAPACITY`).
  The runner now passes `--queue-capacity` (default 15).
- Step 4 done: `camera_zmq.CAMERA_BUFFER_COUNT = 10` (was Picamera2's default
  of 6), with a fallback to 6 if the allocation fails. Not yet verified on hardware.
- Step 5 done:
  - The summary now has top-level `missing_frames`, `gap_count` and
    `loss_measure`, all taken from sensor timestamps. `sequence` is kept only
    as an order check.
  - The runner records each series' end state, image counts, queue high-water
    mark and error.
  - The runner rewrites the index after every condition and stops if a
    session records 0 frames.
- Steps 2 and 3 are on hold pending a planning discussion. Step 6 waits for
  step 2.

## Stage profiling

### Instrumentation (added 2026-10-04, not yet committed)

While a diagnostics session is active, each frame record has a `stages_ms`
field. The summary has `stages_ms_by_mode`, and
`frame-loss-analysis/analyze_stage_profile.py` prints the table. Stages:

- `decode`: frombuffer and reshape.
- `accumulate`: validation, crop, and `np.add` into the uint32 sum.
- `complete`: new float32 mean and `np.divide`.
- `reset`: `sum.fill(0)`.
- `enqueue`: sidecar, lock and queue put.
- `legacy_accumulate` and `legacy_save`.

Any remaining callback time is reported as `unattributed`. Stage timing
runs only during a diagnostics session; otherwise there are no clock reads.

### Offline control: `bench_frame_stages.py` (Pi 4, no camera, synthetic frames)

One run on 2026-10-04: numpy 1.24.2, 20 iterations, ms.

| operation | median | p95 | notes |
|---|---|---|---|
| decode (frombuffer + reshape) | 0.01 | 0.02 | a view, no copy |
| add u16 -> u32 into sum (accumulate) | 59.2 | 69.7 | |
| divide u32 -> float32, preallocated output | 63.8 | 71.9 | |
| fresh `np.empty` + divide (as production does) | 110.0 | 118.4 | about 46 ms of first-touch page faults on a new 47 MiB array |
| `sum.fill(0)` (reset) | 18.1 | 23.6 | |
| **series frame at nframes=1 (add + fresh divide + reset)** | **187.3** | | the live series callbacks measured 155-215 ms |
| TIFF write, float32, /tmp (writer thread) | 76.3 | 78.9 | |
| TIFF write, float32, /dev/shm (writer thread) | 58.7 | 63.2 | |

An earlier run on the same machine gave 46/59/103/16 ms (add, divide,
empty+divide, reset): about 10-20% run-to-run variance.

**Suggested, not yet confirmed live:**

- The arithmetic alone accounts for the observed series callback time.
  `complete` (about 110 ms) is the largest stage. More than 40% of it is
  page-faulting a freshly allocated output array.
- The add (about 59 ms) is close to the legacy 67-87 ms, which fits
  legacy = decode + `iac.update_x_xx`. That still has to be confirmed
  with the live `legacy_accumulate` stage.
- Contention with the writer thread would show up as live stage times above
  these numbers, or as large `unattributed` time.

### Live measurement (2026-10-04 20:46, one repeat, camera at 10 fps)

Command: `run_frame_loss_diagnostics.py --conditions series legacy --repeats 1`.
Settings: 10 camera buffers (`buffer_count` 10 in camera_info), queue capacity 15.
Full table: `stage_profile_20261004_2046.csv`. Times in ms.

**Series: 15 images, nframes=1, SD card.**
- State complete, 15/15 images written.
- 23 frames missing in 3 gaps; queue high-water mark 8 of 15.

| stage | frames | median | p95 | max | share of callback |
|---|---|---|---|---|---|
| decode | 137 | 0.06 | 0.10 | 0.13 | - |
| accumulate | 15 | 56.2 | 95.4 | 134.3 | 32% |
| **complete** | 15 | **103.5** | 114.5 | 119.0 | **58%** |
| reset | 15 | 16.2 | 16.7 | 16.8 | 9% |
| enqueue | 15 | 0.18 | 0.20 | 0.20 | 0.1% |
| unattributed | 137 | 0.04 | 0.14 | 0.21 | - |
| **per-image callback** | 15 | **~176** | | 259 | |

**Legacy: accumulate of 100 frames, SD card.**
- 76 frames missing in 1 gap, all after the save.

| stage | frames | median | p95 | max |
|---|---|---|---|---|
| decode | 100 | 0.06 | 0.07 | 0.10 |
| legacy_accumulate | 100 | 88.0 | 94.2 | 185.2 (first frame) |
| legacy_save | 1 | 8283 | | |
| unattributed | 100 | 0.04 | 0.06 | 0.08 |

### Findings (live data)

1. **The callback time is the image arithmetic; lock waits and contention
   add nothing measurable.**
   - The stages account for all but about 0.04-0.2 ms of every callback.
   - Lock waits, the queue put and the sidecar cost almost nothing
     (enqueue is 0.2 ms).
   - Each series image costs complete about 104 ms, accumulate about 56 ms
     and reset about 16 ms, about 176 ms in total. The frame period is 100 ms.
2. **`complete` is the largest stage**: 58% of an image callback.
   - On its own it takes longer than one frame period.
   - It matches the offline "fresh np.empty + divide" (103-110 ms).
   - The offline control shows that about 46 ms of it is page-faulting a
     newly allocated 47 MiB float32 array. The divide itself is about 64 ms.
3. **`accumulate` is the second largest stage** (about 56 ms median), in line
   with the offline `np.add` (46-59 ms).
   - It is slowest at the start of the series: 134 ms on the first image,
     because of first-touch faults on the new sum array, then about 70 ms.
   - It settles to about 45-55 ms later.
4. **Decode is free** (0.06 ms). `np.frombuffer` and the reshape are views,
   not copies. This corrects the plan's assumption of a 23.5 MiB copy.
5. **Legacy: all of the callback time is `iac.update_x_xx`.**
   - legacy_accumulate takes 88 ms median, against 0.06 ms for decode.
   - That explains the earlier 67-87 ms legacy callbacks. It is close to the
     100 ms budget, and its p95 (94 ms) leaves little margin.
6. **The legacy save took 8.3 s in this run**, compared with 1.1-1.9 s on
   2026-10-04 17:21.
   - The series writer had already finished: image 15 was written at
     20:46:52 and the legacy capture started at 20:46:54.
   - The kernel was most likely still flushing the series' roughly 700 MiB
     of TIFFs to the SD card at that point. That would also explain why the
     series writer's save_time rose from 0.1 s to 13.4 s.
   - *Suggested, not proven:* page-cache writeback is what slowed the save.
   - During the save the camera was starved and 76 frames were dropped.
   - The legacy images themselves were intact (100 contiguous frames).
7. **Ten buffers delay the loss but do not prevent it.**
   - With 10 buffers the series lost its first frames after image 12
     (latency climbed to about 1.2 s first).
   - With 6 buffers in the earlier runs, the first gap came after about 6
     frames.
   - It still lost 23 frames, because every image costs about 176 ms against
     a 100 ms budget. This matches the expectation in step 4.
8. **Writer backlog.** The SD-card TIFF writer fell behind (save_time rose
   from 0.1 s to 13.4 s). With a queue of 15 the series still completed.
   With the old capacity of 3 it would have overloaded at about image 6.

### What this means for the next step (not implemented)

To get a series image under 100 ms, the camera thread would need to stop
re-allocating, dividing and re-zeroing a full-size array per image.
Options to discuss:

- move the divide to the writer thread
- reuse preallocated buffers
- at nframes=1, skip the sum and divide entirely

The legacy path is close to the budget and stalls the camera for 1-8 s per
save. Moving the save off the camera thread is a separate item.

## Post-fix stage profiling (Options B + A, implemented 2026-10-05, not committed)

### What changed

- **`nframes=1`:** one pass converts the cropped frame into a recycled float32
  buffer. There is no running sum, no divide by the group size, and no zeroing.
- **`nframes>1`:** the callback only adds frames into a pooled uint32 sum. The
  mean (the divide) is computed by the writer thread from a `LazyMean`, and the
  sum is zeroed by the pool in the writer thread.
- **Output:** float32 mean TIFF plus JSON sidecar, bit-identical to before
  (checked by tests against the old arithmetic).

### Offline result (measured; Pi 4, no camera)

The real `ContinuousSeries` was fed full-size synthetic frames with a 100 ms
period, with the TIFF and JSON writes stubbed out, so only callback work was
measured. "Steady state" excludes each series' first images, which need fresh
buffers. Times in ms.

| nframes | callback median | callback max | accumulate | complete | enqueue | first callback |
|---|---|---|---|---|---|---|
| before (live 2026-10-04) | ~176 per image | 259 | 56.2 | 103.5 | 0.2 | 259 |
| 1 | **30.7** | 31.0 | 0.0 | 30.4 | 0.19 | 77 |
| 2 | **61.7** | 79.7 | 61.6 | 0.03 | 0.20 | 106 |
| 10 | **51.8** | 68.4 | 51.7 | 0.03 | 0.22 | 107 |

- `reset` is no longer emitted.
- The first callback of a series is slower (77-107 ms) because its buffers are
  allocated fresh. After that, buffers are recycled (11 of 12 acquires reused
  at nframes=1).
- `bench_frame_stages.py` (run alone, 2026-10-05) gave add 48.8, divide 58.4,
  empty+divide 101.7 and reset 16.0 ms, consistent with earlier runs.
- A first attempt at these measurements ran two benchmarks at the same time and
  roughly doubled every number. Those results were discarded.

### Still inferred, not yet measured live

- That the live callback matches the offline numbers while the SD-card writer
  is busy. Memory-bandwidth contention could add time.
- That the series loses no frames (`missing_frames` 0).
- For nframes>1, the writer now also does the divide (~100 ms per image).
  That is fine at 10/nframes images per second, but it adds to the writer's
  existing SD-card backlog.

### Live measurement (2026-10-05 12:24, measured)

Settings: idle, legacy and series, 3 repeats each; series of 10 images at
nframes=1; queue capacity 8; 10 camera buffers. Memory: MemAvailable was
1397 MiB at start and 934 MiB at its lowest. The 400 MiB guard never tripped.
Index: `Images/frame_diagnostics/20261005_122447_index.json`. Times in ms.

| session | missing frames | gaps | notes |
|---|---|---|---|
| idle r1-r3 | 0 / 0 / 0 | 0 | baseline clean |
| **series r1-r3** | **0 / 0 / 0** | **0** | 10/10 images written each time, state complete; queue high-water 2, 6, 6 of 8 |
| legacy r1 | 6 | 1 | all after the save (700 ms gap); the 100 averaged frames were contiguous |
| legacy r2 | 13 | 4 | **2 one-frame gaps inside the averaging window**, 1 just before the save, 10 frames after the save |
| legacy r3 | 15 | 1 | all after the save; the averaged frames were contiguous |

**Series callbacks for frames that complete an image (10 per trial):**

| trial | values | median | max |
|---|---|---|---|
| r1 | 69.6, 82.0, 41.7, 34.3, 29.9, 30.4, 30.5, 31.4, 30.6, 78.5 | ~33 | 82.0 |
| r2 | 90.9, 100.6, 39.5, 103.2, 31.8, 75.7, 97.5, 98.6, 147.9, 48.7 | ~94 | 147.9 |
| r3 | 71.4, 80.3, 39.8, 31.8, 77.0, 40.2, 109.8, 79.0, 78.9, 76.8 | ~77 | 109.8 |

- The other series frames cost about 0.1 ms. `accumulate` is now 0.03 ms;
  `reset` is gone.
- Before the fix, every image took about 176 ms and 23 frames were lost per
  15-image series.

### Findings (live)

1. **No series frames were lost in 3 of 3 trials (30 images).** This is the
   result we wanted.
2. **The callback is under 100 ms most of the time, but not always.**
   - 4 of 30 image callbacks exceeded 100 ms (100.6, 103.2, 147.9 and
     109.8 ms).
   - They were absorbed by the 10 camera buffers. Latency peaked at 232 ms,
     then recovered, and no frame was dropped.
   - The cost is bimodal: about 30 ms (r1 images 4-9, the offline number) or
     about 75-100 ms.
   - *Inferred, not proven:* about 77 ms is the cost of `np.empty` plus
     page-faulting a fresh 47 MiB buffer, measured offline earlier. When the
     writer is behind (queue high-water 6/8), the free mean buffers are all
     still in the queue, so the pool is empty and the callback allocates.
     Values above 100 ms would be that same allocation plus memory-bandwidth
     contention with the writer's TIFF write.
   - Possible follow-up: pre-allocate and pre-fault the pool to
     queue_capacity + 1 buffers at series start, so the callback never
     allocates.
3. **Legacy accumulation is now marginal.**
   - legacy_accumulate (`iac.update_x_xx`) measured a median of 77.5, 93.2
     and 82.1 ms in the three trials, with p95 of 83.8, 106.9 and 85.7 ms.
   - In r2 it was over budget often enough that 2 frames were dropped inside
     the 100-frame averaging window. The image still averages 100 delivered
     frames, but they span 102 frame periods.
   - The code was not changed. The r2 slowdown is *inferred* to be
     background SD-card writeback from the preceding series' TIFFs.
   - The save still stalls the camera for 1.2-2.1 s, and 6-15 frames are lost
     after each save.
4. **Runner reporting glitch.** For series r1 and r3, the client timed out
   waiting for the `stop_frame_diagnostics` reply, so the runner printed
   frames=None. The camera service still wrote both per-frame files
   (`..._series_r1_aab5de9cd1f5_frames.json` and
   `..._series_r3_f92163870d86_frames.json`), and the numbers above come from
   those files. The fix is to have the runner fall back to the file by label;
   this is cosmetic.
