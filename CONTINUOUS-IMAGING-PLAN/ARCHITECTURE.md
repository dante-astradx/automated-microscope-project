# Continuous Imaging: Proposed Architecture

## Status and goal

This is a design proposal only. The goal is to add a continuous/queued series
path while retaining the current camera capture behavior for all existing users.
Acquisition of the next output image should overlap writing the preceding output
image, with bounded memory and explicit overload/error reporting.

## Current code path (baseline)

The relevant components are:

- `Motor` in `motor.py` chooses the imaging context and calls the camera client.
- `Camera` in `camera.py` sends JSON commands through a ZeroMQ REQ socket.
- `camera_zmq.py` owns the ZeroMQ REP server, configures Picamera2, and starts
  an encoder with `OutputZMQ` as its output callback.
- `OutputZMQ.outputframe()` receives each encoder frame. The current accumulator
  holds running pixel sum and sum-of-squares arrays in RAM.
- The current callback calls `save_accumulated()` synchronously after the
  requested number of frames has accumulated. That routine computes mean and
  variance, writes a two-page float32 TIFF, then writes JSON.
- Current accumulation state and the `accumulating()` status predicate do not
  represent “all frames accumulated and files written” identically; the current
  path waits for output files in the motor workflow.

The new path must not alter that legacy behavior as a side effect.

## Proposed components and responsibilities

### 1. Motor orchestration (`motor.py`)

Motor should:

1. Validate/set the slide barcode context already configured on the file-transfer
   instance, smear, FOV, optional coordinates, magnification, image count,
   `nframes`, and optional exposure.
2. Reuse existing folder generation, filename conventions, manifest/z-stack
   metadata initialization as agreed; keep individual sidecars and do not run
   JSON consolidation.
3. Start illumination, move the carousel, then apply any exposure override.
4. Submit one finite series request to `Camera`, poll progress/status, relay stop
   requests, and ensure illumination cleanup in a `finally` path.
5. Report completion only when all requested output files have been written, not
   merely when acquisition has ended.

Motor must not issue one command per output image if that introduces a round trip
or wait for each image's file write. The camera should own time-sensitive frame
group boundaries within the requested series.

### 2. Camera client (`camera.py`)

Add clearly named series-capture start and status methods/command payloads,
separate from `take_rpi_image()` and the current `accumulate` protocol. Preserve
the client's existing REQ/REP discipline and timeout/reconnection behavior, or
improve it with isolated tests.

### 3. Camera service (`camera_zmq.py` and possibly a new module)

Keep one Picamera2 instance, one encoder, and one server endpoint. Add an
independent series-capture state machine. If the new state/worker logic makes
`camera_zmq.py` too complex, put it in a focused module (for example
`continuous_capture.py`) that the current server instantiates. Do not duplicate
camera initialization or bind a competing server to the same port.

The service should:

1. Accept one series configuration: output count, input frames per output,
   filename/path information, exposure/gain and microscope metadata.
2. On each callback during an active series, obtain a stable view/copy of the
   sensor frame and add it to the active group's running sum.
3. When exactly `nframes` input frames are included, produce a **separate
   float32 mean array**. The frame that completes one group is not also counted
   in the next group.
4. Enqueue that independent mean and the matching output metadata. Reset/reuse
   the sum state and continue receiving frames without waiting for TIFF/JSON I/O.
5. Have one writer worker consume completed results in sequence order and write
   one single-page float32 TIFF plus one JSON sidecar per output image.
6. Track acquisition and write progress independently, including queue depth,
   frames in current group, outputs captured/written, first error, and terminal
   state.
7. On completion, ensure the last partial/complete state is handled explicitly;
   never claim a partial group is a complete output. Drain the writer queue before
   returning successful series completion.

### 3a. ZeroMQ status and writer-error reporting — resolved

Use a simple **start-and-poll** protocol. Starting a series returns promptly with
acceptance (or rejection), a `series_id`, and the initial state; acceptance does
not mean capture or saving is complete. Motor polls status for that ID until a
terminal result. Suggested messages (field names illustrative):

```json
{"command":"start_series","image_count":100,"nframes":10}
```

```json
{"ok":true,"series_id":"abc123","state":"capturing","requested_images":100}
```

```json
{"command":"series_status","series_id":"abc123"}
```

An in-progress response reports `state`, `requested_images`,
`captured_images`, `written_images`, `queue_depth`, and `queue_high_watermark`.
Also include process resident memory (`process_rss_bytes`) and system available
memory (`system_available_bytes`) when the platform exposes them. These fields
make writer backlog and memory pressure observable without adding a new UI
endpoint. Memory values are diagnostics, not a promise that all reported system
available memory is safe for the queue to consume.

On failure, retain (latch) the first terminal error and return it for subsequent
status polls until Motor observes it or a new series starts. Report a stable
error `code`, concise human-readable `message`, and when applicable failing
`image_index`/`filename`; always include captured/written counts and state. For
example:

```json
{
  "ok": true,
  "series_id": "abc123",
  "state": "failed",
  "requested_images": 100,
  "captured_images": 24,
  "written_images": 18,
  "queue_depth": 5,
  "error": {
    "code": "WRITE_FAILED",
    "image_index": 19,
    "filename": "..._image19",
    "message": "Could not write TIFF: no space left on device"
  }
}
```

Writer, queue-overload, camera, cancellation, and protocol errors must not be
reported as `COMPLETE`. Preserve already completed output files and report
partial counts. Keep detailed traceback/diagnostics in the camera log; the
ZeroMQ/UI error should be concise and actionable. Protect status shared between
the callback, writer, and server threads with a lock or equivalent synchronization.

Motor treats only a fully drained `COMPLETE` result as success. For `FAILED`,
`OVERLOADED`, or `CANCELLED`, it logs and publishes a concise status message with
the error and partial counts, updates the existing scoreboard to an error state,
and raises an exception so the existing microscope-app task handler performs
cleanup. Initially, use the existing `update_status`/`log_output` and scoreboard
paths; a separate UI endpoint or progress card is out of scope. Polling cadence,
request timeouts, and behavior if the camera server becomes unreachable must be
covered by protocol tests.

Motor should emit concise, **rate-limited** progress lines through its existing
logger, which feeds the web terminal/SSE and daily log. Report a snapshot at
series start, periodically during acquisition (for example every 5 seconds or
after a meaningful queue-depth change), and at completion, cancellation, or
failure. Include captured/written counts, current and high-water queue depth,
process RSS, system available RAM, and state. Do not log per frame. Example:

```text
Continuous capture: 42/100 captured, 36 written; queue 6 (peak 8); process RSS 620 MiB; system available 1.1 GiB
```

The camera status response is the source of queue and memory metrics; Motor
formats and publishes these summaries through existing logging/status paths.
Unavailable memory metrics should be reported as unavailable rather than causing
capture failure. Final and overload messages must include the terminal state and
captured/written counts.

### 4. Writer queue and memory ownership

Use a bounded `queue.Queue` or equivalent. The queue contains a completed,
independent float32 mean array and immutable metadata (filename, image sequence,
exposure, gain, magnification, Z, and timestamps). Do not put a reference to a
sum array that is reset/reused by the callback.

The initial design is a single writer to preserve file ordering and reduce
storage contention. Queue size is configured in **images**, and capacity should
be selected using measured RAM headroom, not an arbitrary large maximum. Account
for one image currently being written in addition to the queued images. A full
queue must trigger the defined overload policy; no silent frame drops.

For 4056 × 3040 pixels, one float32 output mean is about 49 MB (47 MiB). Peak
memory is higher than queue capacity times this amount because of active sums,
input/camera buffers, and writer temporaries. Measure actual resident memory on
the Raspberry Pi.

### 5. Pixel and TIFF data path

- Continue using the current full-resolution RGGB/Bayer pixel arrangement; no
  demosaicing, cropping, resizing, color conversion, or lossy compression.
- Preserve float32 mean-image TIFF data. New path writes one TIFF page per output
  file and omits variance by design.
- `nframes` is any positive integer; the new series path has no configured
  `nframes` cap. Practical values and sustainable continuous-acquisition ranges
  will be established through staged software and hardware tests.
- Use the existing `uint32` running-sum accumulator for the initial
  implementation. Do not add sum-of-squares state for the new path because the
  variance page is omitted. A completed mean is produced as a separate float32
  array before the sum buffer is reset/reused.
- No configured `nframes` cap does not eliminate integer-overflow risk. Before
  scientific use, verify the actual input sample range and test accumulator
  correctness across the intended `nframes` range. If the maximum intended sum
  could exceed `uint32`, revisit the accumulator type rather than silently
  accepting corrupted means.
- Validate whether current raw encoder bytes are packed/unpacked and what exact
  significant bit depth the callback receives. Do not infer sensor effective bit
  depth solely from the `uint16` NumPy container.
- For `nframes=1`, the output float32 mean should equal the current callback's
  decoded single input frame converted to float32 (within exact conversion
  expectations). For larger `nframes`, preserve the current arithmetic mean
  definition and verify it against deterministic reference inputs.

### 6. Metadata and timing

Keep existing metadata fields where applicable. Add sequence index, requested
`nframes`, and clearly defined timestamps. Distinguish at least:

- input frame timestamp(s), preferably camera/encoder timestamp if reliably
  available, otherwise a documented monotonic callback timestamp;
- time the completed mean is enqueued;
- writer start/end or output write duration;
- series start/end and completion/error status.

Do not label time from request acceptance through file writing as sensor exposure
time. Preserve actual exposure/gain read from camera metadata, not only requested
values. If a group has per-frame exposure metadata, decide whether to validate
consistency or record the per-group actual values.

## State model

Suggested series states (names illustrative):

`IDLE -> ACCEPTED -> CAPTURING -> DRAINING_WRITER -> COMPLETE`

Terminal alternatives: `CANCELLED`, `FAILED`, or `OVERLOADED`. Capture may finish
before writing; the series is successful only after all requested outputs are
written. If an error occurs, report captured and written counts and preserve
already completed files. Define cleanup for an incomplete current group.

The legacy accumulation state and series state must not accidentally accept
conflicting capture requests. Reject incompatible simultaneous acquisition
commands explicitly.

## Phased implementation

1. **Baseline and checkpoints:** commit/branch current work; record current
   legacy behavior and one-image file/metadata examples.
2. **Pure accumulator tests:** test exact frame grouping, mean values, sequence
   boundaries, and no extra trigger frame without physical camera hardware.
3. **Writer worker tests:** test independent buffer ownership, output order,
   TIFF/JSON content, writer exceptions, queue saturation, cancellation, and
   clean shutdown.
4. **Protocol integration:** add new command/state while exercising the legacy
   `accumulate` command tests unchanged.
5. **Motor integration:** configure folders, carousel, exposure override, series
   request, stop and cleanup behavior.
6. **Hardware characterization:** small frame counts first; measure actual
   cadence, loss, queue depth, save throughput, peak RAM, and image equivalence.
7. **Rollout:** keep the new path opt-in until acceptance criteria pass. Do not
   remove legacy functionality in this project phase.

## Resolved technical decisions

### Raw encoder output and sensor timestamp — resolved from code/API

- The camera config requests a `raw` stream in `SRGGB12` format at
  4056 × 3040, and `picam2.encode_stream_name` is set to `"raw"`.
- The configured encoder is Picamera2's base `Encoder`, not an H.264/MJPEG
  encoder. Picamera2 sets the encoder's stream format, size, and stride from the
  selected `raw` stream. Its `_encode()` maps that stream buffer and passes the
  mapped frame to the output callback. The callback must consume or copy the
  buffer before it returns; it must not retain the mapped camera buffer for the
  asynchronous writer.
- The existing callback uses `np.frombuffer(frame, dtype=np.uint16)`. The code
  and stream configuration establish this as the current interpretation, but
  do not alone prove runtime stride, packing, or effective sensor bit depth.
  Hardware validation must inspect the active stream configuration and confirm
  dimensions, stride, dtype, Bayer order, and sample-value range before treating
  these properties as experimentally verified.
- Picamera2's installed `Encoder._timestamp()` reads
  `request.request.metadata[controls.SensorTimestamp]`. It converts the value
  from nanoseconds to microseconds, subtracts the first encoded frame's value,
  and passes the resulting relative timestamp to `Output.outputframe()`.
- Libcamera defines `SensorTimestamp` as the time when the first row of the
  sensor active array is exposed. It is nanoseconds on Linux `CLOCK_BOOTTIME`,
  a monotonic clock measured since system boot; it is not wall-clock time. The
  encoder callback receives this value rebased to zero and expressed in
  microseconds. Use it for relative frame cadence and inter-frame timing; do not
  interpret it as a calendar timestamp. If cross-device wall-clock correlation
  is required later, investigate libcamera's separate `FrameWallClock` metadata.
- The timestamp API/clock choice is resolved for design. Hardware tests must
  still verify that timestamp metadata is present and behaves as expected on
  the deployed camera/libcamera version.

### Sidecar `frames` and timing fields — resolved

- Each new series-image sidecar must include `frames`, equal to the configured
  `nframes` contributing input camera frames for that output image.
- Each new series-image sidecar must include `image_time`, in seconds, measured
  with a monotonic clock from the start of that image group's accumulation to
  the moment its completed float32 mean is placed on the writer queue. It covers
  the acquisition/accumulation work for that group; it is not a single-frame
  exposure duration and does not include queue wait or disk persistence.
- Add `save_time`, in seconds, measured monotonically from queue insertion until
  the TIFF and JSON sidecar have been successfully written and the writer has
  completed that queue item. This is a useful per-image diagnostic for writer
  throughput and queueing pressure, but it is not an acquisition or exposure
  duration. Record a write failure as an error rather than a successful
  `save_time`.
- Keep the legacy capture path's existing `time` field and semantics unchanged.
  Do not reuse `time` for `image_time` in the new series path: its current meaning
  includes the save operation, so aliasing it to the new acquisition-only metric
  would be misleading. No production consumer of `frames` or `time` was found
  during the code search; existing workflows must still retain their current
  fields and behavior.
- Use monotonic timestamps/durations for `image_time` and `save_time`; keep wall
  clock date/time metadata as a separate field when needed. The camera sensor
  timestamp remains the source for per-input-frame cadence, as resolved above.

## Remaining open technical decisions before implementation

- Queue capacity and exact overload response based on measured sustained storage
  throughput and safe RAM headroom.