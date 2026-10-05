"""Offline microbenchmark of the per-frame work in the camera callback.

No camera, no camera service, no ZeroMQ: synthetic full-size frames only. This
is the control for the in-callback stage timers (frame_diagnostics stages_ms):
it measures each operation alone, so differences against the live numbers
point at contention (writer thread, GIL, memory pressure) rather than the
arithmetic itself.

Operations, matching the production code:
  decode          np.frombuffer + reshape of the padded raw buffer (camera_zmq)
  crop_copy       copying the cropped (3040, 4056) view (what np.add reads)
  zeros_u32       np.zeros uint32 sum allocation (first frame of a series)
  add_u16_u32     np.add(sum, active_view, out=sum)          ("accumulate")
  empty_f32       np.empty float32 mean allocation            ("complete", part 1)
  divide_u32_f32  np.divide(sum, nframes, out=mean)           ("complete", part 2)
  empty+divide    a fresh np.empty + divide each time, as production does
  reset_fill      sum.fill(0)                                  ("reset")
  tiff_tmp        tifffile write of one float32 image to /tmp     (writer thread)
  tiff_shm        tifffile write of one float32 image to /dev/shm (writer thread)

Safe to run at any time; uses ~400 MiB of RAM briefly and deletes its temp
files. Does not touch /home/microscope_auto/Images.

Usage:
  python IMAGE-SERIES-ANALYSIS/bench_frame_stages.py [--iterations 20] [--warmup 3]
"""

import argparse
import os
import platform
import sys
import tempfile
import time

import numpy as np
import tifffile

ROWS = 3040
ACTIVE_WIDTH = 4056
RAW_WIDTH = 4064  # padded row length in uint16 samples, as delivered by the camera


def measure(fn, iterations, warmup, setup=None):
    """Time ``fn`` after ``warmup`` untimed calls. Return a list of seconds."""
    for _ in range(warmup):
        if setup:
            setup()
        fn()
    times = []
    for _ in range(iterations):
        if setup:
            setup()
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return times


def report(name, times, nbytes):
    ms = np.asarray(times) * 1000
    median = float(np.median(ms))
    gbps = (nbytes / (median / 1000) / 1e9) if nbytes and median > 0 else None
    gbps_text = f"{gbps:7.2f}" if gbps is not None else "      -"
    print(f"  {name:16s} median {median:8.2f}  p95 {np.percentile(ms, 95):8.2f}  "
          f"max {ms.max():8.2f} ms   {gbps_text} GB/s   ({nbytes / 2**20:7.1f} MiB touched)")
    return median


def tiff_bench(directory, image, iterations, warmup):
    if not os.path.isdir(directory):
        print(f"  (skipping {directory}: not a directory)")
        return None
    fd_, path = tempfile.mkstemp(prefix="bench_frame_stages_", suffix=".tif", dir=directory)
    os.close(fd_)
    try:
        def write():
            tifffile.imwrite(path, image)  # same call as continuous_capture._write_tiff
        return measure(write, iterations, warmup)
    finally:
        if os.path.exists(path):
            os.remove(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--tiff-iterations", type=int, default=5,
                        help="TIFF writes are slow; fewer iterations by default")
    parser.add_argument("--nframes", type=int, default=1, help="divisor for the mean")
    args = parser.parse_args()
    it, wu = args.iterations, args.warmup

    print(f"Python {platform.python_version()}  numpy {np.__version__}  "
          f"tifffile {tifffile.__version__}  ({platform.machine()}, {os.cpu_count()} CPUs)")
    print(f"Frame: raw ({ROWS}, {RAW_WIDTH}) uint16, active ({ROWS}, {ACTIVE_WIDTH}); "
          f"{it} iterations after {wu} warmup")

    rng = np.random.default_rng(0)
    raw = rng.integers(0, 4096, size=(ROWS, RAW_WIDTH), dtype=np.uint16)
    raw_bytes = raw.tobytes()  # stand-in for the camera buffer passed to outputframe
    active_n = ROWS * ACTIVE_WIDTH
    u16, u32, f32 = 2, 4, 4

    sum_u32 = np.zeros((ROWS, ACTIVE_WIDTH), dtype=np.uint32)
    mean = np.empty((ROWS, ACTIVE_WIDTH), dtype=np.float32)
    decoded = np.reshape(np.frombuffer(raw_bytes, dtype=np.uint16), (ROWS, -1))
    active = decoded[:, :ACTIVE_WIDTH]

    def decode():
        im = np.frombuffer(raw_bytes, dtype=np.uint16)
        np.reshape(im, (ROWS, -1))

    results = {}
    print("\nCallback work (camera thread):")
    results["decode"] = report("decode", measure(decode, it, wu), 0)
    results["crop_copy"] = report("crop_copy", measure(lambda: active.copy(), it, wu),
                                  active_n * u16 * 2)
    # np.zeros/np.empty of this size only reserve memory (lazy mmap): pages are
    # faulted in on first write, so their cost shows up in the next operation.
    results["zeros_u32"] = report("zeros_u32", measure(
        lambda: np.zeros((ROWS, ACTIVE_WIDTH), dtype=np.uint32), it, wu), 0)
    results["add_u16_u32"] = report("add_u16_u32", measure(
        lambda: np.add(sum_u32, active, out=sum_u32), it, wu,
        setup=lambda: sum_u32.fill(0)), active_n * (u16 + 2 * u32))
    results["empty_f32"] = report("empty_f32", measure(
        lambda: np.empty((ROWS, ACTIVE_WIDTH), dtype=np.float32), it, wu), 0)
    results["divide_u32_f32"] = report("divide_u32_f32", measure(
        lambda: np.divide(sum_u32, args.nframes, out=mean), it, wu), active_n * (u32 + f32))

    def empty_and_divide():
        fresh = np.empty((ROWS, ACTIVE_WIDTH), dtype=np.float32)
        np.divide(sum_u32, args.nframes, out=fresh)
        fresh.flags.writeable = False
    results["empty+divide"] = report("empty+divide", measure(empty_and_divide, it, wu),
                                     active_n * (u32 + f32))
    results["reset_fill"] = report("reset_fill", measure(lambda: sum_u32.fill(0), it, wu),
                                   active_n * u32)

    series_frame = (results["add_u16_u32"] + results["empty+divide"] + results["reset_fill"])
    first_touch = results["empty+divide"] - results["divide_u32_f32"]
    print(f"\n  series frame at nframes=1 (add + empty+divide + reset): {series_frame:8.2f} ms "
          f"(frame period 100 ms)")
    print(f"  first-touch cost of a fresh 47 MiB float32 array (empty+divide - divide): "
          f"{first_touch:8.2f} ms")
    print("  decode is only a view (no copy); the cropped view is read inside np.add")

    image = np.asarray(rng.random((ROWS, ACTIVE_WIDTH)), dtype=np.float32)
    print("\nWriter work (writer thread, not the callback):")
    for name, directory in (("tiff_tmp", "/tmp"), ("tiff_shm", "/dev/shm")):
        times = tiff_bench(directory, image, args.tiff_iterations, 1)
        if times:
            results[name] = report(name, times, image.nbytes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
