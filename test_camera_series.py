"""Tests for the continuous-series ZeroMQ integration (camera_zmq.py, camera.py).

No camera hardware is used. The camera service loop is driven with a fake
Picamera2 and a fake ZeroMQ socket, so every command's replies can be counted.
These tests cannot establish camera frame rates or storage throughput.
"""

import json
import threading
import time

import numpy as np
import pytest
import tifffile
from unittest.mock import MagicMock, patch

import camera
import camera_zmq
import continuous_capture as cc

ROWS = 4
WIDTH = 6
PADDED_WIDTH = 8
CAMERA_METADATA = {"ExposureTime": 30000, "AnalogueGain": 1.5}


def quiet(msg):
    pass


def make_frame(value, rows=ROWS, padded_width=PADDED_WIDTH):
    return np.full((rows, padded_width), value, dtype=np.uint16)


def frame_bytes(value):
    """Raw callback bytes for a padded frame, as the encoder delivers them."""
    return make_frame(value).tobytes()


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def start_msg(tmp_path, **overrides):
    msg = {
        "command": "start_series",
        "image_count": 2,
        "nframes": 1,
        "file_path": str(tmp_path),
        "filename": "TEST_series",
        "width": WIDTH,
        "metadata": {"barcode": "TEST001"},
    }
    msg.update(overrides)
    return msg


def make_output():
    """An OutputZMQ at test size: ROWS rows, WIDTH active columns."""
    return camera_zmq.OutputZMQ(size=(WIDTH, ROWS), verbose=False)


def start(manager, tmp_path, legacy_busy=False, **overrides):
    return manager.start(start_msg(tmp_path, **overrides), legacy_busy=legacy_busy,
                         camera_metadata=CAMERA_METADATA)


# ----------------------------------------------------------------------------
# SeriesManager: starting a series
# ----------------------------------------------------------------------------

class TestSeriesManagerStart:
    def test_valid_request_is_accepted_promptly(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        started = time.monotonic()
        reply = start(manager, tmp_path)
        elapsed = time.monotonic() - started

        assert reply["ok"] is True
        assert reply["series_id"]
        assert reply["state"] == cc.STATE_CAPTURING
        assert reply["requested_images"] == 2
        assert reply["queue_capacity"] == cc.DEFAULT_QUEUE_CAPACITY
        assert elapsed < 1.0  # returns without waiting for capture or writes
        assert manager.is_active()
        manager.shutdown()

    def test_records_camera_exposure_gain_and_request_fields(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        reply = start(manager, tmp_path, image_count=1, z_height=350, magnification=40)
        manager.on_frame(make_frame(9), 1234)
        assert wait_until(lambda: manager.status({"series_id": reply["series_id"]})["is_terminal"])

        sidecar = json.loads((tmp_path / "TEST_series_image1.json").read_text())
        assert sidecar["exposure_time"] == 30000
        assert sidecar["analogue_gain"] == 1.5
        assert sidecar["magnification"] == "40x"
        assert sidecar["z_height"] == 350
        assert sidecar["barcode"] == "TEST001"
        assert sidecar["sensor_timestamps_us"] == [1234]

    def test_queue_capacity_is_passed_through(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        assert start(manager, tmp_path, queue_capacity=5)["queue_capacity"] == 5
        manager.shutdown()

    @pytest.mark.parametrize("overrides", [
        {"image_count": 0},
        {"nframes": -1},
        {"nframes": "ten"},
        {"queue_capacity": 0},
        {"width": 0},
        {"filename": ""},
        {"metadata": "not a dict"},
        {"metadata": {"frames": 5}},
    ])
    def test_invalid_parameters_are_rejected(self, tmp_path, overrides):
        manager = camera_zmq.SeriesManager(logger=quiet)
        reply = start(manager, tmp_path, **overrides)

        assert reply["ok"] is False
        assert reply["error"]["code"] == camera_zmq.ERROR_INVALID_REQUEST
        assert not manager.is_active()

    @pytest.mark.parametrize("field", ["image_count", "nframes", "file_path", "filename"])
    def test_missing_required_field_is_rejected(self, tmp_path, field):
        msg = start_msg(tmp_path)
        del msg[field]
        reply = camera_zmq.SeriesManager(logger=quiet).start(
            msg, legacy_busy=False, camera_metadata=CAMERA_METADATA)

        assert reply["ok"] is False
        assert field in reply["error"]["message"]

    def test_missing_output_dir_is_rejected(self, tmp_path):
        reply = start(camera_zmq.SeriesManager(logger=quiet), tmp_path,
                      file_path=str(tmp_path / "missing"))
        assert reply["error"]["code"] == camera_zmq.ERROR_INVALID_REQUEST

    def test_rejected_while_legacy_capture_is_busy(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        reply = start(manager, tmp_path, legacy_busy=True)

        assert reply["ok"] is False
        assert reply["error"]["code"] == camera_zmq.ERROR_BUSY
        assert not manager.is_active()

    def test_second_series_rejected_while_first_is_active(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        first = start(manager, tmp_path)
        second = start(manager, tmp_path)

        assert first["ok"] is True
        assert second["ok"] is False
        assert second["error"]["code"] == camera_zmq.ERROR_BUSY
        manager.shutdown()



# ----------------------------------------------------------------------------
# SeriesManager: status and frame forwarding
# ----------------------------------------------------------------------------

class TestSeriesManagerStatus:
    def test_status_returns_the_series_status_snapshot(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        reply = start(manager, tmp_path, image_count=2)
        manager.on_frame(make_frame(1), 0)
        manager.on_frame(make_frame(2), 100)
        assert wait_until(lambda: manager.status({"series_id": reply["series_id"]})["is_terminal"])

        status = manager.status({"series_id": reply["series_id"]})
        json.dumps(status)
        assert status["ok"] is True
        assert status["state"] == cc.STATE_COMPLETE
        assert status["captured_images"] == 2
        assert status["written_images"] == 2
        for key in ("queue_depth", "queue_high_watermark", "process_rss_bytes",
                    "system_available_bytes", "error"):
            assert key in status

    def test_status_for_unknown_or_missing_series_id(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        start(manager, tmp_path)

        unknown = manager.status({"series_id": "nope"})
        assert unknown["ok"] is False
        assert unknown["error"]["code"] == camera_zmq.ERROR_UNKNOWN_SERIES
        assert manager.status({})["error"]["code"] == camera_zmq.ERROR_INVALID_REQUEST
        manager.shutdown()

    def test_status_before_any_series_is_unknown(self):
        reply = camera_zmq.SeriesManager(logger=quiet).status({"series_id": "abc"})
        assert reply["error"]["code"] == camera_zmq.ERROR_UNKNOWN_SERIES

    def test_on_frame_passes_the_exact_frame_and_timestamp(self):
        manager = camera_zmq.SeriesManager(logger=quiet)
        series = MagicMock()
        manager._series = series
        frame = make_frame(7)
        manager.on_frame(frame, 4321)

        series.add_frame.assert_called_once()
        assert series.add_frame.call_args.args[0] is frame
        assert series.add_frame.call_args.args[1] == 4321

    def test_on_frame_without_a_series_does_nothing(self):
        camera_zmq.SeriesManager(logger=quiet).on_frame(make_frame(1), 0)  # must not raise



# ----------------------------------------------------------------------------
# SeriesManager: failures and cancellation
# ----------------------------------------------------------------------------

class TestSeriesManagerFailures:
    def test_writer_failure_is_reported_not_success(self, tmp_path):
        with patch.object(cc, "_write_tiff", side_effect=OSError("No space left on device")):
            manager = camera_zmq.SeriesManager(logger=quiet)
            reply = start(manager, tmp_path, image_count=2)
            manager.on_frame(make_frame(1), 0)
            assert wait_until(lambda: manager.status({"series_id": reply["series_id"]})["is_terminal"])

        status = manager.status({"series_id": reply["series_id"]})
        assert status["state"] == cc.STATE_FAILED
        assert status["error"]["code"] == cc.ERROR_WRITE_FAILED
        assert "No space left on device" in status["error"]["message"]
        # Still latched on a later poll, and a failed series does not block a new one.
        assert manager.status({"series_id": reply["series_id"]})["error"] == status["error"]
        assert not manager.is_active()

    def test_overload_is_reported_not_success(self, tmp_path):
        release = threading.Event()
        real_write = cc._write_tiff

        def slow_write(path, image):
            release.wait(5)
            real_write(path, image)

        with patch.object(cc, "_write_tiff", slow_write):
            manager = camera_zmq.SeriesManager(logger=quiet)
            series_id = start(manager, tmp_path, image_count=5, queue_capacity=1)["series_id"]
            for value in range(1, 6):
                manager.on_frame(make_frame(value), value)
            release.set()
            assert wait_until(lambda: manager.status({"series_id": series_id})["is_terminal"])

        status = manager.status({"series_id": series_id})
        assert status["state"] == cc.STATE_OVERLOADED
        assert status["error"]["code"] == cc.ERROR_QUEUE_OVERLOADED
        assert status["written_images"] < 5

    def test_unexpected_frame_exception_is_reported_as_failure(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        series_id = start(manager, tmp_path, image_count=2)["series_id"]
        with patch.object(manager._series, "add_frame", side_effect=RuntimeError("boom")):
            manager.on_frame(make_frame(1), 0)  # must not raise into the callback
        assert wait_until(lambda: manager.status({"series_id": series_id})["is_terminal"])

        status = manager.status({"series_id": series_id})
        assert status["state"] == cc.STATE_FAILED
        assert status["error"]["code"] == camera_zmq.ERROR_CALLBACK

    def test_cancel_then_a_new_series_can_start(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        first = start(manager, tmp_path, image_count=3)
        assert manager.cancel({"series_id": first["series_id"]})["ok"] is True
        assert wait_until(lambda: not manager.is_active())
        assert manager.status({"series_id": first["series_id"]})["state"] == cc.STATE_CANCELLED

        second = start(manager, tmp_path, filename="TEST_second")
        assert second["ok"] is True
        assert second["series_id"] != first["series_id"]
        # Only the most recent series is kept.
        old = manager.status({"series_id": first["series_id"]})
        assert old["error"]["code"] == camera_zmq.ERROR_UNKNOWN_SERIES
        manager.shutdown()

    def test_cancel_unknown_series(self):
        reply = camera_zmq.SeriesManager(logger=quiet).cancel({"series_id": "nope"})
        assert reply["error"]["code"] == camera_zmq.ERROR_UNKNOWN_SERIES

    def test_completed_series_does_not_block_a_new_one(self, tmp_path):
        manager = camera_zmq.SeriesManager(logger=quiet)
        first = start(manager, tmp_path, image_count=1)
        manager.on_frame(make_frame(1), 0)
        assert wait_until(lambda: manager.status({"series_id": first["series_id"]})["is_terminal"])

        assert start(manager, tmp_path, filename="TEST_second")["ok"] is True
        manager.shutdown()



# ----------------------------------------------------------------------------
# OutputZMQ encoder callback
# ----------------------------------------------------------------------------

class TestOutputFrameCallback:
    def test_callback_forwards_the_decoded_frame_and_timestamp(self):
        output = make_output()
        output.series_manager = MagicMock()
        output.outputframe(frame_bytes(5), timestamp=777)

        output.series_manager.on_frame.assert_called_once()
        forwarded, timestamp = output.series_manager.on_frame.call_args.args
        np.testing.assert_array_equal(forwarded, make_frame(5))
        assert timestamp == 777

    def test_callback_without_series_manager_is_unchanged(self):
        output = make_output()  # series_manager defaults to None
        output.outputframe(frame_bytes(3), timestamp=1)

        assert output.cnt == 1
        np.testing.assert_array_equal(output.im, make_frame(3))

    def test_no_active_series_does_not_divert_legacy_accumulation(self, tmp_path):
        output = make_output()
        output.series_manager = camera_zmq.SeriesManager(logger=quiet)  # idle
        output.accumulate(2, "LEGACY", str(tmp_path), exposure_time=11000, analogue_gain=1.0)
        for value in (2, 4):
            output.outputframe(frame_bytes(value), timestamp=0)
        assert output.accN == 2
        output.outputframe(frame_bytes(0), timestamp=0)  # triggers the legacy save

        with tifffile.TiffFile(tmp_path / "LEGACY.tif") as tif:
            pages = [p.asarray() for p in tif.pages]
        assert len(pages) == 2  # legacy mean + variance, unchanged
        assert pages[0].shape == (ROWS, PADDED_WIDTH)  # legacy keeps the padding
        np.testing.assert_array_equal(pages[0], np.full((ROWS, PADDED_WIDTH), 3, dtype=np.float32))

    def test_series_frames_reach_the_series_through_the_callback(self, tmp_path):
        output = make_output()
        manager = camera_zmq.SeriesManager(logger=quiet)
        output.series_manager = manager
        series_id = start(manager, tmp_path, image_count=1, nframes=2)["series_id"]
        output.outputframe(frame_bytes(10), timestamp=0)
        output.outputframe(frame_bytes(20), timestamp=33333)
        assert wait_until(lambda: manager.status({"series_id": series_id})["is_terminal"])

        assert manager.status({"series_id": series_id})["state"] == cc.STATE_COMPLETE
        with tifffile.TiffFile(tmp_path / "TEST_series_image1.tif") as tif:
            pages = [p.asarray() for p in tif.pages]
        assert len(pages) == 1
        np.testing.assert_array_equal(pages[0], np.full((ROWS, WIDTH), 15, dtype=np.float32))
        sidecar = json.loads((tmp_path / "TEST_series_image1.json").read_text())
        assert sidecar["sensor_timestamps_us"] == [0, 33333]


# ----------------------------------------------------------------------------
# camera.py client methods
# ----------------------------------------------------------------------------

@pytest.fixture
def client():
    with patch.object(camera.Camera, "start_zmq"):
        cam = camera.Camera()
    cam.send_command = MagicMock(return_value={"ok": True})
    cam.zmqSocket = object()  # pretend a socket is connected
    return cam


class TestCameraClient:
    def test_start_series_sends_the_expected_payload(self, client):
        client.start_series(3, 2, "NAME", "/data", z_height=350, magnification=40,
                            metadata={"barcode": "X"})
        msg = client.send_command.call_args.args[0]

        assert msg == {
            "command": "start_series", "image_count": 3, "nframes": 2,
            "filename": "NAME", "file_path": "/data", "z_height": 350,
            "magnification": 40, "metadata": {"barcode": "X"},
        }

    def test_start_series_omits_optional_values_so_service_defaults_apply(self, client):
        client.start_series(1, 1, "NAME", "/data")
        msg = client.send_command.call_args.args[0]

        assert "queue_capacity" not in msg
        assert "width" not in msg
        assert msg["metadata"] == {}

    def test_start_series_passes_queue_capacity_and_width(self, client):
        client.start_series(1, 1, "NAME", "/data", queue_capacity=5, width=4056)
        msg = client.send_command.call_args.args[0]
        assert msg["queue_capacity"] == 5
        assert msg["width"] == 4056

    def test_status_and_cancel_payloads(self, client):
        client.series_status("abc")
        assert client.send_command.call_args.args[0] == {"command": "series_status", "series_id": "abc"}
        client.cancel_series("abc")
        assert client.send_command.call_args.args[0] == {"command": "cancel_series", "series_id": "abc"}

    def test_reconnects_after_a_timeout_dropped_the_socket(self, client):
        client.zmqSocket = None
        with patch.object(client, "start_zmq") as start_zmq:
            client.series_status("abc")
        start_zmq.assert_called_once()

    def test_legacy_take_rpi_image_payload_is_unchanged(self, client):
        client.take_rpi_image(100, "IMG", "/data", z_height=300, magnification=40)
        assert client.send_command.call_args.args[0] == {
            "command": "accumulate", "nframes": 100, "filename": "IMG", "file_path": "/data",
            "z_height": 300, "magnification": 40,
        }



# ----------------------------------------------------------------------------
# camera_zmq() server loop, run with a fake camera and a fake ZeroMQ socket
# ----------------------------------------------------------------------------

class FakeSocket:
    """A REP socket stand-in: serves scripted requests and records every reply.

    Each script step is a request dict, or a callable taking the replies so far
    (to build a request from an earlier reply, such as a series_id). When the
    script is exhausted it sends "stop" so the server loop exits.
    """

    def __init__(self, script):
        self.script = list(script) + [{"command": "stop"}]
        self.requests = []
        self.replies = []
        self._awaiting_reply = False

    def bind(self, address):
        pass

    def poll(self, timeout, flags):
        return 1 if self.script else 0

    def recv(self):
        assert not self._awaiting_reply, "request received before previous reply was sent"
        step = self.script.pop(0)
        msg = step(self.replies) if callable(step) else step
        self.requests.append(msg)
        self._awaiting_reply = True
        return json.dumps(msg).encode()

    def send_string(self, text):
        assert self._awaiting_reply, "reply sent without a request"
        self.replies.append(json.loads(text))
        self._awaiting_reply = False


class FakePicamera2:
    def __init__(self):
        self.controls_set = []
        self.output = None

    def create_video_configuration(self, **kwargs):
        return kwargs

    def configure(self, config):
        pass

    def set_controls(self, controls):
        self.controls_set.append(controls)

    def start(self):
        pass

    def capture_metadata(self):
        return dict(CAMERA_METADATA)

    def start_encoder(self, encoder, output):
        self.output = output

    def stop_encoder(self):
        pass

    def stop(self):
        pass


def run_server(script, output_hook=None):
    """Run camera_zmq() against scripted requests. Return (socket, picam, output)."""
    socket = FakeSocket(script)
    picam = FakePicamera2()
    context = MagicMock()
    context.socket.return_value = socket

    original_start_encoder = picam.start_encoder

    def start_encoder(encoder, output):
        original_start_encoder(encoder, output)
        if output_hook is not None:
            output_hook(output)

    picam.start_encoder = start_encoder

    real_output_class = camera_zmq.OutputZMQ  # captured before it is patched below

    def make_output(size=None, verbose=None):
        return real_output_class(size=(WIDTH, ROWS), verbose=verbose)

    with patch.object(camera_zmq.zmq, "Context", return_value=context), \
         patch.object(camera_zmq, "Picamera2", return_value=picam), \
         patch.object(camera_zmq, "OutputZMQ", side_effect=make_output), \
         patch.object(camera_zmq, "running", True), \
         patch.object(camera_zmq.time, "sleep"), \
         patch("builtins.print"):
        camera_zmq.camera_zmq(verbose=False)

    return socket, picam, picam.output



def feed_frames(output, values, timestamp_start=0):
    for offset, value in enumerate(values):
        output.outputframe(frame_bytes(value), timestamp=timestamp_start + offset)


class TestServerLoopSeries:
    def test_start_status_cancel_each_get_exactly_one_reply(self, tmp_path):
        socket, _, _ = run_server([
            start_msg(tmp_path, image_count=3),
            lambda replies: {"command": "series_status", "series_id": replies[0]["series_id"]},
            lambda replies: {"command": "cancel_series", "series_id": replies[0]["series_id"]},
        ])

        # start, status, cancel, and the final stop
        assert len(socket.replies) == len(socket.requests) == 4
        started, status, cancelled, _ = socket.replies
        assert started["ok"] is True
        assert status["series_id"] == started["series_id"]
        assert status["requested_images"] == 3
        assert cancelled["ok"] is True

    def test_malformed_requests_still_get_one_reply(self, tmp_path):
        socket, _, _ = run_server([
            {"command": "start_series", "nframes": 1},  # missing fields
            {"command": "series_status"},  # missing series_id
            {"command": "series_status", "series_id": "nope"},
            {"command": "cancel_series", "series_id": "nope"},
            {"no_command_key": True},
        ])

        assert len(socket.replies) == len(socket.requests)
        assert socket.replies[0]["error"]["code"] == camera_zmq.ERROR_INVALID_REQUEST
        assert socket.replies[1]["error"]["code"] == camera_zmq.ERROR_INVALID_REQUEST
        assert socket.replies[2]["error"]["code"] == camera_zmq.ERROR_UNKNOWN_SERIES
        assert socket.replies[3]["error"]["code"] == camera_zmq.ERROR_UNKNOWN_SERIES
        assert "key error in" in socket.replies[4]  # existing legacy handling

    def test_series_rejected_while_legacy_accumulate_runs(self, tmp_path):
        socket, _, output = run_server([
            {"command": "accumulate", "nframes": 5, "filename": "L", "file_path": str(tmp_path)},
            start_msg(tmp_path),
        ])

        assert socket.replies[0] == {"accumulating": "started"}
        assert socket.replies[1]["ok"] is False
        assert socket.replies[1]["error"]["code"] == camera_zmq.ERROR_BUSY
        assert output.series_manager.is_active() is False

    def test_legacy_accumulate_rejected_while_series_runs(self, tmp_path):
        socket, _, output = run_server([
            start_msg(tmp_path, image_count=3),
            {"command": "accumulate", "nframes": 5, "filename": "L", "file_path": str(tmp_path)},
        ])

        assert socket.replies[0]["ok"] is True
        assert socket.replies[1] == {"accumulating": "series running"}
        assert output.accT is None  # the legacy accumulator was not started

    def test_unexpected_series_command_error_still_replies(self, tmp_path):
        with patch.object(camera_zmq, "handle_series_command", side_effect=RuntimeError("boom")):
            socket, _, _ = run_server([start_msg(tmp_path)])

        assert len(socket.replies) == len(socket.requests)
        assert socket.replies[0]["ok"] is False
        assert socket.replies[0]["error"]["code"] == camera_zmq.ERROR_SERVER

    def test_end_to_end_series_through_the_server(self, tmp_path):
        state = {}

        def frames_then_status(replies):
            feed_frames(state["output"], [10, 20, 30, 40])
            assert wait_until(lambda: not state["output"].series_manager.is_active())
            return {"command": "series_status", "series_id": replies[0]["series_id"]}

        socket, _, _ = run_server(
            [start_msg(tmp_path, image_count=2, nframes=2), frames_then_status],
            output_hook=lambda output: state.update(output=output),
        )

        status = socket.replies[1]
        assert status["state"] == cc.STATE_COMPLETE
        assert status["written_images"] == 2
        for index, expected in ((1, 15), (2, 35)):
            with tifffile.TiffFile(tmp_path / f"TEST_series_image{index}.tif") as tif:
                assert len(tif.pages) == 1
                np.testing.assert_array_equal(
                    tif.pages[0].asarray(), np.full((ROWS, WIDTH), expected, dtype=np.float32))



class TestServerLoopLegacyCommands:
    def test_legacy_accumulate_calls_the_accumulator_with_the_same_values(self, tmp_path):
        calls = []

        def hook(output):
            real = output.accumulate

            def recording(*args, **kwargs):
                calls.append((args, kwargs))
                return real(*args, **kwargs)

            output.accumulate = recording

        socket, _, _ = run_server([
            {"command": "accumulate", "nframes": 7, "filename": "IMG", "file_path": str(tmp_path),
             "z_height": 300, "magnification": 40},
            {"command": "accumulate", "nframes": 7, "filename": "IMG", "file_path": str(tmp_path)},
        ], output_hook=hook)

        assert socket.replies[0] == {"accumulating": "started"}
        assert socket.replies[1] == {"accumulating": "already running"}
        assert calls == [((7,), {
            "filename_base": "IMG", "folder_path": str(tmp_path),
            "exposure_time": 30000, "analogue_gain": 1.5,
            "z_height": 300, "magnification": 40,
        })]

    def test_preview_focus_exposure_and_status_commands_still_reply(self, tmp_path):
        tiny = np.zeros((2, 2), dtype=np.uint8)

        def hook(output):
            output.im = make_frame(1)
            output.get_centered_im = lambda: tiny
            output.get_reduced_im = lambda: tiny

        with patch.object(camera_zmq.a, "get_focus_score", return_value="1.0"):
            socket, picam, _ = run_server([
                {"command": "centered"},
                {"command": "getFocus"},
                {"command": "reduced"},
                {"command": "exposureTime", "exposureTime": 25000},
                {"command": "status"},
                {"command": "is_save_complete"},
                {"command": "get_image_path"},
            ], output_hook=hook)

        assert len(socket.replies) == len(socket.requests) == 8
        assert "image" in socket.replies[0] and "focus_score" in socket.replies[0]
        assert socket.replies[1] == {"focus_score": "1.0"}
        assert "image" in socket.replies[2]
        assert socket.replies[3] == {"handled": "exposureTime"}
        assert {"ExposureTime": 25000} in picam.controls_set
        assert socket.replies[4]["accumulating"] is False
        assert socket.replies[5] == {"save_complete": True}
        assert socket.replies[6]["handled"] == "get_image_path"

