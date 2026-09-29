# Continuous Imaging: Requirements and Constraints

## Purpose

Design an opt-in camera acquisition path for capturing a sequence of closely
spaced images at one manually selected field of view. The sequence is intended
for motion-blur and pixel-noise experiments on wet- and dry-mounted slides.
Image acquisition should not wait for the preceding image's disk write to finish,
provided the camera, processing, and storage hardware can sustain the requested
rate.

This document records intended requirements. It does not authorize or describe
an implementation already completed.

## Functional requirements

1. **Preserve the existing acquisition path.** Existing single-image,
   accumulation, z-stack, focus, background, and darkfield workflows must
   continue to behave as they do today. Continuous/queued capture is a separate,
   explicitly selected path.
2. **Capture a requested series.** A caller can request a finite number of
   output images (`image_count`) and the number of consecutive camera frames
   contributing to each output image (`nframes`). These are distinct values:
   `nframes=1, image_count=100` means 100 single-frame images; `nframes=10,
   image_count=100` means 100 averaged images, each based on 10 camera frames.
3. **Acquisition overlaps persistence.** After exactly `nframes` frames have
   contributed to one output image, acquisition/accumulation can proceed for
   the next image while a background writer saves the completed image.
4. **Define image boundaries exactly.** No extra camera frame is required to
   trigger saving. Every input frame belongs to at most one output image, and
   each complete output contains exactly the requested number of frames.
5. **Keep one TIFF per output image.** Do not require a multipage movie TIFF.
   Keep the standard sequential image suffix convention (`_image1`,
   `_image2`, etc.) and a JSON sidecar for every TIFF.
6. **Preserve pixel data representation.** TIFF output is one float32 mean-image
   page per output image, in the existing full-resolution RGGB/Bayer mosaic
   spatial arrangement. Do not demosaic, resize, crop, color-convert, or apply
   lossy compression in the scientific output path. Document and validate the
   actual raw input packing/bit depth separately; the existing camera config is
   `SRGGB12` and callback currently reads samples as `uint16`.
7. **Do not write a variance page.** Variance is not used by this experiment and
   should not be produced for this new capture path. Existing capture behavior
   remains unchanged unless separately approved.
8. **Support exposure configuration.** Apply the selected objective's default
   exposure using the existing objective control. An optional exposure override
   is applied after carousel/objective selection. Record the exposure actually
   used in each output image's metadata.
9. **Retain current microscope and file conventions.** Motor remains responsible
   for manually positioned FOV context, barcode/smear/FOV naming, folder
   creation, objective selection, illumination lifecycle, and stop handling.
   Barcode is initialized before the call; XY and Z may be omitted and recorded
   as `null` metadata / `NA` filename tokens. Do not change the current folder
   schema.
10. **Keep image metadata.** At minimum preserve current camera/sensor/lens/light,
    exposure, gain, frame count, magnification, Z, image filename, and filepath
    fields. Add clearly defined sequence index and timing fields needed to
    distinguish acquisition timing from file-write duration. Metadata must not
    imply that write completion time is sensor capture time.
11. **Completion and errors are observable.** Caller/operator can determine
    requested, acquired, written, and failed image counts and whether the series
    completed. Camera errors, write errors, timeout, cancellation, and overload
    must not be silently reported as successful completion.

## Resource and overload requirements

- Use a **bounded** queue/buffer pool. Queue capacity is a configurable,
  documented safety limit, not an unbounded attempt to hide insufficient write
  throughput.
- A completed image submitted to the writer must be independent of arrays reused
  for subsequent accumulation. Prefer a distinct float32 mean array per result;
  the active sum buffer can then be reset/reused.
- Measure peak memory including: camera-owned buffers, input frame, active
  accumulator, queued mean arrays, the writer's in-progress image, TIFF
  conversion/temporary buffers, Python overhead, and OS headroom.
- Queue saturation must use an explicit safe policy. Preferred initial policy:
  stop/abort the series with an explicit overload error and report partial
  results; never silently drop frames or overwrite queued image data.
- Do not claim a target frame rate is guaranteed. Record timing and measure
  sustained acquisition and write throughput on the target Raspberry Pi and
  storage device.
- If the camera callback cannot safely block, define and test a bounded
  handoff/buffer-pool policy that detects overflow. Do not block the callback
  indefinitely while holding camera-owned resources.

## Non-functional and compatibility constraints

- Reuse the existing Picamera2 camera service and ZMQ endpoint unless a tested
  technical reason requires otherwise. Do not run a second service on the same
  port.
- Add a distinct command/state path for series capture; do not reinterpret the
  existing `accumulate` command in a way that changes current workflows.
- The live preview commands must continue to function during idle operation and
  must not corrupt/modify scientific frame buffers.
- Keep camera acquisition coordination in the camera service. Keep microscope
  positioning, objective, lighting, barcode, folders, and experiment orchestration
  in `Motor`/existing file-transfer conventions.
- Do not consolidate per-image JSON sidecars into the z-stack JSON in this
  project phase. Do not change `populate_zstack_json_from_folder()`.
- Changes should be developed and reviewed with the existing working-tree
  changes preserved. Prefer a commit or branch checkpoint before large refactors.

## Not in scope for the first implementation

- H.264/MP4 or other lossy video output as scientific data.
- Continuous-stage movement or autofocus during a series.
- Changing the existing folder schema or existing z-stack capture behavior.
- Guaranteed fixed frame cadence beyond what exposure, sensor readout, frame
  processing, memory bandwidth, and storage throughput can sustain.
- JSON aggregation or deletion of per-image JSON files.

## Terminology

- **Input frame:** one sensor frame delivered by the camera pipeline.
- **`nframes`:** number of input frames accumulated into one output image.
- **Output image:** one float32 mean TIFF page and its JSON sidecar.
- **`image_count`:** number of output images requested in a series.
- **Series:** the finite run of `image_count` output images.
- **Queue item:** an immutable/independent completed output image and its
  associated filename/metadata, waiting for persistence.