# Continuous Imaging: Intended User Workflow

## Purpose

Describe how an operator is expected to use a future continuous imaging mode.
This workflow is prospective; the series-capture path described here has not
been implemented.

## Before a series

1. Start the microscope and camera services using the normal, existing startup
   process. The new mode will use the same camera service, not a second server.
2. Initialize the slide barcode on the existing file-transfer object before
   invoking the motor workflow.
3. Manually move the slide in X/Y to the desired field of view and manually set
   focus in Z. The series workflow records those values if supplied but does not
   move these axes.
4. Choose smear ID, FOV number, and magnification (initial scope: 20x or 40x).
5. Choose:
   - `image_count`: number of TIFF/JSON output pairs to produce;
   - `nframes`: number of consecutive sensor frames averaged into each output;
   - optional exposure time in microseconds. If omitted, use the default exposure
     set by selecting the objective.
6. Confirm adequate free storage and that the selected exposure is compatible
   with the rate the camera can sustain. The requested image count and `nframes`
   are counts, not a guarantee of an FPS.

## Start and run

1. Call the future `Motor.collect_motion_blur_data(...)` method with the barcode
   already initialized, and the chosen smear/FOV/objective/counts/exposure.
2. Motor creates/reuses the established barcode/smear/FOV/objective folders and
   manifest/z-stack metadata container without changing their naming schema.
3. Motor turns on illumination, moves the objective carousel, and applies the
   optional exposure override after objective selection.
4. Motor submits **one finite series request** to the camera service. The camera
   groups the incoming frame stream into consecutive, non-overlapping groups of
   `nframes` and queues one float32 mean image for each group.
5. While the writer saves earlier output images, camera acquisition and
   accumulation continue, subject to processing and storage throughput and the
   bounded queue capacity.
6. The operator can request/observe progress: requested outputs, groups
   captured, outputs written, queue depth, and any error/overload state.
7. A normal completion means every requested output image and sidecar has been
   written. Capture completion alone is not enough; the writer must drain.
8. A stop or overload reports a non-success terminal state and the number of
   complete outputs retained. No silent dropped frames are acceptable.

## Output layout and naming

- Save under the current generated slide case and FOV/objective directory.
- Each output image has its own TIFF and JSON sidecar; no multipage movie TIFF.
- TIFF contains one full-resolution float32 mean-image page in RGGB/Bayer spatial
  order. No variance page, demosaicing, rescaling, color conversion, or lossy
  compression in the scientific output path.
- Filenames retain the standard `FileTransfer5` image basename with a sequential
  `_imageN` suffix (for example `_image1`, `_image2`). Missing coordinate tokens
  use `NA`; corresponding metadata values are JSON `null`.
- Per-image JSON files remain alongside their TIFFs. They are not folded into
  the z-stack JSON in this phase.
- Sidecars record sequence number, `nframes`, actual exposure/gain, magnification,
  supplied coordinates, and well-defined acquisition/write timing metadata.

## Meaning of acquisition settings

- `nframes=1`, `image_count=100`: produce 100 mean TIFFs, each based on one sensor
  frame. No across-frame averaging occurs within an output image.
- `nframes=10`, `image_count=100`: produce 100 mean TIFFs, each based on a
  separate group of 10 consecutive sensor frames. Total requested source frames
  are 1,000 if the series completes without loss.
- `image_count` controls output images; it does not set camera FPS.
- Exposure time controls each contributing sensor frame's integration duration.
  Longer exposures can limit attainable frame cadence and affect within-frame
  motion blur.
- Inter-output cadence is measured from frame timestamps and is not inferred from
  JSON write-completion time.

## After a series

1. Wait for terminal `COMPLETE` and writer drain before treating the dataset as
   complete or transferring it.
2. Check that requested, captured, and written output counts agree and that no
   queue overflow or write error was reported.
3. Inspect metadata and sample TIFFs to verify exposure, gain, frame count, data
   type, dimensions, and RGGB layout.
4. Record the experiment conditions needed for a dry/wet comparison: mounting
   condition, smear/FOV, magnification, exposure, gain, `nframes`, image count,
   and measured timing.
5. Keep the normal non-continuous workflows available for other acquisition
   tasks.