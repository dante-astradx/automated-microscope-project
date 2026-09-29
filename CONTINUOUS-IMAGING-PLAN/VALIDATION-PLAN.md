# Continuous Imaging: Validation and Acceptance Plan

## Goal

Demonstrate that the new capture path produces the intended image data and
sequence timing, overlaps capture with writing when hardware permits, and fails
visibly rather than silently losing or corrupting data. Validate in software
before using the output for scientific conclusions.

## A. Unit and component tests (no camera hardware)

### Grouping and mean calculation

- For `nframes=1`, assert one output is generated from exactly the single input
  frame and that the float32 mean equals that frame's sample values.
- For `nframes>1`, feed deterministic arrays with known values; assert each mean
  is correct and groups contain exactly `nframes` consecutive frames.
- Feed multiple groups with distinct values to prove no frame is duplicated,
  skipped, or shared between neighboring outputs.
- Assert no additional callback/frame is needed to finalize a group.
- Assert the new TIFF payload has one float32 page and no variance page.
- Test largest supported `nframes` against accumulator dtype/overflow policy.

### Buffer ownership and writer

- Enqueue a result, reuse/reset the accumulator immediately, then assert the
  writer saves the original completed mean, not the modified accumulator.
- Assert outputs are written in sequence-number order to individual TIFF files.
- Assert one JSON sidecar is written per TIFF with matching filename, group
  index, `nframes`, exposure/gain and timing fields.
- Simulate TIFF and JSON write errors and verify failure status, counts, and
  worker shutdown behavior.
- Fill the bounded queue deliberately; assert the agreed overload policy is
  reported, with no silent frame drop or false success.
- Test stop/cancel during accumulation, between groups, and while writer queue
  is draining; distinguish complete images from incomplete groups.

### Protocol and compatibility

- Test accepted/rejected series requests, status polling, invalid parameters,
  busy camera state, cancellation and terminal results.
- Assert legacy `accumulate` tests and observed behavior remain unchanged.
- Assert UI preview/status requests cannot mutate or corrupt queued data.
- Assert incompatible legacy and series captures cannot run simultaneously.

## B. Motor workflow tests (mock camera/filesystem)

- Verify barcode is already supplied by the file-transfer object and existing
  folder generator/naming schema is used.
- Verify smear/FOV/objective metadata and omitted coordinate representation.
- Verify carousel selection occurs before an optional exposure override.
- Verify one series request carries `image_count`, `nframes`, paths and metadata;
  Motor does not issue per-output capture requests or wait on each file write.
- Verify illumination is turned off on success, stop and exceptions.
- Verify motor stop requests are forwarded and progress/terminal errors are
  surfaced.
- Verify image JSON sidecars remain in their folders and no z-stack JSON
  consolidation is attempted.

## C. Hardware smoke tests (small and increasing runs)

Run tests on the Raspberry Pi and intended storage volume, starting with the
lowest-risk settings and increasing only after inspection:

1. `image_count=2`, `nframes=1`.
2. `image_count=10`, `nframes=1`.
3. `image_count=10`, `nframes=10`.
4. Longer series at planned magnification/exposure/counts.

For every run, record:

- Requested, fully acquired, fully written, and partial counts.
- Per-frame or per-group camera timestamps and inter-frame/inter-image intervals.
- Acquisition callback/accumulation processing time, enqueue time, writer time,
  queue depth/high-water mark, and any backpressure or overflow.
- Process peak RSS, system available RAM, swap activity, CPU load and storage
  throughput/free space.
- Confirm the web terminal/daily log receives rate-limited queue and memory
  snapshots at start, during the run, and at terminal completion/error; confirm
  snapshots do not generate a per-frame log flood.
- TIFF dimensions, page count, dtype, file size, sample values and RGGB pattern.
- JSON metadata agreement with actual camera exposure/gain and requested settings.
- Any camera errors, missing files, malformed TIFFs, or timing gaps.

Do not infer achieved FPS from number of requested frames divided by total series
duration alone; inspect frame timestamps and gaps. Distinguish sensor cadence from
writer completion timing.

## D. Data-equivalence checks

- At `nframes=1`, compare the new float32 output against the current saved mean
  page for the same raw input frame, allowing only documented representation
  differences.
- At `nframes>1`, compare calculated means against an offline mean from retained
  sample input frames or a deterministic fixture.
- Confirm no demosaic, resize, crop, color conversion, or lossy compression has
  entered the scientific output path.
- Confirm there is exactly one TIFF page, float32, at full dimensions, and that
  the variance page is absent only in the new path.

## Acceptance criteria

Do not enable the feature for normal scientific acquisition until all of the
following hold:

1. All requested output groups have exactly the requested input frame count.
2. No silent drops, duplicate frames, overwritten buffers, or false-success
   completions occur in overload/error tests.
3. Capturing later groups demonstrably overlaps writing earlier groups when
   writer latency permits; all queued output is drained before success.
4. Sustained hardware runs at expected settings have no queue overflow, and
   measured queue/RAM/storage headroom is adequate for intended run length.
5. TIFF pages, dtype, dimensions, RGGB spatial arrangement, and mean values meet
   the data-format requirements.
6. Per-image JSON is complete and individual sidecars are retained.
7. Existing capture, z-stack, preview, and motor workflows pass regression tests.
8. The achieved cadence and known limitations are documented for experimenters.