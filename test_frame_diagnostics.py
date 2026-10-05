"""Tests for frame-delivery diagnostics (frame_diagnostics.py and its camera_zmq wiring).

No camera hardware is used. These tests check the bookkeeping only; whether
frames are actually dropped can only be measured on the Pi.
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import camera
import camera_zmq
import frame_diagnostics as fd
import continuous_capture as cc
from test_camera_series import frame_bytes, make_output, quiet, run_server, start_msg, wait_until

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

    def test_loss_is_reported_from_timestamps_even_without_sequence_gaps(self):
        # What the Pi actually does: contiguous buffer sequence numbers while
        # sensor timestamps jump (2026-10-04 run).
        stamps = [0, 1, 2, 5, 6, 10]  # frame periods; 2 + 3 frames missing
        s = fd.summarize([rec(i, t * PERIOD_US) for i, t in enumerate(stamps)],
                         frame_period_us=PERIOD_US)
        assert s["sequence"]["missing_frames"] == 0
        assert s["missing_frames"] == 5
        assert s["gap_count"] == 2
        assert s["loss_measure"] == "sensor_timestamp"

    def test_loss_measure_names_encoder_fallback(self):
        records = [[None, None, None, i * PERIOD_US, 1.0, "idle"] for i in (0, 1, 3)]
        s = fd.summarize(records, frame_period_us=PERIOD_US)
        assert s["missing_frames"] == 1 and s["loss_measure"] == "encoder_timestamp"

    def test_loss_fields_are_none_without_timestamps(self):
        s = fd.summarize([rec(0, 0)])
        assert s["missing_frames"] is None and s["gap_count"] is None
        assert s["loss_measure"] is None

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


class RecordingPicam:
    """Records configure() calls; optionally fails for some buffer counts."""

    def __init__(self, fail_counts=()):
        self.fail_counts = set(fail_counts)
        self.configured = []

    def create_video_configuration(self, **kwargs):
        return kwargs

    def configure(self, config):
        if config["buffer_count"] in self.fail_counts:
            raise RuntimeError("Failed to allocate buffers")
        self.configured.append(config)


class TestCameraBufferCount:
    def test_uses_raised_buffer_count(self):
        picam = RecordingPicam()
        with patch("builtins.print"):
            assert camera_zmq.configure_camera(picam) == camera_zmq.CAMERA_BUFFER_COUNT
        assert camera_zmq.CAMERA_BUFFER_COUNT > camera_zmq.PICAMERA2_DEFAULT_BUFFER_COUNT
        (config,) = picam.configured
        assert config["buffer_count"] == camera_zmq.CAMERA_BUFFER_COUNT
        assert config["raw"] == {"format": "SRGGB12", "size": (4056, 3040)}

    def test_falls_back_to_default_when_allocation_fails(self):
        picam = RecordingPicam(fail_counts={camera_zmq.CAMERA_BUFFER_COUNT})
        with patch("builtins.print"):
            used = camera_zmq.configure_camera(picam)
        assert used == camera_zmq.PICAMERA2_DEFAULT_BUFFER_COUNT
        assert [c["buffer_count"] for c in picam.configured] == [used]

    def test_default_count_failure_is_raised(self):
        picam = RecordingPicam(fail_counts={camera_zmq.PICAMERA2_DEFAULT_BUFFER_COUNT})
        with pytest.raises(RuntimeError):
            camera_zmq.configure_camera(picam, buffer_count=camera_zmq.PICAMERA2_DEFAULT_BUFFER_COUNT)

    def test_server_configures_raised_buffer_count(self):
        with patch.object(camera_zmq, "configure_camera", wraps=camera_zmq.configure_camera) as cfg:
            run_server([])
        cfg.assert_called_once()


class TestRunnerHelpers:
    @pytest.fixture
    def runner(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "IMAGE-SERIES-ANALYSIS", "run_frame_loss_diagnostics.py")
        spec = importlib.util.spec_from_file_location("run_frame_loss_diagnostics", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_run_series_records_end_state_and_queue_capacity(self, runner, tmp_path):
        class Cam:
            def start_series(self, *args, **kwargs):
                self.kwargs = kwargs
                return {"ok": True, "series_id": "s1"}

            def series_status(self, series_id):
                return {"is_terminal": True, "state": "overloaded", "requested_images": 15,
                        "captured_images": 9, "written_images": 9, "queue_capacity": 15,
                        "queue_high_watermark": 15, "error": {"code": "queue_full"}}

        cam = Cam()
        finished, result = runner.run_series(cam, str(tmp_path / "s"), "n", 15, 1, 15, 5)
        assert finished is True
        assert cam.kwargs["queue_capacity"] == 15
        assert result["state"] == "overloaded" and result["captured_images"] == 9
        assert result["error"] == {"code": "queue_full"} and result["series_id"] == "s1"

    def test_rejected_series_is_reported(self, runner, tmp_path):
        class Cam:
            def start_series(self, *args, **kwargs):
                return {"ok": False, "error": {"code": "busy"}}

        with patch("builtins.print"):
            finished, result = runner.run_series(Cam(), str(tmp_path / "s"), "n", 1, 1, 15, 5)
        assert finished is False and result == {"state": "rejected", "error": {"code": "busy"}}

    # --- memory safety ------------------------------------------------------

    def test_defaults_are_the_small_memory_safe_run(self, runner):
        with patch.object(sys, "argv", ["run_frame_loss_diagnostics.py"]), \
             patch.object(runner, "run", side_effect=lambda args: args) as run:
            args = runner.main()
        run.assert_called_once()
        assert args.conditions == ["idle", "legacy", "series"]
        assert args.repeats == 3 and args.series_images == 10 and args.queue_capacity == 8
        assert args.guard_mib == 400
        assert runner.ALL_CONDITIONS == ("idle", "legacy", "series")  # no tmpfs conditions

    def test_mem_available_parses_meminfo(self, runner, tmp_path):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal: 3700000 kB\nMemAvailable:  409600 kB\n")
        assert runner.mem_available_mib(str(meminfo)) == 400.0
        assert runner.mem_available_mib(str(tmp_path / "missing")) is None

    def test_guard_trips_below_threshold_and_tracks_minimum(self, runner):
        values = iter([900, 500, 399])
        guard = runner.MemoryGuard(400, read=lambda: next(values))
        guard.check()
        guard.check()
        with pytest.raises(runner.MemoryLow):
            guard.check()
        assert guard.min_mib == 399 and guard.tripped_mib == 399

    def test_unreadable_memory_never_trips(self, runner):
        guard = runner.MemoryGuard(400, read=lambda: None)
        guard.check()
        assert guard.min_mib is None

    def test_guard_trip_cancels_the_series_and_attaches_its_result(self, runner, tmp_path):
        class Cam:
            cancelled = None

            def start_series(self, *args, **kwargs):
                return {"ok": True, "series_id": "s1"}

            def series_status(self, series_id):
                return {"is_terminal": Cam.cancelled is not None, "state":
                        "cancelled" if Cam.cancelled else "capturing", "captured_images": 4}

            def cancel_series(self, series_id):
                Cam.cancelled = series_id

        values = iter([900, 300] + [900] * 50)
        guard = runner.MemoryGuard(400, read=lambda: next(values))
        with patch.object(runner.time, "sleep"), patch("builtins.print"), \
             pytest.raises(runner.MemoryLow) as info:
            runner.run_series(Cam(), str(tmp_path / "s"), "n", 10, 1, 8, 5, guard=guard)
        assert Cam.cancelled == "s1"
        assert info.value.result["state"] == "cancelled"
        assert info.value.result["series_id"] == "s1"

    def _args(self, tmp_path, **overrides):
        values = dict(conditions=["idle", "series"], repeats=2, legacy_nframes=100,
                      series_images=10, series_nframes=1, idle_seconds=0.0,
                      settle_seconds=0.0, output_root=str(tmp_path / "out"),
                      diagnostics_dir=str(tmp_path / "diag"), queue_capacity=8,
                      timeout=5.0, guard_mib=400, min_start_mib=1500)
        values.update(overrides)
        return SimpleNamespace(**values)

    class _RunnerCam:
        def __init__(self):
            self.started = []

        def start_frame_diagnostics(self, label):
            self.started.append(label)
            return {"ok": True}

        def stop_frame_diagnostics(self, output_dir):
            return {"path": f"{output_dir}/x.json", "summary": {"frames_recorded": 50}}

        def start_series(self, *args, **kwargs):
            return {"ok": True, "series_id": "s"}

        def series_status(self, series_id):
            return {"is_terminal": True, "state": "complete"}

        def cancel_series(self, series_id):
            pass

    def test_refuses_to_start_when_memory_is_low(self, runner, tmp_path):
        cam = self._RunnerCam()
        guard = runner.MemoryGuard(400, read=lambda: 1000)
        with patch("builtins.print"):
            code = runner.run(self._args(tmp_path), cam=cam, guard=guard)
        assert code == 3 and cam.started == []

    def test_guard_stops_the_run_skips_the_rest_and_writes_the_index(self, runner, tmp_path):
        cam = self._RunnerCam()
        state = {"tripped": False}

        def read():
            # Trip the guard once the series condition has started.
            if "series_r1" in cam.started and not state["tripped"]:
                state["tripped"] = True
                return 300
            return 2000

        guard = runner.MemoryGuard(400, read=read)
        cam.series_status = lambda series_id: {"is_terminal": state["tripped"],
                                               "state": "cancelled"}
        with patch.object(runner.time, "sleep"), patch("builtins.print"):
            code = runner.run(self._args(tmp_path), cam=cam, guard=guard)
        assert code == 4
        assert cam.started == ["idle_r1", "series_r1"]
        (index,) = list((tmp_path / "diag").glob("*_index.json"))
        entries = json.loads(index.read_text())
        assert [e["label"] for e in entries] == ["idle_r1", "series_r1", "idle_r2", "series_r2"]
        assert entries[1]["memory_guard_tripped"]
        assert entries[1]["min_mem_available_mib"] == 300
        assert entries[2]["skipped"] and entries[3]["skipped"]

    def test_full_run_completes_and_removes_leftover_tmpfs(self, runner, tmp_path):
        leftover = tmp_path / "shm" / "frame_diag_images"
        leftover.mkdir(parents=True)
        (leftover / "old.tif").write_bytes(b"x")
        cam = self._RunnerCam()
        guard = runner.MemoryGuard(400, read=lambda: 2000)
        with patch.object(runner.remove_leftover_tmpfs, "__defaults__", (str(leftover),)), \
             patch.object(runner.time, "sleep"), patch("builtins.print"):
            code = runner.run(self._args(tmp_path), cam=cam, guard=guard)
        assert code == 0
        assert cam.started == ["idle_r1", "series_r1", "idle_r2", "series_r2"]
        assert not leftover.exists()
        assert (tmp_path / "out").exists()  # test images go to the SD-card output root


# ----------------------------------------------------------------------------
# Stage profiling (StageTimer, stages_ms records and summary)
# ----------------------------------------------------------------------------

class TestStageTimer:
    def test_records_and_accumulates_stages(self):
        timer = fd.StageTimer()
        with patch.object(fd.time, "perf_counter", side_effect=[1.0, 1.25, 2.0, 2.5, 3.0, 3.001]):
            with timer.stage("a"):
                pass
            with timer.stage("b"):
                pass
            with timer.stage("a"):
                pass
        assert timer.stages_ms() == {"a": 251.0, "b": 500.0}

    def test_nothing_timed_reports_none(self):
        assert fd.StageTimer().stages_ms() is None

    def test_nested_stages_are_each_timed_inclusively(self):
        timer = fd.StageTimer()
        with patch.object(fd.time, "perf_counter", side_effect=[0.0, 0.1, 0.2, 0.4]):
            with timer.stage("outer"):
                with timer.stage("inner"):
                    pass
        assert timer.stages_ms() == {"outer": 400.0, "inner": 100.0}

    def test_errors_in_the_timed_block_propagate_and_are_still_timed(self):
        timer = fd.StageTimer()
        with pytest.raises(ValueError):
            with timer.stage("x"):
                raise ValueError("capture error")
        assert "x" in timer.stages_ms()

    def test_timer_failures_never_raise(self):
        timer = fd.StageTimer()
        with patch.object(fd.time, "perf_counter", side_effect=RuntimeError("clock")):
            with timer.stage("x"):
                pass
        assert timer.stages_ms() is None
        timer.stages = None  # misuse: corrupt state
        with timer.stage("y"):
            pass
        assert timer.stages_ms() is None

    def test_stage_helper_is_a_no_op_without_a_timer(self):
        with patch.object(fd.time, "perf_counter", side_effect=AssertionError("clock read")):
            with fd.stage(None, fd.STAGE_DECODE):
                pass
        broken = SimpleNamespace(stage=lambda name: 1 / 0)
        with fd.stage(broken, fd.STAGE_DECODE):  # a broken timer falls back to no-op
            pass


class TestStageRecordsAndSummary:
    def test_stop_writes_stages_ms_with_consistent_fields(self, tmp_path):
        d = fd.FrameDiagnostics()
        d.start(label="stages")
        timer = fd.StageTimer()
        timer.stages = {fd.STAGE_DECODE: 0.002}
        d.record_callback(0, 0.003, fd.MODE_SERIES, timer)
        d.record_callback(PERIOD_US, 0.001, fd.MODE_IDLE)
        _, path = d.stop(output_dir=str(tmp_path))
        with open(path) as fp:
            data = json.load(fp)
        assert data["fields"] == list(fd.FIELDS) and data["fields"][-1] == "stages_ms"
        assert all(len(r) == len(data["fields"]) for r in data["records"])
        assert data["records"][0][6] == {"decode": 2.0}
        assert data["records"][1][6] is None

    def test_summary_has_per_mode_stage_stats_and_keeps_existing_keys(self):
        records = []
        for i in range(4):
            stages = {"decode": 10.0, "accumulate": 40.0 + i, "complete": 100.0}
            records.append([i, i * PERIOD_US, 1000, None, 160.0, fd.MODE_SERIES, stages])
        records.append([4, 4 * PERIOD_US, 1000, None, 0.1, fd.MODE_IDLE, {"decode": 0.05}])
        s = fd.summarize(records, frame_period_us=PERIOD_US)
        for key in ("frames_recorded", "missing_frames", "gap_count", "loss_measure",
                    "sequence", "timestamp_gaps", "latency_ms", "callback_ms_by_mode",
                    "callbacks_slower_than_frame_period"):
            assert key in s
        series = s["stages_ms_by_mode"]["series"]
        assert series["frames_timed"] == 4
        assert list(series["stages"]) == ["decode", "accumulate", "complete"]  # pipeline order
        assert series["stages"]["accumulate"]["median"] == 41.5
        assert series["stages"]["complete"]["max"] == 100.0
        assert "p95" in series["stages"]["decode"]
        assert series["sum_of_stage_medians_ms"] == 151.5
        assert series["unattributed_ms"]["median"] == pytest.approx(8.5)
        assert series["median_share_of_callback"]["complete"] == 0.625
        assert "idle" in s["stages_ms_by_mode"]

    def test_stage_share_uses_only_frames_where_the_stage_ran(self):
        # nframes=1-like mix: most series frames are trivial, a few complete.
        records = [[i, i * PERIOD_US, 1000, None, 0.1, fd.MODE_SERIES, {"decode": 0.05}]
                   for i in range(10)]
        records += [[10 + i, (10 + i) * PERIOD_US, 1000, None, 200.0, fd.MODE_SERIES,
                     {"decode": 0.05, "complete": 100.0}] for i in range(3)]
        share = fd.summarize(records)["stages_ms_by_mode"]["series"]["median_share_of_callback"]
        assert share["complete"] == 0.5
        assert share["decode"] == 0.5  # 0.05 ms of the 0.1 ms median over all 13 frames

    def test_old_six_field_records_are_still_summarized(self):
        s = fd.summarize([rec(i, i * PERIOD_US) for i in range(5)])
        assert s["stages_ms_by_mode"] == {}
        assert s["missing_frames"] == 0


class TestCallbackStageProfiling:
    @staticmethod
    def _records(d):
        return d._snapshot()["records"]

    def test_series_stages_are_recorded_and_fit_in_callback_time(self, tmp_path):
        output = make_output()
        manager = camera_zmq.SeriesManager(logger=quiet)
        output.series_manager = manager
        output.frame_diagnostics = d = fd.FrameDiagnostics()
        d.start()
        series_id = manager.start(start_msg(tmp_path, image_count=2, nframes=2),
                                  legacy_busy=False,
                                  camera_metadata={"ExposureTime": 1, "AnalogueGain": 1.0})["series_id"]
        for i, value in enumerate((1, 3, 5, 7)):
            output.outputframe(frame_bytes(value), timestamp=i * PERIOD_US)
        assert wait_until(lambda: manager.status({"series_id": series_id})["is_terminal"])
        assert manager.status({"series_id": series_id})["state"] == cc.STATE_COMPLETE

        records = self._records(d)
        assert [r[5] for r in records] == [fd.MODE_SERIES] * 4
        for r in records:
            assert {"decode", "accumulate"} <= set(r[6])
            assert sum(r[6].values()) <= r[4] + 0.01  # rounding of callback_ms
        completing = [r[6] for r in records[1::2]]  # frames 2 and 4 complete a group
        for stages in completing:
            assert {"decode", "accumulate", "complete", "enqueue"} <= set(stages)
        for stages in (r[6] for r in records[0::2]):
            assert "complete" not in stages and "enqueue" not in stages
        # The sum is no longer zeroed in the callback (the pool zeroes it in the writer).
        assert all(fd.STAGE_RESET not in r[6] for r in records)

    def test_nframes_1_fast_path_stages(self, tmp_path):
        output = make_output()
        manager = camera_zmq.SeriesManager(logger=quiet)
        output.series_manager = manager
        output.frame_diagnostics = d = fd.FrameDiagnostics()
        d.start()
        series_id = manager.start(start_msg(tmp_path, image_count=3, nframes=1),
                                  legacy_busy=False,
                                  camera_metadata={"ExposureTime": 1, "AnalogueGain": 1.0})["series_id"]
        for i, value in enumerate((1, 3, 5)):
            output.outputframe(frame_bytes(value), timestamp=i * PERIOD_US)
        assert wait_until(lambda: manager.status({"series_id": series_id})["is_terminal"])
        assert manager.status({"series_id": series_id})["state"] == cc.STATE_COMPLETE
        for r in self._records(d):
            assert set(r[6]) == {"decode", "accumulate", "complete", "enqueue"}
            assert sum(r[6].values()) <= r[4] + 0.01

    def test_legacy_stages_are_recorded(self, tmp_path):
        output = make_output()
        output.series_manager = camera_zmq.SeriesManager(logger=quiet)
        output.frame_diagnostics = d = fd.FrameDiagnostics()
        d.start()
        output.accumulate(2, "LEGACY", str(tmp_path), exposure_time=11000, analogue_gain=1.0)
        for i, value in enumerate((2, 4, 0)):
            output.outputframe(frame_bytes(value), timestamp=i * PERIOD_US)
        records = self._records(d)
        assert [r[5] for r in records] == ["legacy", "legacy", "legacy_save"]
        assert "legacy_accumulate" in records[0][6] and "legacy_accumulate" in records[1][6]
        assert "legacy_save" in records[2][6] and "legacy_accumulate" not in records[2][6]
        assert all("decode" in r[6] for r in records)
        assert (tmp_path / "LEGACY.tif").exists()

    def test_diagnostics_off_times_nothing_and_capture_still_works(self, tmp_path):
        output = make_output()
        manager = camera_zmq.SeriesManager(logger=quiet)
        output.series_manager = manager
        output.frame_diagnostics = d = fd.FrameDiagnostics()  # never started
        series_id = manager.start(start_msg(tmp_path, image_count=1, nframes=2),
                                  legacy_busy=False,
                                  camera_metadata={"ExposureTime": 1, "AnalogueGain": 1.0})["series_id"]
        with patch.object(fd, "StageTimer", side_effect=AssertionError("timer created")), \
             patch.object(fd.StageTimer, "stage", side_effect=AssertionError("stage timed")):
            output.outputframe(frame_bytes(10), timestamp=0)
            output.outputframe(frame_bytes(20), timestamp=PERIOD_US)
        assert wait_until(lambda: manager.status({"series_id": series_id})["is_terminal"])
        assert manager.status({"series_id": series_id})["state"] == cc.STATE_COMPLETE
        assert d._snapshot()["records"] == []
