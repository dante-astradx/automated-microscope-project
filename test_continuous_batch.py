"""Tests for repeated-series (batch) capture: core, camera service and client.

No camera hardware is used. Frames are synthetic and sensor timestamps are
supplied directly, so the camera-side inter-series pause can be tested
exactly. These tests cannot establish camera frame rates or storage
throughput.
"""

import json
import threading
import time

import numpy as np
import pytest
from unittest.mock import MagicMock, patch

import camera
import camera_zmq
import continuous_capture as cc

ROWS = 4
WIDTH = 6
PADDED_WIDTH = 8
FRAME_US = 100_000  # simulated 10 fps sensor cadence
CAMERA_METADATA = {"ExposureTime": 30000, "AnalogueGain": 1.5}


def quiet(msg):
    pass


def make_frame(value):
    return np.full((ROWS, PADDED_WIDTH), value, dtype=np.uint16)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def make_batch(tmp_path, image_count=2, series_count=3, pause_us=0, **kwargs):
    kwargs.setdefault("width", WIDTH)
    kwargs.setdefault("logger", quiet)
    # Frames are fed instantly here, faster than any writer, so use a queue
    # large enough for the whole batch unless a test is about the queue bound.
    kwargs.setdefault("queue_capacity", 50)
    return cc.ContinuousSeriesBatch(
        image_count=image_count,
        series_count=series_count,
        nframes=kwargs.pop("nframes", 1),
        output_dir=str(tmp_path),
        base_filename="TEST",
        inter_series_pause_us=pause_us,
        **kwargs,
    )


def feed(batch, count, start_us=0, value=1):
    """Feed ``count`` frames at FRAME_US spacing. Return the next timestamp."""
    ts = start_us
    for _ in range(count):
        batch.add_frame(make_frame(value), ts)
        ts += FRAME_US
    return ts


def sidecar(tmp_path, series_index, image_index):
    return json.loads((tmp_path / f"TEST_series{series_index}_image{image_index}.json").read_text())


class BlockingTiffWriter:
    """Replaces _write_tiff and blocks until released, to hold the writer busy."""

    def __init__(self):
        self.release = threading.Event()
        self._real = cc._write_tiff

    def __call__(self, path, image):
        assert self.release.wait(5), "writer was never released"
        self._real(path, image)


# ----------------------------------------------------------------------------
# Core: scheduling the inter-series pause
# ----------------------------------------------------------------------------

class TestBatchScheduling:
    def test_zero_pause_runs_series_back_to_back(self, tmp_path):
        batch = make_batch(tmp_path, image_count=2, series_count=3, pause_us=0)
        batch.start()
        feed(batch, 6)
        assert batch.wait(5)

        snap = batch.status_snapshot()
        assert snap["state"] == cc.STATE_COMPLETE
        assert snap["written_images"] == 6
        assert snap["frames_skipped_in_pauses"] == 0
        assert [b["observed_gap_us"] for b in snap["boundaries"]] == [FRAME_US, FRAME_US]

    def test_frames_before_the_pause_threshold_are_skipped(self, tmp_path):
        # Last frame of series 1 at 100 ms; 500 ms pause -> first acceptable
        # frame is at 600 ms. Frames at 200..500 ms (4 frames) are skipped.
        batch = make_batch(tmp_path, image_count=2, series_count=2, pause_us=500_000)
        batch.start()
        feed(batch, 8)
        assert batch.wait(5)

        snap = batch.status_snapshot()
        assert snap["state"] == cc.STATE_COMPLETE
        assert snap["frames_skipped_in_pauses"] == 4
        boundary = snap["boundaries"][0]
        assert boundary["before_series_index"] == 2
        assert boundary["previous_last_timestamp_us"] == 100_000
        assert boundary["first_timestamp_us"] == 600_000
        assert boundary["observed_gap_us"] == 500_000
        assert boundary["frames_skipped"] == 4
        assert sidecar(tmp_path, 2, 1)["sensor_timestamps_us"] == [600_000]
        assert sidecar(tmp_path, 2, 2)["sensor_timestamps_us"] == [700_000]

    def test_pause_is_a_minimum_rounded_up_to_the_frame_cadence(self, tmp_path):
        # 450 ms requested; frames every 100 ms -> first acceptable gap is 500 ms.
        batch = make_batch(tmp_path, image_count=1, series_count=2, pause_us=450_000)
        batch.start()
        feed(batch, 7)
        assert batch.wait(5)
        assert batch.status_snapshot()["boundaries"][0]["observed_gap_us"] == 500_000

    def test_each_series_has_correct_within_series_timestamps(self, tmp_path):
        batch = make_batch(tmp_path, image_count=3, series_count=3, pause_us=200_000)
        batch.start()
        feed(batch, 20)
        assert batch.wait(5)
        for series_index in (1, 2, 3):
            stamps = [sidecar(tmp_path, series_index, i)["sensor_timestamps_us"][0]
                      for i in (1, 2, 3)]
            assert np.diff(stamps).tolist() == [FRAME_US, FRAME_US]

    def test_uses_last_frame_of_a_multi_frame_group_for_the_gap(self, tmp_path):
        batch = make_batch(tmp_path, image_count=1, series_count=2, pause_us=300_000, nframes=3)
        batch.start()
        feed(batch, 9)
        assert batch.wait(5)
        boundary = batch.status_snapshot()["boundaries"][0]
        assert boundary["previous_last_timestamp_us"] == 200_000
        assert boundary["first_timestamp_us"] == 500_000
        assert sidecar(tmp_path, 2, 1)["sensor_timestamps_us"] == [500_000, 600_000, 700_000]

    def test_no_gap_after_the_last_series(self, tmp_path):
        batch = make_batch(tmp_path, image_count=1, series_count=2, pause_us=500_000)
        batch.start()
        feed(batch, 12)  # extra frames after the batch finished are ignored
        assert batch.wait(5)
        snap = batch.status_snapshot()
        assert snap["captured_images"] == 2
        assert len(snap["boundaries"]) == 1


# ----------------------------------------------------------------------------
# Core: naming and sidecar metadata
# ----------------------------------------------------------------------------

class TestBatchOutput:
    def test_filenames_are_unique_per_series(self, tmp_path):
        batch = make_batch(tmp_path, image_count=2, series_count=3)
        batch.start()
        feed(batch, 6)
        assert batch.wait(5)
        names = sorted(p.name for p in tmp_path.glob("*.tif"))
        assert names == sorted(
            f"TEST_series{s}_image{i}.tif" for s in (1, 2, 3) for i in (1, 2)
        )
        assert len(list(tmp_path.glob("*.json"))) == 6

    def test_sidecar_identifies_batch_and_series(self, tmp_path):
        batch = make_batch(tmp_path, image_count=2, series_count=2, pause_us=200_000,
                           metadata={"barcode": "B1", "exposure_time": 30000})
        batch.start()
        feed(batch, 6)
        assert batch.wait(5)

        first = sidecar(tmp_path, 1, 1)
        second_start = sidecar(tmp_path, 2, 1)
        second_end = sidecar(tmp_path, 2, 2)
        assert first["batch_id"] == batch.batch_id
        assert first["series_id"] == f"{batch.batch_id}-s1"
        assert second_start["series_id"] == f"{batch.batch_id}-s2"
        assert (first["series_index"], first["image_index"], first["image_count"]) == (1, 1, 2)
        assert (second_end["series_index"], second_end["image_index"]) == (2, 2)
        assert second_end["batch_image_index"] == 4
        assert first["series_count"] == 2
        assert first["inter_series_pause_us"] == 200_000
        assert first["inter_series_gap_us"] is None
        assert second_start["inter_series_gap_us"] == 200_000
        assert second_end["inter_series_gap_us"] is None
        assert first["frames"] == 1
        assert first["barcode"] == "B1"
        assert first["exposure_time"] == 30000
        assert second_start["image_filename"] == "TEST_series2_image1.tif"

    def test_single_series_output_is_unchanged(self, tmp_path):
        series = cc.ContinuousSeries(image_count=2, nframes=1, output_dir=str(tmp_path),
                                     base_filename="TEST", width=WIDTH, logger=quiet)
        series.start()
        series.add_frame(make_frame(1), 0)
        series.add_frame(make_frame(1), 100)
        assert series.wait(5)
        assert sorted(p.name for p in tmp_path.glob("*.tif")) == [
            "TEST_image1.tif", "TEST_image2.tif"]
        data = json.loads((tmp_path / "TEST_image1.json").read_text())
        assert "batch_id" not in data and "series_index" not in data
        assert "batch_id" not in series.status_snapshot()

    @pytest.mark.parametrize("kwargs", [
        {"series_count": 0},
        {"series_count": True},
        {"series_count": 1.5},
        {"image_count": 0},
        {"pause_us": -1},
        {"pause_us": 0.5},
        {"pause_us": True},
        {"metadata": {"series_index": 3}},
        {"metadata": {"batch_id": "x"}},
        {"metadata": {"frames": 5}},
    ])
    def test_rejects_invalid_configuration(self, tmp_path, kwargs):
        with pytest.raises(ValueError):
            make_batch(tmp_path, **kwargs)


# ----------------------------------------------------------------------------
# Core: shared queue, writer overlap, failures and cancellation
# ----------------------------------------------------------------------------

class TestBatchQueueAndFailures:
    def test_next_series_captures_while_writer_is_still_busy(self, tmp_path):
        blocker = BlockingTiffWriter()
        with patch.object(cc, "_write_tiff", blocker):
            batch = make_batch(tmp_path, image_count=1, series_count=3,
                               pause_us=0, queue_capacity=5)
            batch.start()
            feed(batch, 3)
            snap = batch.status_snapshot()
            # All three series captured before any image finished writing.
            assert snap["captured_images"] == 3
            assert snap["written_images"] == 0
            assert snap["completed_series"] == 3
            blocker.release.set()
            assert batch.wait(5)
        assert batch.status_snapshot()["state"] == cc.STATE_COMPLETE

    def test_one_queue_bounds_the_whole_batch(self, tmp_path):
        blocker = BlockingTiffWriter()
        with patch.object(cc, "_write_tiff", blocker):
            batch = make_batch(tmp_path, image_count=2, series_count=3, queue_capacity=2)
            batch.start()
            feed(batch, 1)
            # The writer takes image 1 off the queue and blocks on it.
            assert wait_until(lambda: batch.status_snapshot()["writing_image_index"] == 1)
            # Images 2 and 3 fill the queue; image 4 (series 2, image 2) overloads.
            feed(batch, 3, start_us=FRAME_US)
            snap = batch.status_snapshot()
            assert snap["error"]["code"] == cc.ERROR_QUEUE_OVERLOADED
            assert snap["error"]["image_index"] == 4
            assert snap["error"]["series_index"] == 2
            assert snap["error"]["series_image_index"] == 2
            blocker.release.set()
            assert batch.wait(5)
        final = batch.status_snapshot()
        assert final["state"] == cc.STATE_OVERLOADED
        assert final["captured_images"] == 3
        assert final["written_images"] == 3

    def test_write_failure_in_a_later_series_fails_the_batch(self, tmp_path):
        real = cc._write_tiff

        def fail_series_2(path, image):
            if "_series2_" in path:
                raise OSError("disk full")
            real(path, image)

        with patch.object(cc, "_write_tiff", fail_series_2):
            batch = make_batch(tmp_path, image_count=1, series_count=3)
            batch.start()
            feed(batch, 3)
            assert batch.wait(5)
        snap = batch.status_snapshot()
        assert snap["state"] == cc.STATE_FAILED
        assert snap["error"]["code"] == cc.ERROR_WRITE_FAILED
        assert snap["error"]["series_index"] == 2
        assert snap["written_images"] == 1

    def test_missing_timestamp_with_a_pause_fails_visibly(self, tmp_path):
        batch = make_batch(tmp_path, image_count=1, series_count=2, pause_us=100_000)
        batch.start()
        batch.add_frame(make_frame(1), 0)
        batch.add_frame(make_frame(1), None)
        assert batch.wait(5)
        snap = batch.status_snapshot()
        assert snap["state"] == cc.STATE_FAILED
        assert snap["error"]["code"] == cc.ERROR_MISSING_TIMESTAMP
        assert snap["error"]["series_index"] == 2

    def test_backwards_timestamp_at_a_boundary_fails_visibly(self, tmp_path):
        batch = make_batch(tmp_path, image_count=1, series_count=2, pause_us=100_000)
        batch.start()
        batch.add_frame(make_frame(1), 500_000)
        batch.add_frame(make_frame(1), 400_000)
        assert batch.wait(5)
        assert batch.status_snapshot()["error"]["code"] == cc.ERROR_INVALID_FRAME

    def test_cancel_during_a_pause_stops_later_series(self, tmp_path):
        batch = make_batch(tmp_path, image_count=1, series_count=3, pause_us=1_000_000)
        batch.start()
        feed(batch, 3)  # series 1 captured, then two frames skipped in the pause
        snap = batch.status_snapshot()
        assert snap["awaiting_next_series"] is True
        assert snap["frames_skipped_in_pauses"] == 2
        batch.cancel()
        feed(batch, 20, start_us=300_000)  # ignored after cancel
        assert batch.wait(5)
        final = batch.status_snapshot()
        assert final["state"] == cc.STATE_CANCELLED
        assert final["captured_images"] == 1
        assert final["written_images"] == 1
        assert final["completed_series"] == 1
        assert final["discarded_partial_frames"] == 0

    def test_status_reports_current_series_progress(self, tmp_path):
        batch = make_batch(tmp_path, image_count=3, series_count=2, pause_us=0)
        batch.start()
        feed(batch, 4)
        snap = batch.status_snapshot()
        assert snap["requested_images"] == 6
        assert snap["images_per_series"] == 3
        assert snap["current_series_index"] == 2
        assert snap["series_captured_images"] == 1
        assert snap["completed_series"] == 1
        assert snap["series_ids"] == [f"{batch.batch_id}-s1", f"{batch.batch_id}-s2"]
        json.dumps(snap)
        batch.cancel()
        assert batch.wait(5)


# ----------------------------------------------------------------------------
# Camera service: SeriesManager batch commands and the server loop
# ----------------------------------------------------------------------------

def batch_msg(tmp_path, **overrides):
    msg = {
        "command": "start_series_batch",
        "image_count": 2,
        "series_count": 2,
        "nframes": 1,
        "inter_series_pause_us": 0,
        "file_path": str(tmp_path),
        "filename": "TEST",
        "width": WIDTH,
        "queue_capacity": 50,
        "metadata": {"barcode": "TEST001"},
    }
    msg.update(overrides)
    return msg


def start_batch(manager, tmp_path, legacy_busy=False, **overrides):
    return manager.start_batch(batch_msg(tmp_path, **overrides), legacy_busy=legacy_busy,
                               camera_metadata=CAMERA_METADATA)


class TestSeriesManagerBatch:
    def test_batch_is_accepted_and_runs_to_completion(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        reply = start_batch(manager, tmp_path, inter_series_pause_us=200_000,
                            magnification=20, z_height=None)
        assert reply["ok"] is True
        batch_id = reply["batch_id"]
        assert reply["series_ids"] == [f"{batch_id}-s1", f"{batch_id}-s2"]
        assert reply["requested_images"] == 4
        assert reply["inter_series_pause_us"] == 200_000
        assert manager.is_active()

        for i in range(8):
            manager.on_frame(make_frame(1), i * FRAME_US)
        assert wait_until(lambda: manager.batch_status({"batch_id": batch_id})["is_terminal"])
        status = manager.batch_status({"batch_id": batch_id})
        assert status["state"] == cc.STATE_COMPLETE
        assert status["written_images"] == 4
        assert status["boundaries"][0]["observed_gap_us"] == 200_000

        data = sidecar(tmp_path, 2, 1)
        assert data["exposure_time"] == 30000
        assert data["analogue_gain"] == 1.5
        assert data["magnification"] == "20x"
        assert data["barcode"] == "TEST001"

    @pytest.mark.parametrize("overrides", [
        {"series_count": 0},
        {"inter_series_pause_us": -5},
        {"inter_series_pause_us": 0.5},
        {"image_count": 0},
        {"metadata": "not a dict"},
        {"metadata": {"series_index": 1}},
    ])
    def test_invalid_batch_is_rejected(self, tmp_path, overrides):
        manager = camera_zmq.SeriesManager(logger=quiet)
        reply = start_batch(manager, tmp_path, **overrides)
        assert reply["ok"] is False
        assert reply["error"]["code"] == camera_zmq.ERROR_INVALID_REQUEST
        assert not manager.is_active()

    @pytest.mark.parametrize("field", ["image_count", "series_count", "nframes",
                                       "file_path", "filename"])
    def test_missing_required_field_is_rejected(self, tmp_path, field):
        msg = batch_msg(tmp_path)
        del msg[field]
        reply = camera_zmq.SeriesManager(logger=quiet).start_batch(
            msg, legacy_busy=False, camera_metadata=CAMERA_METADATA)
        assert reply["ok"] is False
        assert field in reply["error"]["message"]

    def test_batch_busy_rules(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        assert start_batch(manager, tmp_path, legacy_busy=True)["error"]["code"] == camera_zmq.ERROR_BUSY
        first = start_batch(manager, tmp_path)
        assert first["ok"] is True
        # Neither another batch nor a single series may start while it runs.
        assert start_batch(manager, tmp_path)["error"]["code"] == camera_zmq.ERROR_BUSY
        single = manager.start({"image_count": 1, "nframes": 1, "file_path": str(tmp_path),
                                "filename": "S", "width": WIDTH},
                               legacy_busy=False, camera_metadata=CAMERA_METADATA)
        assert single["error"]["code"] == camera_zmq.ERROR_BUSY
        manager.shutdown()

    def test_cancel_batch_and_unknown_ids(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        batch_id = start_batch(manager, tmp_path)["batch_id"]
        assert manager.batch_status({"batch_id": "nope"})["error"]["code"] == camera_zmq.ERROR_UNKNOWN_SERIES
        assert manager.batch_status({})["error"]["code"] == camera_zmq.ERROR_INVALID_REQUEST
        assert manager.cancel_batch({"batch_id": "nope"})["error"]["code"] == camera_zmq.ERROR_UNKNOWN_SERIES
        reply = manager.cancel_batch({"batch_id": batch_id})
        assert reply == {"ok": True, "batch_id": batch_id, "state": reply["state"]}
        assert wait_until(lambda: manager.batch_status({"batch_id": batch_id})["is_terminal"])
        assert manager.batch_status({"batch_id": batch_id})["state"] == cc.STATE_CANCELLED

    def test_batch_commands_do_not_find_a_single_series(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        series_id = manager.start({"image_count": 1, "nframes": 1, "file_path": str(tmp_path),
                                   "filename": "S", "width": WIDTH},
                                  legacy_busy=False, camera_metadata=CAMERA_METADATA)["series_id"]
        reply = manager.batch_status({"batch_id": series_id})
        assert reply["error"]["code"] == camera_zmq.ERROR_UNKNOWN_SERIES
        manager.shutdown()

    def test_callback_error_during_a_batch_is_reported(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        batch_id = start_batch(manager, tmp_path)["batch_id"]
        with patch.object(cc.ContinuousSeriesBatch, "add_frame", side_effect=RuntimeError("boom")):
            manager.on_frame(make_frame(1), 0)  # must not raise into the callback
        assert wait_until(lambda: manager.batch_status({"batch_id": batch_id})["is_terminal"])
        status = manager.batch_status({"batch_id": batch_id})
        assert status["state"] == cc.STATE_FAILED
        assert status["error"]["code"] == camera_zmq.ERROR_CALLBACK


class TestServerLoopBatch:
    """Drive the real camera_zmq() loop with the fake socket/camera harness."""

    def test_batch_commands_each_get_exactly_one_reply(self, tmp_path):
        from test_camera_series import run_server

        socket, _, _ = run_server([
            batch_msg(tmp_path),
            lambda replies: {"command": "series_batch_status", "batch_id": replies[0]["batch_id"]},
            lambda replies: {"command": "cancel_series_batch", "batch_id": replies[0]["batch_id"]},
            {"command": "series_batch_status", "batch_id": "nope"},
            {"command": "series_batch_status"},
        ])
        # four scripted requests plus the harness's final stop
        assert len(socket.replies) == len(socket.requests) == 6
        started, status, cancelled, unknown, missing, _ = socket.replies
        assert started["ok"] is True
        assert status["batch_id"] == started["batch_id"]
        assert status["requested_images"] == 4
        assert cancelled["ok"] is True
        assert unknown["error"]["code"] == camera_zmq.ERROR_UNKNOWN_SERIES
        assert missing["error"]["code"] == camera_zmq.ERROR_INVALID_REQUEST

    def test_end_to_end_batch_through_the_server(self, tmp_path):
        from test_camera_series import run_server, frame_bytes

        state = {}

        def frames_then_status(replies):
            for i in range(8):
                state["output"].outputframe(frame_bytes(i + 1), timestamp=i * FRAME_US)
            assert wait_until(lambda: not state["output"].series_manager.is_active())
            return {"command": "series_batch_status", "batch_id": replies[0]["batch_id"]}

        socket, _, _ = run_server(
            [batch_msg(tmp_path, inter_series_pause_us=300_000), frames_then_status],
            output_hook=lambda output: state.update(output=output),
        )
        status = socket.replies[1]
        assert status["state"] == cc.STATE_COMPLETE
        assert status["written_images"] == 4
        # Series 1 = frames at 0 and 100 ms. 300 ms pause -> series 2 starts at 400 ms.
        assert status["boundaries"][0]["first_timestamp_us"] == 400_000
        assert status["frames_skipped_in_pauses"] == 2
        assert sidecar(tmp_path, 2, 2)["sensor_timestamps_us"] == [500_000]

    def test_single_series_commands_still_work_unchanged(self, tmp_path):
        from test_camera_series import run_server, start_msg

        socket, _, _ = run_server([
            start_msg(tmp_path, image_count=1),
            lambda replies: {"command": "series_status", "series_id": replies[0]["series_id"]},
        ])
        started, status, _ = socket.replies
        assert started["ok"] is True and "batch_id" not in started
        assert status["series_id"] == started["series_id"]
        assert "batch_id" not in status


# ----------------------------------------------------------------------------
# Camera client
# ----------------------------------------------------------------------------

@pytest.fixture
def client():
    with patch.object(camera.Camera, "start_zmq"):
        instance = camera.Camera()
    instance.zmqSocket = MagicMock()
    instance.send_command = MagicMock(return_value={"ok": True})
    return instance


class TestCameraClientBatch:
    def test_start_series_batch_payload(self, client):
        client.start_series_batch(5, 3, 1, "NAME", "/data", inter_series_pause_us=500_000,
                                  z_height=None, magnification=20, metadata={"barcode": "X"})
        assert client.send_command.call_args.args[0] == {
            "command": "start_series_batch", "image_count": 5, "series_count": 3,
            "nframes": 1, "filename": "NAME", "file_path": "/data",
            "inter_series_pause_us": 500_000, "z_height": None, "magnification": 20,
            "metadata": {"barcode": "X"},
        }

    def test_optional_values_are_omitted_unless_set(self, client):
        client.start_series_batch(1, 2, 1, "NAME", "/data")
        msg = client.send_command.call_args.args[0]
        assert "queue_capacity" not in msg and "width" not in msg
        assert msg["inter_series_pause_us"] == 0
        client.start_series_batch(1, 2, 1, "NAME", "/data", queue_capacity=4, width=4056)
        msg = client.send_command.call_args.args[0]
        assert msg["queue_capacity"] == 4 and msg["width"] == 4056

    def test_status_and_cancel_payloads(self, client):
        client.series_batch_status("b1")
        assert client.send_command.call_args.args[0] == {
            "command": "series_batch_status", "batch_id": "b1"}
        client.cancel_series_batch("b1")
        assert client.send_command.call_args.args[0] == {
            "command": "cancel_series_batch", "batch_id": "b1"}

    def test_reconnects_when_the_socket_was_dropped(self, client):
        client.zmqSocket = None
        with patch.object(client, "start_zmq") as start_zmq:
            client.series_batch_status("b1")
        start_zmq.assert_called_once()
