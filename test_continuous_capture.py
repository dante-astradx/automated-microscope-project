"""Tests for the hardware-independent continuous capture core.

These use small synthetic frames and pytest's tmp_path. They verify grouping,
mean values, buffer ownership, writer output, and error/overload handling. They
cannot establish camera frame rates or storage throughput.
"""

import json
import threading
import time

import numpy as np
import pytest
import tifffile
from unittest.mock import patch

import continuous_capture as cc

ROWS = 4
WIDTH = 6
PADDED_WIDTH = 8  # two columns of simulated row padding
PAD_VALUE = 60000  # never a valid output value; proves padding is cropped


def make_frame(value, rows=ROWS, padded_width=PADDED_WIDTH, width=WIDTH):
    """A uint16 frame: active columns hold ``value``, padding holds PAD_VALUE."""
    frame = np.full((rows, padded_width), PAD_VALUE, dtype=np.uint16)
    frame[:, :width] = value
    return frame


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def make_series(tmp_path, image_count=3, nframes=1, **kwargs):
    kwargs.setdefault("width", WIDTH)
    kwargs.setdefault("logger", lambda msg: None)
    return cc.ContinuousSeries(
        image_count=image_count,
        nframes=nframes,
        output_dir=str(tmp_path),
        base_filename="TEST_series",
        **kwargs,
    )


def read_tiff_pages(path):
    with tifffile.TiffFile(path) as tif:
        return [page.asarray() for page in tif.pages]


class BlockingTiffWriter:
    """Replaces _write_tiff and blocks until released, to hold the writer busy."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = []
        self._real = cc._write_tiff

    def __call__(self, path, image):
        self.calls.append(path)
        self.started.set()
        assert self.release.wait(5), "writer was never released"
        self._real(path, image)


# ----------------------------------------------------------------------------
# Grouping and mean calculation
# ----------------------------------------------------------------------------

class TestFrameGroupAccumulator:
    def test_nframes_1_mean_equals_the_single_frame_exactly(self):
        acc = cc.FrameGroupAccumulator(1, width=WIDTH)
        frame = make_frame(0)
        frame[:, :WIDTH] = np.arange(ROWS * WIDTH, dtype=np.uint16).reshape(ROWS, WIDTH) * 97

        group = acc.add_frame(frame, timestamp_us=123)

        assert group is not None
        assert group.mean.dtype == np.float32
        assert group.mean.shape == (ROWS, WIDTH)
        np.testing.assert_array_equal(group.mean, frame[:, :WIDTH].astype(np.float32))
        assert group.sensor_timestamps_us == (123,)

    def test_groups_are_consecutive_and_never_share_frames(self):
        acc = cc.FrameGroupAccumulator(3, width=WIDTH)
        groups = []
        for value in range(1, 10):  # frames 1..9 -> groups (1,2,3) (4,5,6) (7,8,9)
            group = acc.add_frame(make_frame(value), timestamp_us=value * 100)
            if group is not None:
                groups.append(group)

        assert [float(g.mean[0, 0]) for g in groups] == [2.0, 5.0, 8.0]
        assert [g.sensor_timestamps_us for g in groups] == [
            (100, 200, 300),
            (400, 500, 600),
            (700, 800, 900),
        ]

    def test_group_completes_on_its_last_frame_without_an_extra_frame(self):
        acc = cc.FrameGroupAccumulator(4, width=WIDTH)
        results = [acc.add_frame(make_frame(8)) for _ in range(4)]

        assert results[:3] == [None, None, None]
        assert results[3] is not None
        assert acc.frames_in_group == 0

    def test_mean_matches_legacy_arithmetic(self):
        rng = np.random.default_rng(7)
        frames = [rng.integers(0, 4096, size=(ROWS, PADDED_WIDTH), dtype=np.uint16) for _ in range(7)]
        acc = cc.FrameGroupAccumulator(7, width=WIDTH)
        for frame in frames:
            group = acc.add_frame(frame)

        total = np.zeros((ROWS, WIDTH), dtype=np.uint32)
        for frame in frames:
            total += frame[:, :WIDTH]
        legacy = (total.astype(float) / 7).astype(np.float32)
        np.testing.assert_array_equal(group.mean, legacy)

    def test_padding_columns_are_cropped(self):
        acc = cc.FrameGroupAccumulator(1, width=WIDTH)
        group = acc.add_frame(make_frame(5))

        assert group.mean.shape == (ROWS, WIDTH)
        assert not np.any(group.mean == PAD_VALUE)

    def test_default_width_is_4056(self):
        assert cc.DEFAULT_ACTIVE_WIDTH == 4056
        assert cc.FrameGroupAccumulator(1).width == 4056

    def test_large_nframes_with_max_values_does_not_overflow(self):
        nframes = 1000
        acc = cc.FrameGroupAccumulator(nframes, width=WIDTH)
        frame = make_frame(65535)
        for _ in range(nframes - 1):
            assert acc.add_frame(frame) is None
        group = acc.add_frame(frame)

        np.testing.assert_array_equal(group.mean, np.full((ROWS, WIDTH), 65535, dtype=np.float32))

    def test_mean_is_independent_of_the_reused_sum_buffer(self):
        acc = cc.FrameGroupAccumulator(1, width=WIDTH)
        first = acc.add_frame(make_frame(10)).mean
        acc.add_frame(make_frame(20))

        assert not first.flags.writeable
        np.testing.assert_array_equal(first, np.full((ROWS, WIDTH), 10, dtype=np.float32))

    def test_mutating_input_after_add_frame_does_not_change_the_result(self):
        acc = cc.FrameGroupAccumulator(2, width=WIDTH)
        frame = make_frame(4)
        acc.add_frame(frame)
        frame[:] = 1000  # simulate the camera reusing its buffer
        group = acc.add_frame(make_frame(6))

        np.testing.assert_array_equal(group.mean, np.full((ROWS, WIDTH), 5, dtype=np.float32))

    @pytest.mark.parametrize("bad", [
        np.zeros((ROWS, PADDED_WIDTH), dtype=np.uint32),
        np.zeros((ROWS, WIDTH - 1), dtype=np.uint16),
        np.zeros(PADDED_WIDTH, dtype=np.uint16),
    ])
    def test_rejects_invalid_frames(self, bad):
        acc = cc.FrameGroupAccumulator(1, width=WIDTH)
        with pytest.raises(ValueError):
            acc.add_frame(bad)

    def test_rejects_shape_change_mid_series(self):
        acc = cc.FrameGroupAccumulator(2, width=WIDTH)
        acc.add_frame(make_frame(1))
        with pytest.raises(ValueError):
            acc.add_frame(make_frame(1, rows=ROWS + 1))
        assert acc.frames_in_group == 1

    @pytest.mark.parametrize("value", [0, -1, True, 1.5])
    def test_rejects_invalid_nframes(self, value):
        with pytest.raises(ValueError):
            cc.FrameGroupAccumulator(value)



# ----------------------------------------------------------------------------
# Writer output, sidecars, and buffer ownership
# ----------------------------------------------------------------------------

class TestSeriesWriter:
    def test_complete_series_writes_one_single_page_float32_tiff_per_image(self, tmp_path):
        series = make_series(tmp_path, image_count=3, nframes=2)
        series.start()
        for value in (1, 3, 10, 20, 100, 300):
            assert series.add_frame(make_frame(value))

        assert series.wait(5)
        assert series.state == cc.STATE_COMPLETE
        for index, expected in enumerate((2.0, 15.0, 200.0), start=1):
            pages = read_tiff_pages(tmp_path / f"TEST_series_image{index}.tif")
            assert len(pages) == 1  # mean only; no variance page
            assert pages[0].dtype == np.float32
            assert pages[0].shape == (ROWS, WIDTH)
            np.testing.assert_array_equal(pages[0], np.full((ROWS, WIDTH), expected, dtype=np.float32))

    def test_sidecar_fields(self, tmp_path):
        series = make_series(
            tmp_path,
            image_count=1,
            nframes=2,
            metadata={"exposure_time": 30000, "analogue_gain": 1.0, "magnification": "40x", "z_height": 350},
        )
        series.start()
        series.add_frame(make_frame(1), timestamp_us=0)
        series.add_frame(make_frame(1), timestamp_us=33333)
        assert series.wait(5)

        sidecar = json.loads((tmp_path / "TEST_series_image1.json").read_text())
        assert sidecar["frames"] == 2
        assert sidecar["image_index"] == 1
        assert sidecar["image_count"] == 1
        assert sidecar["series_id"] == series.series_id
        assert sidecar["image_filename"] == "TEST_series_image1.tif"
        assert sidecar["sensor_timestamps_us"] == [0, 33333]
        assert sidecar["exposure_time"] == 30000
        assert sidecar["analogue_gain"] == 1.0
        assert sidecar["magnification"] == "40x"
        assert sidecar["z_height"] == 350
        assert sidecar["image_time"] >= 0
        assert sidecar["save_time"] >= 0
        assert "time" not in sidecar  # legacy field name is not reused
        for key in ("camera", "sensor", "lens_adapter", "light_source", "data_timestamp", "image_filepath"):
            assert key in sidecar

    def test_writes_outputs_in_sequence_order(self, tmp_path):
        blocker = BlockingTiffWriter()
        blocker.release.set()
        with patch.object(cc, "_write_tiff", blocker):
            series = make_series(tmp_path, image_count=3, nframes=1)
            series.start()
            for value in (1, 2, 3):
                series.add_frame(make_frame(value))
            assert series.wait(5)

        names = [path.rsplit("/", 1)[-1] for path in blocker.calls]
        assert names == ["TEST_series_image1.tif", "TEST_series_image2.tif", "TEST_series_image3.tif"]

    def test_queued_image_survives_accumulator_reuse(self, tmp_path):
        blocker = BlockingTiffWriter()
        with patch.object(cc, "_write_tiff", blocker):
            series = make_series(tmp_path, image_count=2, nframes=1)
            series.start()
            series.add_frame(make_frame(7))
            assert blocker.started.wait(5)  # writer holds image 1
            series.add_frame(make_frame(9))  # reuses the sum buffer while image 1 waits
            blocker.release.set()
            assert series.wait(5)

        assert series.state == cc.STATE_COMPLETE
        np.testing.assert_array_equal(
            read_tiff_pages(tmp_path / "TEST_series_image1.tif")[0],
            np.full((ROWS, WIDTH), 7, dtype=np.float32),
        )
        np.testing.assert_array_equal(
            read_tiff_pages(tmp_path / "TEST_series_image2.tif")[0],
            np.full((ROWS, WIDTH), 9, dtype=np.float32),
        )

    def test_capture_continues_while_writer_is_busy(self, tmp_path):
        blocker = BlockingTiffWriter()
        with patch.object(cc, "_write_tiff", blocker):
            series = make_series(tmp_path, image_count=3, nframes=1, queue_capacity=3)
            series.start()
            series.add_frame(make_frame(1))
            assert blocker.started.wait(5)
            # The writer is blocked on image 1, yet images 2 and 3 are accepted.
            assert series.add_frame(make_frame(2))
            assert series.add_frame(make_frame(3))
            snapshot = series.status_snapshot()
            assert snapshot["captured_images"] == 3
            assert snapshot["written_images"] == 0
            assert snapshot["state"] == cc.STATE_DRAINING
            blocker.release.set()
            assert series.wait(5)

        assert series.state == cc.STATE_COMPLETE

    def test_frames_after_completion_are_ignored(self, tmp_path):
        series = make_series(tmp_path, image_count=1, nframes=1)
        series.start()
        assert series.add_frame(make_frame(1))
        assert series.add_frame(make_frame(2)) is False
        assert series.wait(5)

        assert series.status_snapshot()["captured_images"] == 1
        assert not (tmp_path / "TEST_series_image2.tif").exists()

    def test_frames_before_start_are_ignored(self, tmp_path):
        series = make_series(tmp_path, image_count=1, nframes=1)
        assert series.add_frame(make_frame(1)) is False
        assert series.status_snapshot()["captured_images"] == 0



# ----------------------------------------------------------------------------
# Overload and write errors
# ----------------------------------------------------------------------------

class TestSeriesFailures:
    def test_queue_full_is_overloaded_and_never_complete(self, tmp_path):
        blocker = BlockingTiffWriter()
        with patch.object(cc, "_write_tiff", blocker):
            series = make_series(tmp_path, image_count=5, nframes=1, queue_capacity=1)
            series.start()
            series.add_frame(make_frame(1))  # taken by the writer, which then blocks
            assert blocker.started.wait(5)
            assert series.add_frame(make_frame(2))  # fills the 1-image queue
            assert series.add_frame(make_frame(3)) is False  # no room: overload

            snapshot = series.status_snapshot()
            assert snapshot["error"]["code"] == cc.ERROR_QUEUE_OVERLOADED
            assert snapshot["error"]["image_index"] == 3
            assert snapshot["captured_images"] == 2  # image 3 is not counted
            assert series.add_frame(make_frame(4)) is False  # capture has stopped

            blocker.release.set()
            assert series.wait(5)

        snapshot = series.status_snapshot()
        assert snapshot["state"] == cc.STATE_OVERLOADED
        assert snapshot["written_images"] == 2  # already-queued images are kept
        assert (tmp_path / "TEST_series_image2.tif").exists()
        assert not (tmp_path / "TEST_series_image3.tif").exists()

    def test_write_error_is_latched_failed_and_never_complete(self, tmp_path):
        real_write = cc._write_tiff
        calls = []

        def failing_write(path, image):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("No space left on device")
            real_write(path, image)

        with patch.object(cc, "_write_tiff", failing_write):
            series = make_series(tmp_path, image_count=3, nframes=1)
            series.start()
            for value in (1, 2, 3):
                series.add_frame(make_frame(value))
            assert series.wait(5)

        snapshot = series.status_snapshot()
        assert snapshot["state"] == cc.STATE_FAILED
        assert snapshot["error"]["code"] == cc.ERROR_WRITE_FAILED
        assert snapshot["error"]["image_index"] == 2
        assert snapshot["error"]["filename"] == "TEST_series_image2"
        assert "No space left on device" in snapshot["error"]["message"]
        assert snapshot["written_images"] == 1
        # The error stays latched on later polls.
        assert series.status_snapshot()["error"] == snapshot["error"]

    def test_json_write_error_is_failed(self, tmp_path):
        with patch.object(cc, "_write_json", side_effect=OSError("disk full")):
            series = make_series(tmp_path, image_count=1, nframes=1)
            series.start()
            series.add_frame(make_frame(1))
            assert series.wait(5)

        snapshot = series.status_snapshot()
        assert snapshot["state"] == cc.STATE_FAILED
        assert snapshot["error"]["code"] == cc.ERROR_WRITE_FAILED
        assert snapshot["written_images"] == 0

    def test_first_error_is_kept_and_not_hidden_by_cancel(self, tmp_path):
        with patch.object(cc, "_write_tiff", side_effect=OSError("first failure")):
            series = make_series(tmp_path, image_count=5, nframes=1)
            series.start()
            series.add_frame(make_frame(1))
            assert wait_until(lambda: series.status_snapshot()["error"] is not None)
            series.cancel()
            assert series.wait(5)

        snapshot = series.status_snapshot()
        assert snapshot["state"] == cc.STATE_FAILED
        assert "first failure" in snapshot["error"]["message"]

    def test_invalid_frame_fails_the_series(self, tmp_path):
        series = make_series(tmp_path, image_count=2, nframes=1)
        series.start()
        assert series.add_frame(np.zeros((ROWS, PADDED_WIDTH), dtype=np.uint32)) is False
        assert series.wait(5)

        snapshot = series.status_snapshot()
        assert snapshot["state"] == cc.STATE_FAILED
        assert snapshot["error"]["code"] == cc.ERROR_INVALID_FRAME



# ----------------------------------------------------------------------------
# Cancellation
# ----------------------------------------------------------------------------

class TestSeriesCancellation:
    def test_cancel_during_accumulation_discards_only_the_incomplete_group(self, tmp_path):
        series = make_series(tmp_path, image_count=3, nframes=3)
        series.start()
        for value in (1, 2, 3, 4, 5):  # one full group, plus 2 frames of the next
            series.add_frame(make_frame(value))
        series.cancel()
        assert series.wait(5)

        snapshot = series.status_snapshot()
        assert snapshot["state"] == cc.STATE_CANCELLED
        assert snapshot["captured_images"] == 1
        assert snapshot["written_images"] == 1
        assert snapshot["discarded_partial_frames"] == 2
        assert snapshot["error"] is None
        assert (tmp_path / "TEST_series_image1.tif").exists()
        assert not (tmp_path / "TEST_series_image2.tif").exists()

    def test_cancel_between_groups_keeps_completed_images(self, tmp_path):
        series = make_series(tmp_path, image_count=4, nframes=2)
        series.start()
        for value in (1, 1, 2, 2):
            series.add_frame(make_frame(value))
        series.cancel()
        assert series.wait(5)

        snapshot = series.status_snapshot()
        assert snapshot["state"] == cc.STATE_CANCELLED
        assert snapshot["written_images"] == 2
        assert snapshot["discarded_partial_frames"] == 0

    def test_cancel_after_all_captured_still_completes(self, tmp_path):
        blocker = BlockingTiffWriter()
        with patch.object(cc, "_write_tiff", blocker):
            series = make_series(tmp_path, image_count=2, nframes=1)
            series.start()
            series.add_frame(make_frame(1))
            series.add_frame(make_frame(2))
            assert blocker.started.wait(5)
            series.cancel()  # every image is already captured and queued
            blocker.release.set()
            assert series.wait(5)

        snapshot = series.status_snapshot()
        assert snapshot["state"] == cc.STATE_COMPLETE  # no data was lost
        assert snapshot["written_images"] == 2

    def test_cancel_before_start(self, tmp_path):
        series = make_series(tmp_path, image_count=2, nframes=1)
        series.cancel()

        assert series.state == cc.STATE_CANCELLED
        assert series.add_frame(make_frame(1)) is False
        with pytest.raises(RuntimeError):
            series.start()


# ----------------------------------------------------------------------------
# Status snapshot and configuration
# ----------------------------------------------------------------------------

class TestSeriesStatusAndConfig:
    def test_status_snapshot_is_json_serializable_and_complete(self, tmp_path):
        series = make_series(tmp_path, image_count=2, nframes=1)
        snapshot = series.status_snapshot()

        json.dumps(snapshot)
        for key in (
            "ok", "series_id", "state", "is_terminal", "requested_images",
            "captured_images", "written_images", "queue_depth", "queue_high_watermark",
            "queue_capacity", "error", "process_rss_bytes", "system_available_bytes",
        ):
            assert key in snapshot
        assert snapshot["state"] == cc.STATE_ACCEPTED

    def test_queue_high_watermark_is_tracked(self, tmp_path):
        blocker = BlockingTiffWriter()
        with patch.object(cc, "_write_tiff", blocker):
            series = make_series(tmp_path, image_count=3, nframes=1, queue_capacity=3)
            series.start()
            series.add_frame(make_frame(1))
            assert blocker.started.wait(5)
            series.add_frame(make_frame(2))
            series.add_frame(make_frame(3))
            assert series.status_snapshot()["queue_high_watermark"] == 2
            blocker.release.set()
            assert series.wait(5)

    def test_default_queue_capacity_is_3(self, tmp_path):
        assert cc.DEFAULT_QUEUE_CAPACITY == 3
        assert make_series(tmp_path).status_snapshot()["queue_capacity"] == 3

    def test_queue_capacity_is_configurable(self, tmp_path):
        assert make_series(tmp_path, queue_capacity=8).status_snapshot()["queue_capacity"] == 8

    @pytest.mark.parametrize("kwargs", [
        {"image_count": 0},
        {"nframes": 0},
        {"queue_capacity": 0},
        {"metadata": {"frames": 5}},
        {"metadata": {"time": 1.0}},
    ])
    def test_rejects_invalid_configuration(self, tmp_path, kwargs):
        with pytest.raises(ValueError):
            make_series(tmp_path, **kwargs)

    def test_rejects_missing_output_dir(self, tmp_path):
        with pytest.raises(ValueError):
            cc.ContinuousSeries(1, 1, str(tmp_path / "missing"), "TEST_series")

