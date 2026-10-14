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

### Buffer pre-allocation (implemented 2026-10-05, not committed)

#### What changed (`continuous_capture.py` only)

- **`BufferPool(target=8)`:** after the first `acquire()` gives it the shape,
  a daemon thread keeps 8 ready buffers free. It adds them one at a time with
  `top_up()`, and each `acquire()` wakes it to replace the buffer just taken.
  It never holds the pool lock while allocating. `acquire()` still never
  blocks and still falls back to a fresh allocation when nothing is free.
- **Pre-faulting:** each new buffer is `np.zeros` plus one written zero per
  4 KiB page, in 1 MiB chunks. This faults in every page off the camera
  thread (about 47 ms per 47 MiB buffer, compared with about 56 ms to write
  every element).
- **Pacing:** `add_frame` marks both pools active while the frame is
  converted or added. The worker pauses between chunks during that time,
  unless the pool is empty.
  - *Measured offline:* faulting at the same time as the conversion slowed
    the conversion from about 30 ms to about 45 ms (memory bandwidth). A
    first version without pacing measured about 47 ms per image.
- **Pool size:** the series pools also keep up to 8 recycled buffers
  (`max_free=8`), so a recycled buffer is never dropped and then
  re-allocated. With `max_free=4` the worker re-allocated 31 buffers in a
  30-image run.
- **Memory check:** on the first frame of a series, the series fails with
  `INSUFFICIENT_MEMORY` if MemAvailable < 8 x rows x width x 4 bytes +
  150 MiB. At full resolution that is about 526 MiB.
- **Status:** new fields `pool_target_means`, `pool_target_sums` and
  `pool_filling`. All existing fields are unchanged.
- **Output:** TIFFs and sidecars are unchanged (tests check them byte for
  byte).

#### Offline result (measured 2026-10-05; Pi 4, no camera)

Setup: the real `ContinuousSeries`, 10 images at nframes=1, full-size
3040x4064 uint16 frames every 100 ms, queue capacity 8, callbacks timed with
`StageTimer`. `_write_json` was stubbed out. `_write_tiff` was stubbed to
sleep 0.35 s, so the writer falls behind the way it did live (queue
high-water 7 of 8). Times in ms.

| run | per-image callback | median | max | fresh allocations |
|---|---|---|---|---|
| no pre-fill (target=0, old behaviour) | 73.3, 71.6, 71.2, 70.5, 70.9, 30.4, 71.2, 70.8, 30.5, 70.5 | 70.9 | 73.3 | 8 |
| pre-fill, trial 1 | 87.7, 42.8, 31.9, 31.7, 30.0, 30.3, 30.1, 32.8, 30.4, 30.8 | 31.7 | 87.7 | 1 |
| pre-fill, trial 2 | 95.6, 31.1, 36.2, 29.9, 30.1, 29.9, 30.3, 29.6, 31.3, 29.5 | 30.3 | 95.6 | 1 |
| pre-fill, trial 3 | 89.3, 44.6, 30.9, 30.5, 30.3, 31.2, 30.6, 31.0, 30.7, 30.7 | 30.9 | 89.3 | 1 |
| pre-fill, fast writer (no sleep) | 88.4, 30.1, 30.2, 48.6, 30.0, 29.8, 29.6, 29.9, 29.4, 29.7 | 30.0 | 88.4 | 1 |
| pre-fill, nframes=2 (time of the image-completing frame) | 41.9, 55.0, 44.3, 52.3, 47.4, 45.0, 61.2, 42.7, 50.3, 62.8 | 50.3 | 62.8 | 1 |

- **With a backlogged writer, images 2-10 are now about 30 ms**, compared
  with about 71 ms before. The cost is no longer bimodal.
- **Image 1 (88-96 ms) is still a fresh allocation.** That is the only
  fresh allocation (`pool_fresh_allocations` = 1): no pool can know the
  shape before the first frame.
  - It is higher than the old first image (73 ms) because the worker
    starts faulting the second buffer during that callback.
  - It is still under 100 ms in every offline run.
- **A few images 2-3 took 36-49 ms,** while the worker was still building
  its first ready buffers next to the callback.
- No offline image callback exceeded 100 ms (0 of 60).

#### Still inferred, not yet measured live

- **The live numbers have not been measured yet.** The camera service has
  not been restarted with this code, and the live diagnostics
  (`run_frame_loss_diagnostics.py` + `analyze_stage_profile.py`) have not
  been run. Results will be added here, compared with the 2026-10-05
  12:24 run (4 of 30 image callbacks over 100 ms; 0 missing frames).
- That the live callback stays near 30 ms while the real SD-card writer
  competes for memory bandwidth.
- That image 1, at about 90 ms offline, stays under 100 ms live. Last
  time's > 100 ms values were the allocation plus writer contention. Image 1
  has no writer contention, because nothing has been queued yet.
- Memory (arithmetic, not measured):
  - A series uses one pool (means at nframes=1, sums at nframes>1). That
    pool holds at most 8 free buffers, ready and recycled together, so a
    backlog costs up to about 8 x 47 MiB = 376 MiB more than before. Before,
    the free list was empty under backlog.
  - Worst case at queue capacity 8: about 18 buffers (8 free, 8 queued, 1
    being written, 1 in the callback), about 850 MiB.
  - At the default capacity of 15: about 25 buffers, about 1.15 GiB.
  - On 2026-10-05, MemAvailable went from 1397 to 934 MiB. The extra
    376 MiB would bring the low point to about 560 MiB. The live run should
    confirm it stays above the runner's 400 MiB guard.


#### Live attempt (2026-10-05 14:09, INCOMPLETE: camera stalled)

Settings: the same as the 12:24 run (idle, legacy and series, 3 repeats;
series of 10 images at nframes=1; queue capacity 8; 10 camera buffers),
except `--min-start-mib 1300`. MemAvailable was about 1350-1420 MiB with the
VS Code server using about 1.1 GB, so the default 1500 was not reachable.

- Service restarted at 14:00:28 (PID 23911/23913), after the code change
  (13:53).
- Index: `Images/frame_diagnostics/20261005_140934_index.json`.
- Stage CSV: `stage_profile_20261005_1409.csv`.

Only 3 of 9 sessions completed: idle r1, legacy r1 and series r1.

| session | missing frames | gaps | notes |
|---|---|---|---|
| idle r1 | 0 | 0 | baseline clean |
| legacy r1 | 4 | 1 | legacy_accumulate median 70.0, p95 82.4, max 179.5; save 1126 ms; lowest MemAvailable 1020 MiB |
| **series r1** | **0** | **0** | 10/10 written, state complete; queue high-water 6/8; lowest MemAvailable 627 MiB |
| idle r2 | - | - | **0 frames delivered: camera stalled**; the runner stopped |

**Series r1, callbacks for frames that complete an image (measured, ms):**

| run | values | median | max | over 100 ms |
|---|---|---|---|---|
| 2026-10-05 12:24 r1 (before) | 69.6, 82.0, 41.7, 34.3, 29.9, 30.4, 30.5, 31.4, 30.6, 78.5 | ~33 | 82.0 | 0/10 |
| 2026-10-05 12:24 r2 (before) | 90.9, 100.6, 39.5, 103.2, 31.8, 75.7, 97.5, 98.6, 147.9, 48.7 | ~94 | 147.9 | 2/10 |
| 2026-10-05 12:24 r3 (before) | 71.4, 80.3, 39.8, 31.8, 77.0, 40.2, 109.8, 79.0, 78.9, 76.8 | ~77 | 109.8 | 1/10 |
| **2026-10-05 14:09 r1 (pre-fill)** | 84.3, 46.5, 31.0, 43.1, 30.8, 30.7, 40.0, 43.9, 48.2, 33.1 | 41.5 | 84.3 | **0/10** |

- **Stages:** all of the time is in `complete` (the float32 conversion). The
  rest of the callback is under 1 ms.
- **Latency:** 129-197 ms throughout. No growth, and no frame was dropped.
- **Image 1 (84.3 ms):** the expected fresh allocation, under 100 ms.
- **Images 2-10:** 30.7-48.2 ms. That is under the old 70-150 ms, but
  higher than the 30 ms measured offline. *Inferred:* the excess is memory
  bandwidth shared with the real SD-card writer and the fill worker (the
  queue reached 6/8, so the worker was filling at the same time).
- **One trial only.** It does not yet show "no callback over 100 ms" across
  repeats; the stall stopped trials r2 and r3.

**Camera stall after series r1 (measured facts):**

- **Frames stopped right after the 10th series image.** The series r1
  session lasted 20.4 s but recorded only 11 frames (1 idle + 10 series),
  spanning 1.0 s of sensor time.
  - In the 12:24 run, series sessions kept recording idle frames after the
    series (71-131 frames per session).
  - So delivery stopped while the writer was still draining the queue
    (high-water 6/8), when memory was lowest. That is not proven to be the
    cause.
- At 14:10:04 libcamera logged:
  `Dequeue timer of 1000000.00us has expired!` and
  `Camera frontend has timed out! ... check that your camera sensor connector
  is attached securely`.
- No frames were delivered after that: idle r2 recorded 0 frames.
- The service is still `active (running)`, NRestarts=0, but it no longer
  answers ZeroMQ requests. A later `start_frame_diagnostics` timed out with
  "controller is busy".
- This is the first frontend timeout this boot (the journal starts at
  10:48). It did not happen in the 12:24 run.

**Cause: not established.**

- The libcamera message points at the sensor and CSI link. Every series
  callback returned within 85 ms, and latency did not grow, so a slow
  callback did not cause it.
- The timing is suspicious, though. Delivery stopped exactly when the series
  stopped consuming frames, with the writer draining and MemAvailable at its
  lowest (627 MiB, compared with 934 MiB in the 12:24 run).
- An interaction with this change (memory pressure, or the background fill
  thread) is **not ruled out**.
- A rerun after a service restart, ideally repeated, is needed before
  drawing a conclusion.


#### Live series rerun (2026-10-05 14:28, 5 trials, measured): goal NOT met

Settings: series only, 5 repeats, 10 images at nframes=1, queue capacity 8,
10 camera buffers, `--min-start-mib 1300`.

- Service restarted at 14:27:00. MemAvailable was 1435 MiB at start.
- Index: `Images/frame_diagnostics/20261005_142815_index.json`.
- Stage CSV: `stage_profile_20261005_1428.csv`.
- **No camera stall this time**: there was no frontend timeout in the
  journal, and frames kept arriving after every series.

| trial | missing frames | gaps | state | queue high-water | lowest MemAvailable |
|---|---|---|---|---|---|
| r1 | **78** | 6 | complete 10/10 | 5/8 | 962 MiB |
| r2 | 0 | 0 | complete 10/10 | 8/8 | 888 MiB |
| r3 | **25** | 2 | **overloaded** 9/10 (image 10 not queued) | 8/8 | 964 MiB |
| r4 | **16** | 1 | complete 10/10 | 7/8 | 973 MiB |
| r5 | 0 | 0 | complete 10/10 | 8/8 | 920 MiB |

For comparison, the 12:24 run (before this change) had 0 / 0 / 0 missing
frames and 3/3 complete.

**Image callbacks (ms):**

| trial | values | over 100 ms |
|---|---|---|
| r1 | 108.6, 56.7, 57.6, 56.8, 65.1, 46.9, 115.2, **726.5**, **787.6**, 89.8 | 3 |
| r2 | **123.1**, 59.8, 37.9, 31.9, 33.7, 31.5, 31.8, 32.1, 33.4, 31.7 | 1 |
| r3 | 93.8, 32.4, 31.3, 30.7, 30.9, 30.9, 30.8, 30.2, 31.2, 36.2 | 0 |
| r4 | 87.2, 42.0, 48.0, 31.3, 31.6, 36.2, 32.8, 30.7, 30.8, 30.4 | 0 |
| r5 | **124.9**, 52.2, 41.5, 31.4, 31.7, 32.3, 32.0, 31.6, 32.9, 32.3 | 1 |

That is 5 of 50 over 100 ms. Including the 14:09 trial, it is 5 of 60.

**What the data show (measured):**

1. **Steady-state callbacks did improve.**
   - In r2-r5, images 4-10 were 30-36 ms in every trial. In the 12:24 run
     they were often 75-110 ms.
   - This is the effect the change was meant to have.
2. **Image 1 got slower:** 87-125 ms, compared with 69-91 ms at 12:24.
   - It is still a fresh allocation. The worker starts faulting the next
     buffer during that callback because the pool is empty.
   - 3 of 6 live first images were over 100 ms. Offline, all were under.
3. **Most lost frames were not caused by a slow callback.**
   - The r3 and r4 gaps (1.3-1.7 s), and most of the r1 gaps, came after
     image 10. By then the series callbacks cost about 0.1 ms each.
   - In those trials the writer was 4-12 s behind, by sidecar `save_time`.
   - The camera simply stopped delivering for 1.3-2 s at a time.
4. **r1 had 726 and 788 ms callbacks**, all in `complete`, which is the
   float32 conversion into an already-faulted buffer. Latency rose to 5.2 s.
5. **Memory was fully exhausted.**
   - After the run, swap was 510 of 511 MiB used.
   - `/proc/vmstat` (cumulative since boot) shows `pswpout` 130489 and
     direct-reclaim `allocstall_normal` 508.
   - MemAvailable stayed at 888-973 MiB, so the runner's guard never
     tripped. That is because it counts reclaimable page cache.
6. **The overload in r3 is new.** The queue filled (8/8) before image 10
   could be queued.

**Inferred, not proven:**

- **Gaps and spikes:** both are memory pressure. The pool's 8 ready buffers
  (about 376 MiB at full resolution) plus the queued images force the
  kernel to swap and reclaim page cache while the SD-card writer is
  flushing. That would stall the camera thread (the 726-788 ms conversions
  in r1) and the camera and driver pipeline (the post-series gaps).
- **Overload:** a writer slowed by the same memory pressure is the likely
  cause of r3's queue overload.
- **Why 12:24 was clean:** without the ready buffers, peak memory was
  about 376 MiB lower.
- **Earlier stall:** the 14:09 camera stall may have the same cause.

**Conclusion: the pre-allocation, at target=8, makes the live series worse
on this Pi.** Steady-state callback cost is lower, but frame loss went from
0 in 3 trials to 119 frames across 3 of 5 trials.

Options, not implemented:

- (a) Lower the target to 1-2. This keeps most of the steady-state gain and
  limits the memory cost to about 47-94 MiB.
- (b) Size the target from MemAvailable at series start.
- (c) Revert to the committed behaviour.
- (d) Test target=8 again with swap usage at 0 before the run. Swap was
  already nearly full from the earlier session, which this run did not
  check.


#### Live series rerun with target=2 (2026-10-05 15:00, 5 trials, measured)

Settings: the same as the 14:28 run, but `DEFAULT_POOL_TARGET = 2`. The
series pools keep up to `max(4, target)` = 4 recycled buffers.

- Service restarted at 14:59:00, after the edit at 14:50. The camera came up
  with no errors.
- At start: swap 483 of 511 MiB used (left from earlier sessions, not
  cleared); MemAvailable about 1690 MiB.
- Index: `Images/frame_diagnostics/20261005_150010_index.json`.
- Stage CSV: `stage_profile_20261005_1500.csv`.
- No camera stall and no frontend timeout in the journal.

| trial | missing frames | gaps | state | queue high-water | lowest MemAvailable |
|---|---|---|---|---|---|
| r1 | **25** | 1 | complete 10/10 | 5/8 | 1217 MiB |
| r2 | **1** | 1 | complete 10/10 | 7/8 | 1170 MiB |
| r3 | **7** | 1 | complete 10/10 | 7/8 | 1172 MiB |
| r4 | 0 | 0 | complete 10/10 | 7/8 | 1187 MiB |
| r5 | **22** | 2 | complete 10/10 | 6/8 | 1194 MiB |

**Image callbacks (ms):**

| trial | values | over 100 ms |
|---|---|---|
| r1 | **123.4**, 59.4, 58.6, 57.2, 61.0, 43.7, 43.2, 43.8, 50.0, 50.8 | 1 |
| r2 | **117.8**, 75.9, 30.3, 44.8, 48.7, 33.1, 37.0, 31.9, 32.1, 31.3 | 1 |
| r3 | 90.4, 36.6, 31.3, 30.0, 32.6, 29.8, 30.0, 40.9, 34.7, 30.5 | 0 |
| r4 | 88.3, 49.9, 35.5, 42.2, 30.6, 30.7, 30.6, 30.6, 29.6, 30.2 | 0 |
| r5 | 91.9, 55.5, 54.5, 74.4, 29.8, 29.9, 29.8, 44.4, 32.2, 29.8 | 0 |

**Comparison of the three live runs:**

| run | trials with lost frames | missing frames total | complete | image callbacks over 100 ms | worst callback |
|---|---|---|---|---|---|
| 12:24, before (no pre-fill) | 0 of 3 | 0 | 3/3 | 4 of 30 | 147.9 ms |
| 14:28, target=8 | 3 of 5 | 119 | 4/5 (1 overloaded) | 5 of 50 | 787.6 ms |
| **15:00, target=2** | **4 of 5** | **55** | **5/5** | **2 of 50** | **123.4 ms** |

**What the data show (measured):**

1. **Callbacks improved compared with both earlier runs.**
   - Only 2 of 50 image callbacks were over 100 ms, both first images
     (fresh allocation): 123.4 and 117.8 ms.
   - Images 2-10 were all under 76 ms, and mostly 30-50 ms.
   - Latency peaked at 313 ms. There were no 700 ms spikes, and no overload.
2. **Memory pressure is much lower than with target=8.**
   - The lowest MemAvailable was 1170 MiB, compared with 888-973 MiB at
     target=8.
   - No new swap-out during the run (`pswpout` stayed at 130489).
   - `allocstall_normal` rose 520 -> 588, so there was some direct reclaim.
3. **Frames are still lost, and none of the gaps line up with a slow
   callback.**
   - All 6 gaps (200 ms to 2.6 s) came after image 10, 5.4-8 s into the
     session. By then the series callbacks cost about 0.1 ms each, and
     latency before each gap was normal (129-330 ms).
   - At that time the writer was still saving: sidecar `save_time` reached
     7-10 s.
   - The camera stops delivering frames for a while during the writer's
     drain.
4. **Session length was comparable.** The 12:24 run's series sessions also
   ran 7-13 s with the writer behind (`save_time` up to 9.6 s), and recorded
   no gaps.

**Inferred, not proven:**

- The post-series gaps come from the SD-card writer's drain. The likely
  mechanism is writeback or I/O stalls on the Pi, and some direct reclaim.
  They are not from callback cost.
- Whether the pre-fill itself makes those gaps more likely is **not
  established**. Today's system state also differs from 12:24:
  - swap is nearly full (it was not checked at 12:24);
  - the VS Code server uses about 1.1 GB;
  - more images have been written to the card today.
- A same-conditions A/B test is needed: target=0 (the committed behaviour),
  5 trials, with the same service, swap and memory state. Without it, these
  results cannot be credited to, or blamed on, the pre-fill.

**Status against the goal:** not met. The goal was 0 missing frames in every
trial and no image callback over 100 ms. The callback part is close (2 of 50,
both image 1). The frame-loss part regressed relative to the 12:24 baseline,
cause unattributed.


#### Live A/B: pre-fill disabled, target=0 (2026-10-05 15:32, 5 trials, measured)

Step 1 of the revert plan: `DEFAULT_POOL_TARGET = 0`, so no worker thread is
started. The callback path is the committed one (pool if a buffer is free,
otherwise a fresh allocation; `max_free` 4). The start-of-series memory check
and the `pool_*` status fields stay; with target 0 they are inert.

- Service restarted at 15:31:23, after the edit at 15:26. The camera came up
  with no errors.
- At start: swap 457 of 511 MiB used; MemAvailable about 1640 MiB.
- Index: `Images/frame_diagnostics/20261005_153234_index.json`.
- Stage CSV: `stage_profile_20261005_1532.csv`.
- No camera stall and no frontend timeout.

| trial | missing frames | gaps | state | queue high-water | lowest MemAvailable |
|---|---|---|---|---|---|
| r1 | 0 | 0 | complete 10/10 | 4/8 | 1359 MiB |
| r2 | **13** | 1 | complete 10/10 | 7/8 | 1208 MiB |
| r3 | 0 | 0 | complete 10/10 | 7/8 | 1200 MiB |
| r4 | 0 | 0 | complete 10/10 | 7/8 | 1200 MiB |
| r5 | 0 | 0 | complete 10/10 | 6/8 | 1238 MiB |

**Image callbacks (ms):**

| trial | values | over 100 ms |
|---|---|---|
| r1 | 86.8, **101.3**, 58.0, 54.3, 51.7, 47.2, 41.7, 81.5, 87.5, 84.9 | 1 |
| r2 | **101.1**, **107.8**, 44.4, 82.7, 30.0, 71.5, 72.1, 74.0, 74.4, 79.2 | 2 |
| r3 | 75.1, 82.7, 41.9, 32.2, 78.4, **109.9**, 78.0, 76.4, 75.0, 75.3 | 1 |
| r4 | 75.3, 86.2, 31.3, 72.2, 73.8, 74.1, 93.9, 30.5, 75.0, 73.1 | 0 |
| r5 | **113.6**, 93.9, 41.7, **100.7**, 30.7, 73.2, 30.0, 75.9, 76.6, 76.7 | 2 |

**All four live series runs:**

| run | pre-fill | trials losing frames | missing frames | complete | callbacks over 100 ms | worst callback |
|---|---|---|---|---|---|---|
| 12:24 | none (committed code) | 0 of 3 | 0 | 3/3 | 4 of 30 | 147.9 ms |
| 14:28 | target=8 | 3 of 5 | 119 | 4/5 | 5 of 50 | 787.6 ms |
| 15:00 | target=2 | 4 of 5 | 55 | 5/5 | 2 of 50 | 123.4 ms |
| **15:32** | **none (target=0)** | **1 of 5** | **13** | **5/5** | **6 of 50** | **113.6 ms** |

**What the data show (measured):**

1. **Frames were lost even without pre-fill.**
   - r2 lost 13 frames in one 1.4 s gap, 4.1 s into the session.
   - That was after image 10, while the writer was draining (sidecar
     `save_time` up to 10.1 s).
   - It is the same kind of gap seen at target=2 and target=8.
   - So the post-series gaps are **not caused only by the pre-fill**. They
     also happen with the committed callback behaviour in today's system
     state.
2. **The callback cost matches the committed code.**
   - Images 2-10 were bimodal again: about 30 ms (recycled buffer) or about
     70-110 ms (fresh allocation).
   - 6 of 50 were over 100 ms, the most of any run. That is what the
     pre-fill was meant to fix.
3. **Fewer gaps than with pre-fill on.**
   - Frames lost: 1 trial of 5 (13 frames), compared with 4 of 5 (55 frames)
     at target=2 and 3 of 5 (119 frames) at target=8.
   - Five trials is too few to tell 1 of 5 from 4 of 5 with confidence.
     *Inferred:* the pre-fill made the drain-phase gaps more likely, through
     more memory traffic and less free memory, but it is not their only
     cause.
4. **Memory:** the lowest MemAvailable was 1200-1359 MiB, and there was no
   new swap-out during the run.

**Revised conclusions:**

- The baseline the task started from, "0 dropped frames" (12:24, 3 trials),
  does not reproduce today. With the same callback code, 1 of 5 trials lost
  frames.
- The drain-phase gaps are a separate problem from callback cost. They
  happen when every callback is cheap (about 0.1 ms) and the writer is
  saving 7-10 s of backlog to the SD card.
- *Inferred:* the cause is SD-card writeback and I/O, or reclaim, stalling
  the camera pipeline. The swap that had built up all day is a likely
  contributor, since swap is on the same SD card (`/var/swap`).
- Measuring that properly needs a clean baseline: reboot, empty swap, no
  other load. Then run target=0 and target=2 back to back, 5 or more trials
  each.


### Live A/B after reboot (target=0 / target=2 / target=0)

Plan: `POST-REBOOT-AB-TEST.md`. Every block uses series only, 10 images at
nframes=1, queue capacity 8, 10 camera buffers, and the runner's default
`--min-start-mib` (1500).

**Pre-checks (measured, 15:50, after the reboot):**

| item | value |
|---|---|
| uptime | 2 min |
| swap used | **0 of 511 MiB** |
| MemAvailable | 1736 MiB |
| pswpin / pswpout / allocstall_normal | 0 / 0 / 0 |
| service | active since 15:47:56, camera configured with no errors |
| `DEFAULT_POOL_TARGET` | 0 |
| tests | 315 passed |

#### Block A1: target=0 (pre-fill off), 15:51 (measured)

- Index files: `Images/frame_diagnostics/20261005_155140_index.json` (r1-r4)
  and `20261005_155851_index.json` (r5).
- Stage CSV: `stage_profile_20261005_1551_A1.csv`.

| trial | missing frames | gaps | state | queue high-water | lowest MemAvailable |
|---|---|---|---|---|---|
| r1 | 0 | 0 | complete 10/10 | 1/8 | 1673 MiB |
| r2 | **12** | 1 | complete 10/10 | 7/8 | 1325 MiB |
| r3 | **29** | 2 | **overloaded** 9/10 | 8/8 | 1264 MiB |
| r4 | **22** | 1 | complete 10/10 | 6/8 | 1430 MiB |
| r5 | 0 | 0 | complete 10/10 | 4/8 | 1553 MiB |

**Image callbacks (ms):**

| trial | values | over 100 ms |
|---|---|---|
| r1 | 82.7, 94.0, 57.8, 52.2, 52.6, 90.8, 56.2, 53.1, 47.6, 45.4 | 0 |
| r2 | 84.2, 89.1, 88.1, 80.8, 31.8, 86.9, 87.0, 79.4, 88.4, 75.0 | 0 |
| r3 | 72.1, 73.9, 75.5, 76.1, 83.7, 91.7, 72.6, 72.5, 72.5, 75.5 | 0 |
| r4 | 71.6, 80.6, 40.3, 37.4, 29.4, 73.0, 74.6, 84.0, 79.5, 80.8 | 0 |
| r5 | 71.7, 79.6, 41.4, 33.0, 29.1, 29.7, 30.0, 72.3, **206.9**, 83.0 | 1 |

**Gaps.** All 5 gaps (1.0-2.3 s) came after image 10, 8.3-11.8 s into the
session.

- Latency before each gap was normal (about 330 ms), and the series
  callbacks cost about 0.1 ms at the time.
- The writer was still draining: sidecar `save_time` reached 9.8 s (r2),
  14.1 s (r3) and 8.0 s (r4).
- r1 (writer never behind: `save_time` at most 0.8 s) and r5 (at most
  3.7 s) had no gaps.

**Swap and vmstat across the block:** swap went from 0 to 11 MiB;
`pswpout` 0 -> 1877; `allocstall_normal` 0 -> 19. MemAvailable never fell
below 1264 MiB.

**Runner incident (measured):**

- After series r4, the runner's `stop_frame_diagnostics` request timed out
  ("controller is busy"). So the runner printed `frames=None` for r4, and
  the r5 session could not start.
- The camera service did not crash. It answered `status` in about 0.1 s
  afterwards, and kept delivering frames.
- The r4 per-frame file was written anyway; the r4 numbers above come from
  it.
- I stopped the dangling r5 session by hand (it had recorded 3163 frames
  with 0 missing). I then ran r5 as a separate 1-repeat run (index
  `20261005_155851`).
- *Inferred:* the stop request was slow because the service was writing the
  r4 per-frame JSON while the SD card was busy draining TIFFs. This matches
  the 2026-10-05 "runner reporting glitch".

**What A1 shows (measured):** on a freshly booted system with empty swap and
the committed callback behaviour (no pre-fill):

- 3 of 5 trials lost frames (63 frames in total).
- 1 trial overloaded.

Every gap was in the writer's drain, after the series had ended. So the
post-series gaps are not caused by the pre-fill, and not by swap that built
up during the day. They are present with clean swap, whenever the writer is
several seconds behind.

*Inferred, not proven:* the camera pipeline stalls while the kernel writes
back several hundred MiB of TIFF data to the SD card. The trials without
gaps are those where the writer kept up (r1 high-water 1/8; r5 4/8).

Under the plan's decision rules, this is the case "the A blocks lose frames
after the series even with clean swap": an SD-card writeback/I/O problem,
independent of the buffer change.


#### Block A0: the last commit (37d779d), 10 trials, 16:19 (measured)

Run on user request, before block B. The goal was to test the literal last
commit rather than target=0, which still differed from it in three ways:
`set_active()` calls in the callback, a `/proc/meminfo` read on the first
frame, and up to 5 recycled buffers instead of 4.

- **Code:** today's changes to continuous_capture.py and
  test_continuous_capture.py were stashed with `git stash`.
  - No diff against HEAD for continuous_capture.py, camera_zmq.py or
    frame_diagnostics.py.
  - 288 tests passed.
- **Service:** restarted at 16:19:21, after the stash rewrote the file at
  16:16:07. The camera came up with no errors.
- **Same boot as block A1:** swap was 10 MiB at start and stayed at 10 MiB.
- **Settings:** series only, 10 repeats, 10 images at nframes=1, queue
  capacity 8, default `--min-start-mib` 1500.
- Index: `Images/frame_diagnostics/20261005_161947_index.json`.
- Stage CSV: `stage_profile_20261005_1619_A0.csv`.

| trial | missing frames | gaps | state | queue high-water | writer max `save_time` | callbacks > 100 ms |
|---|---|---|---|---|---|---|
| r1 | **4** | 1 | complete | 4/8 | 6.8 s | 1 |
| r2 | 0 | 0 | complete | 7/8 | 10.0 s | 1 |
| r3 | 0 | 0 | complete | 7/8 | 10.0 s | 2 |
| r4 | 0 | 0 | complete | 7/8 | 9.3 s | 1 |
| r5 | 0 | 0 | complete | 7/8 | 9.8 s | 3 |
| r6 | 0 | 0 | complete | 7/8 | 9.6 s | 1 |
| r7 | **1** | 1 | complete | 7/8 | 7.2 s | 2 (incl. **699.1 ms**) |
| r8 | **21** | 1 | complete | 7/8 | 9.9 s | 1 |
| r9 | **33** | 2 | complete | 7/8 | 10.4 s | 1 |
| r10 | 0 | 0 | complete | 7/8 | 9.4 s | 0 |

**Totals:** 4 of 10 trials lost frames (59 frames); 10/10 complete; 13 of 100
image callbacks were over 100 ms.

**Notes on the data:**

- **r6:** the runner's stop request timed out again ("controller is busy"),
  so its summary line says `frames=None`. The per-frame file was written,
  and the r6 numbers above come from it.
- **r7:** the series started 1.1 s into the session, so its images are frames
  #11-20.
  - Image 10 took 699 ms, all in `complete` (the conversion into a buffer).
  - Latency rose to 796 ms and recovered over 8 frames.
  - The 1-frame gap at 3.0 s follows that.
  - This is the same kind of stall seen at target=8 (726/788 ms), here on
    the committed code.

**Where the gaps fall (measured):**

- 5 of the 6 gaps came after the series:
  - r1: 0.5 s, at 8.2 s;
  - r8: 2.2 s, at 6.0 s;
  - r9: 2.4 s at 8.4 s, and 1.1 s at 11.6 s.
- r7's gap is the 6th; it follows the 699 ms callback.
- Each callback was about 0.1 ms at those times, with the writer still
  draining.
- This is the same pattern as block A1 and every afternoon run.

**Writer speed:** in every trial the writer fell 6.8-10.4 s behind. That
includes the trials with no gaps. In this block, a slow writer did not
always cause a gap.

**What block A0 shows (measured):**

1. **The last commit loses frames on this system now.** 4 of 10 trials;
   1 of 5 in the first five, 3 of 5 in the last five.
   - So today's uncommitted changes are **not** what makes the system lose
     frames.
   - The 12:24 run's 0 of 3 is not reproducible: it was 3 lucky trials, or
     the system has changed since then.
2. **The callbacks behave as before.** They alternate between about 30 ms
   and 70-125 ms, and 13 of 100 are over 100 ms. That is the "no margin"
   state the task started from.
3. **No new swapping:** swap stayed at 10 MiB. `allocstall_normal` rose
   19 -> 251, so there was some direct reclaim during the block.

**Comparison with block A1** (same boot, uncommitted code at target=0):

- A1: 3 of 5 trials lost frames (63 frames).
- A0: 4 of 10 trials lost frames (59 frames).

These are the same within the noise of samples this small.

#### Kernel writeback tweaks: swappiness=1, dirty ratios lowered (21:15, measured)

Hypothesis going in: the gaps are kernel writeback stalls. The SD-card
writer dirties hundreds of MiB of page cache; with `dirty_ratio=20` on 4 GB
RAM the kernel lets that build to ~800 MiB and then flushes in one burst,
which can block the camera path. Swap on the same SD card adds more writes
when memory is tight.

**Settings changed (runtime only, no reboot, no code change):**

| setting | before | after |
|---|---|---|
| `vm.swappiness` | 60 | 1 |
| `vm.dirty_ratio` | 20 (≈800 MiB) | 4 (≈150 MiB) |
| `vm.dirty_background_ratio` | 10 (≈400 MiB) | 2 (≈75 MiB) |

Restore with `sudo sysctl vm.swappiness=60 vm.dirty_ratio=20
vm.dirty_background_ratio=10`.

**Before the block:** swap 351/511 MiB used, MemAvailable 2057 MiB,
`pswpout` 178240, `allocstall_normal` 1015.

**Setup:** series only, 5 repeats, 10 images at nframes=1, queue capacity 8,
`DEFAULT_POOL_TARGET` 0 (pre-fill off, same code as A0/A1), default
`--min-start-mib` 1500.
- Index: `Images/frame_diagnostics/20261005_211523_index.json`.
- Stage CSV: `stage_profile_20261005_2115_tweak.csv`.

| trial | missing frames | gaps | state | queue high-water | writer max `save_time` | callbacks > 100 ms |
|---|---|---|---|---|---|---|
| r1 | **0** | **0** | complete | 8/8 | 11.0 s | 1 (106.0) |
| r2 | **0** | **0** | complete | 8/8 | 12.4 s | 1 (119.3) |
| r3 | **0** | **0** | **overloaded** 9/10 | 8/8 | 11.4 s | 0 |
| r4 | **0** | **0** | complete | 8/8 | 12.0 s | 0 |
| r5 | **0** | **0** | complete | 8/8 | 12.0 s | 0 |

**Totals: 0 missing frames in 5 trials.** No gap anywhere in any session.
4 of 5 complete; r3 overloaded because the writer queue filled before image
10 (a different failure mode: no frame is lost, but the last image is not
saved).

**What changed (measured):**

1. **Frame delivery is clean.** The same code, same capture settings and
   same boot lost 63 frames in 3 of 5 trials earlier today (block A1) and
   59 in 4 of 10 (block A0). With the writeback tweaks: zero.
2. **The writer is still slow.** `save_time` reached 11-12.4 s, the same as
   in the runs that lost frames. So writer backlog alone did not cause the
   gaps; how the kernel flushes it does.
3. **No new paging pressure.** `pswpout` 178240 -> 179366 during the block
   (1126 pages, about 4 MiB) and `allocstall_normal` 1015 -> 1090. Earlier
   blocks moved these far more.
4. **Callbacks are unchanged:** 70-99 ms per image, 2 of 50 over 100 ms.
   The tweaks do not make the callback faster; they stop the system from
   stalling around it.

**Inferred, not proven:**

- The gaps were kernel writeback stalls, and flushing earlier and in smaller
  amounts removes them. This fits the earlier correlation (gaps only when
  the writer had a large backlog) and the Picamera2 reports of drops with
  system load.
- r3's overload is the remaining issue: the writer keeps up well enough to
  avoid stalls now, but not well enough to keep 10 images queued at 8/8
  capacity. Queue capacity or writer speed needs attention separately.
- 5 trials is a small sample. A longer run (and one with a larger series)
  would confirm.

**New questions:**

- Does the effect survive a reboot? These settings revert on restart unless
  added to `/etc/sysctl.conf`.
- Is `swappiness=1` doing the work, or the dirty ratios, or both? A run with
  only one of the two changed would separate them.



#### Confirmation: 15 images, queue capacity 15 (21:39, measured)

The user's stated success criteria: 15 images in the series, queue capacity
15, no dropped frames, and the recorded frames within 100 ms of one another.

**Settings:** `swappiness=1`, `dirty_ratio=4`, `dirty_background_ratio=2`
(as the previous block); `DEFAULT_POOL_TARGET` 0; `--series-images 15
--queue-capacity 15`, 5 repeats, `--min-start-mib` 1500.
- Index: `Images/frame_diagnostics/20261005_213952_index.json`.

| trial | images | missing frames | gaps | state | queue high-water | callbacks > 100 ms |
|---|---|---|---|---|---|---|
| r1 | 15/15 | **0** | **0** | complete | 12/15 | 0 |
| r2 | 15/15 | **0** | **0** | complete | 13/15 | 1 (124.9) |
| r3 | 15/15 | **0** | **0** | complete | 13/15 | 2 (118.6, 100.1) |
| r4 | 15/15 | **0** | **0** | complete | 13/15 | 0 |
| r5 | 15/15 | **0** | **0** | complete | 13/15 | 0 |

**Totals:** 75 of 75 images written. No frame lost in any trial. No gap in
any session (217-225 frames recorded per trial against the ~225 expected).
No overload: the larger queue absorbed the writer's backlog (high-water
12-13 of 15, where capacity 8 had filled to 8/8).

**Image spacing (the 100 ms criterion):** the 15 image-completing frames of
every trial land at exactly t = 100, 200, ..., 1500 ms of sensor time.
That is 100.0 ms apart in every trial: the series runs on the sensor's
10 fps cadence with no image arriving early or late.

**Callbacks:** 28.9-124.9 ms. The two slowest are image 1 and image 2 of a
trial, as before: a fresh buffer at the start. After that they settle to
70-80 ms (or ~30 ms when a recycled buffer is available).

**Writer:** still several seconds behind at the end of a trial (the queue
peaked at 13 of 15). With the writeback tweaks that backlog no longer
produces gaps.

**Conclusion:** the success criteria are met on this configuration. The
kernel writeback tweaks, not the buffer pre-fill, are what stopped the frame
loss.

