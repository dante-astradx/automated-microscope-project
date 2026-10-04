"""Tests for frame-delivery diagnostics (frame_diagnostics.py and its camera_zmq wiring).

No camera hardware is used. These tests check the bookkeeping only; whether
frames are actually dropped can only be measured on the Pi.
"""

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import camera
import camera_zmq
import frame_diagnostics as fd
from test_camera_series import frame_bytes, make_output, quiet, run_server, start_msg

PERIOD_US = 100_000  # 10 fps


def rec(seq, ts, mode=fd.MODE_IDLE, callback_ms=5.0, latency_us=20_000):
    return [seq, ts, latency_us, None, callback_ms, mode]


def fake_request(sequence, sensor_ts_ns):
    """Shape of a Picamera2 CompletedRequest, as far as request_frame_info reads it."""
    buffer = SimpleNamespace(metadata=SimpleNamespace(sequence=sequence, timestamp=sensor_ts_ns))
    raw = SimpleNamespace(buffers={"raw": buffer}, metadata={}, sequence=sequence)
    return SimpleNamespace(request=raw)


class TestSummarize:
    def test_clean_stream_has_no_gaps(self):
        s = fd.summarize([rec(i, i * PERIOD_US) for i in range(20)])
        assert s["frames_recorded"] == 20
        assert s["frame_period_ms"] == 100.0
        assert s["frames_per_second"] == 10.0
        assert s["sequence"]["missing_frames"] == 0
        assert s["sequence"]["expected_frames"] == 20
        assert s["timestamp_gaps"]["gap_count"] == 0
        assert s["callbacks_slower_than_frame_period"] == 0

    def test_sequence_and_timestamp_gaps_are_counted(self):
        kept = [i for i in range(15) if i not in (5, 6, 12)]  # 3 frames dropped
        s = fd.summarize([rec(i, i * PERIOD_US, mode=fd.MODE_SERIES) for i in kept])
        assert s["sequence"]["missing_frames"] == 3
        assert s["sequence"]["gap_count"] == 2
        assert s["sequence"]["gaps"][0] == {"after_sequence": 4, "missing": 2,
                                            "mode_before": "series", "mode_after": "series"}
        assert s["timestamp_gaps"]["gap_count"] == 2
        assert s["timestamp_gaps"]["estimated_missing_frames"] == 3
        assert s["timestamp_gaps"]["gaps"][0]["interval_ms"] == 300.0

    def test_frame_period_hint_overrides_estimate(self):
        records = [rec(i, i * 2 * PERIOD_US) for i in range(5)]  # every other frame
        s = fd.summarize(records, frame_period_us=PERIOD_US)
        assert s["timestamp_gaps"]["estimated_missing_frames"] == 4

    def test_slow_callbacks_and_per_mode_stats(self):
        records = [rec(0, 0, fd.MODE_LEGACY, 10.0), rec(1, PERIOD_US, fd.MODE_LEGACY, 20.0),
                   rec(2, 2 * PERIOD_US, fd.MODE_LEGACY_SAVE, 900.0)]
        s = fd.summarize(records)
        assert s["callbacks_slower_than_frame_period"] == 1
        assert s["callback_ms_by_mode"]["legacy"]["count"] == 2
        assert s["callback_ms_by_mode"]["legacy_save"]["max"] == 900.0

    def test_falls_back_to_encoder_timestamps(self):
        records = [[None, None, None, i * PERIOD_US, 1.0, "idle"] for i in (0, 1, 2, 3, 5)]
        s = fd.summarize(records)
        assert s["sequence"] is None
        assert s["timestamp_gaps"]["estimated_missing_frames"] == 1

    def test_empty_and_single_record_are_safe(self):
        for records in ([], [rec(0, 0)]):
            s = fd.summarize(records)
            json.dumps(s)
            assert s["sequence"] is None and s["timestamp_gaps"] is None


class TestFrameDiagnostics:
    def test_inactive_records_nothing(self):
        d = fd.FrameDiagnostics()
        d.on_request(fake_request(1, 1000))
        d.record_callback(0, 0.01, fd.MODE_IDLE)
        assert d.status()["summary"]["frames_recorded"] == 0

    def test_request_and_callback_are_paired(self):
        d = fd.FrameDiagnostics()
        d.start(label="t")
        for i in range(3):
            d.on_request(fake_request(10 + i, (5 + i) * PERIOD_US * 1000))
            d.record_callback(i * PERIOD_US, 0.002, fd.MODE_IDLE)
        snap = d._snapshot()
        assert [r[0] for r in snap["records"]] == [10, 11, 12]
        assert [r[1] for r in snap["records"]] == [5 * PERIOD_US, 6 * PERIOD_US, 7 * PERIOD_US]
        assert snap["records"][0][4] == 2.0
        assert snap["unpaired_requests"] == 0

    def test_request_without_callback_is_counted(self):
        d = fd.FrameDiagnostics()
        d.start()
        d.on_request(fake_request(1, 0))
        d.on_request(fake_request(2, 0))
        d.record_callback(0, 0.0, fd.MODE_IDLE)
        assert d.status()["unpaired_requests"] == 1

    def test_bad_request_does_not_raise(self):
        d = fd.FrameDiagnostics()
        d.start()
        d.on_request(object())
        d.record_callback(0, 0.0, fd.MODE_IDLE)
        assert d._snapshot()["records"][0][:2] == [None, None]

    def test_record_cap(self):
        d = fd.FrameDiagnostics(max_records=2)
        d.start()
        for i in range(5):
            d.record_callback(i, 0.0, fd.MODE_IDLE)
        status = d.status()
        assert status["summary"]["frames_recorded"] == 2
        assert status["truncated_records"] == 3

    def test_start_twice_is_rejected_and_restart_clears(self):
        d = fd.FrameDiagnostics()
        first = d.start()
        with pytest.raises(RuntimeError):
            d.start()
        d.record_callback(0, 0.0, fd.MODE_IDLE)
        d.stop()
        assert d.start() != first
        assert d.status()["summary"]["frames_recorded"] == 0

    def test_stop_writes_json_and_stops_recording(self, tmp_path):
        d = fd.FrameDiagnostics()
        d.start(label="legacy 100/x", camera_info={"FrameDuration": PERIOD_US})
        for i in range(4):
            d.on_request(fake_request(i, i * PERIOD_US * 1000))
            d.record_callback(None, 0.001, fd.MODE_LEGACY)
        status, path = d.stop(output_dir=str(tmp_path / "out"))
        assert status["active"] is False and "records" not in status
        assert os.path.basename(path).endswith(f"_legacy_100_x_{status['session_id']}_frames.json")
        with open(path) as fp:
            data = json.load(fp)
        assert data["fields"] == list(fd.FIELDS)
        assert len(data["records"]) == 4
        assert data["summary"]["frame_period_ms"] == 100.0
        d.on_request(fake_request(9, 0))
        d.record_callback(0, 0.0, fd.MODE_IDLE)
        assert d.status()["summary"]["frames_recorded"] == 4


def test_request_frame_info_reads_buffer_metadata():
    assert fd.request_frame_info(fake_request(42, 123_456_789)) == (42, 123_456)
    assert fd.request_frame_info(object()) == (None, None)


class TestOutputFrameInstrumentation:
    def test_no_diagnostics_object_behaves_as_before(self):
        output = make_output()
        output.outputframe(frame_bytes(3), timestamp=1)
        assert output.cnt == 1

    def test_inactive_diagnostics_records_nothing(self):
        output = make_output()
        output.frame_diagnostics = fd.FrameDiagnostics()
        output.outputframe(frame_bytes(3), timestamp=1)
        assert output.cnt == 1
        assert output.frame_diagnostics.status()["summary"]["frames_recorded"] == 0

    def test_modes_are_recorded_for_legacy_accumulate(self, tmp_path):
        output = make_output()
        output.series_manager = camera_zmq.SeriesManager(logger=quiet)
        output.frame_diagnostics = d = fd.FrameDiagnostics()
        d.start()
        output.outputframe(frame_bytes(1), timestamp=0)  # idle
        output.accumulate(2, "LEGACY", str(tmp_path), exposure_time=11000, analogue_gain=1.0)
        for i, value in enumerate((2, 4, 0)):  # two accumulated, the third saves
            output.outputframe(frame_bytes(value), timestamp=(i + 1) * PERIOD_US)
        output.outputframe(frame_bytes(1), timestamp=4 * PERIOD_US)  # idle again
        modes = [r[5] for r in d._snapshot()["records"]]
        assert modes == ["idle", "legacy", "legacy", "legacy_save", "idle"]
        assert (tmp_path / "LEGACY.tif").exists()
        assert output.cnt == 5

    def test_series_mode_is_recorded(self, tmp_path):
        output = make_output()
        manager = camera_zmq.SeriesManager(logger=quiet)
        output.series_manager = manager
        output.frame_diagnostics = d = fd.FrameDiagnostics()
        d.start()
        manager.start(start_msg(tmp_path, image_count=1, nframes=2), legacy_busy=False,
                      camera_metadata={"ExposureTime": 1, "AnalogueGain": 1.0})
        output.outputframe(frame_bytes(10), timestamp=0)
        assert d._snapshot()["records"][0][5] == fd.MODE_SERIES

    def test_diagnostics_failure_does_not_reach_the_camera_thread(self):
        class BrokenLock:
            def __enter__(self):
                raise RuntimeError("broken")

            def __exit__(self, *exc):
                return False

        output = make_output()
        output.frame_diagnostics = d = fd.FrameDiagnostics()
        d.start()
        d._lock = BrokenLock()
        d.on_request(fake_request(1, 0))
        output.outputframe(frame_bytes(3), timestamp=1)  # must not raise
        assert output.cnt == 1


class TestServerDiagnosticsCommands:
    def test_start_status_stop_each_get_one_reply(self, tmp_path):
        state = {}

        def frames_then_status(replies):
            for i in range(3):
                state["output"].outputframe(frame_bytes(i), timestamp=i * PERIOD_US)
            return {"command": "frame_diagnostics_status"}

        socket, picam, output = run_server([
            {"command": "start_frame_diagnostics", "label": "run1"},
            {"command": "start_frame_diagnostics"},
            frames_then_status,
            {"command": "stop_frame_diagnostics", "output_dir": str(tmp_path)},
        ], output_hook=lambda o: state.update(output=o))

        assert len(socket.replies) == len(socket.requests) == 5
        started, again, status, stopped, _ = socket.replies
        assert started["ok"] is True and started["camera_info"]["ExposureTime"] == 30000
        assert again["error"]["code"] == camera_zmq.ERROR_BUSY
        assert status["active"] is True and status["summary"]["frames_recorded"] == 3
        assert stopped["ok"] is True and stopped["summary"]["frames_recorded"] == 3
        with open(stopped["path"]) as fp:
            assert len(json.load(fp)["records"]) == 3
        assert picam.post_callback == output.frame_diagnostics.on_request

    def test_stop_without_start_is_an_error(self):
        socket, _, _ = run_server([{"command": "stop_frame_diagnostics"}])
        assert socket.replies[0]["error"]["code"] == camera_zmq.ERROR_INVALID_REQUEST

    def test_unexpected_error_still_replies(self):
        with patch.object(camera_zmq, "handle_diagnostics_command", side_effect=RuntimeError("x")):
            socket, _, _ = run_server([{"command": "frame_diagnostics_status"}])
        assert socket.replies[0]["error"]["code"] == camera_zmq.ERROR_SERVER


class TestClientDiagnosticsMethods:
    @pytest.fixture
    def client(self):
        with patch.object(camera.Camera, "start_zmq"):
            cam = camera.Camera()
        cam.send_command = lambda msg: {"sent": msg}
        cam.zmqSocket = object()
        return cam

    def test_payloads(self, client):
        assert client.start_frame_diagnostics("L")["sent"] == {
            "command": "start_frame_diagnostics", "label": "L"}
        assert client.frame_diagnostics_status()["sent"] == {"command": "frame_diagnostics_status"}
        assert client.stop_frame_diagnostics()["sent"] == {"command": "stop_frame_diagnostics"}
        assert client.stop_frame_diagnostics("/d")["sent"] == {
            "command": "stop_frame_diagnostics", "output_dir": "/d"}
