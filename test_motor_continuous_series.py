"""Tests for Motor.collect_motion_blur_data using the continuous series protocol.

No hardware is used: the camera client, light, stage and filesystem helpers are
mocks, and time.sleep/time.monotonic are replaced with a fake clock so polling,
rate-limited logging and timeouts run instantly and deterministically.
"""

import pytest
from unittest.mock import MagicMock, patch

import motor

SERIES_ID = "series-123"
BASENAME = "TEST001_20260101_M1_unstained_SM2_40x_2_130x_15y_350z"


class FakeClock:
    """monotonic() returns the fake time; sleep() advances it."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def status(state, captured, written, requested=3, is_terminal=None, error=None,
           frames_in_group=0, series_id=SERIES_ID):
    if is_terminal is None:
        is_terminal = state in ("complete", "failed", "overloaded", "cancelled")
    return {
        "ok": True,
        "series_id": series_id,
        "state": state,
        "is_terminal": is_terminal,
        "requested_images": requested,
        "captured_images": captured,
        "written_images": written,
        "discarded_images": 0,
        "discarded_partial_frames": 0,
        "nframes": 4,
        "frames_in_current_group": frames_in_group,
        "queue_depth": 1,
        "queue_high_watermark": 2,
        "queue_capacity": 3,
        "writing_image_index": None,
        "error": error,
        "process_rss_bytes": 200 * 1024 * 1024,
        "system_available_bytes": 3 * 1024 * 1024 * 1024,
    }


ACCEPTED = {"ok": True, "series_id": SERIES_ID, "state": "capturing",
            "requested_images": 3, "nframes": 4, "queue_capacity": 3}

HAPPY_STATUSES = [
    status("capturing", 0, 0),
    status("capturing", 1, 0),
    status("capturing", 2, 1),
    status("draining", 3, 2),
    status("complete", 3, 3),
]


def statuses_then_repeat(replies):
    """series_status side effect: return replies in order, then repeat the last."""
    replies = list(replies)
    calls = {"n": 0}

    def side_effect(series_id):
        index = min(calls["n"], len(replies) - 1)
        calls["n"] += 1
        return replies[index]

    return side_effect


class TestCollectMotionBlurData:
    def _make_motor(self, tmp_path, statuses=HAPPY_STATUSES):
        instance = motor.Motor.__new__(motor.Motor)
        instance.stop_requested = False
        instance.filename = MagicMock()
        instance.filename.barcode = "TEST001"
        instance.filename.date = "20260101"
        instance.filename.data_path_generator.return_value = str(tmp_path)
        instance.filename.data_filename_generator.side_effect = (
            lambda fov, objective, x, y, z: (
                f"TEST001_20260101_M1_unstained_SM2_{objective}x_{fov}_{x}x_{y}y_{z}z",
                str(tmp_path),
            )
        )
        instance.imager = MagicMock()
        instance.imager.start_series.return_value = dict(ACCEPTED)
        instance.imager.series_status.side_effect = statuses_then_repeat(statuses)
        instance.imager.cancel_series.return_value = {
            "ok": True, "series_id": SERIES_ID, "state": "draining"
        }
        instance.logger = MagicMock()
        instance.check_stop = MagicMock()
        instance.start_imaging = MagicMock()
        instance.stop_imaging = MagicMock()
        instance.set_smear_id = MagicMock()
        instance.move_carousel = MagicMock()
        instance.move_x_axis = MagicMock()
        instance.move_y_axis = MagicMock()
        instance.move_z_axis = MagicMock()
        return instance

    def _run(self, instance, **kwargs):
        """Run collect_motion_blur_data with patched helpers and a fake clock."""
        args = dict(x_pos=130, y_pos=15, z_pos=350, magnification=40,
                    smear_id="SM2", fov_number=2, nframes=4, image_count=3)
        args.update(kwargs)
        self.clock = FakeClock()
        with patch("motor.generate_barcode_folders") as self.generate_folders, \
             patch("motor.create_manifest_json") as self.create_manifest, \
             patch("motor.create_zstack_json") as self.create_zstack, \
             patch("motor.populate_zstack_json_from_folder") as self.populate_zstack, \
             patch("motor.time.sleep", side_effect=self.clock.sleep), \
             patch("motor.time.monotonic", side_effect=self.clock.monotonic):
            return instance.collect_motion_blur_data(**args)

    def _progress_logs(self, instance):
        return [
            call.args[0] for call in instance.logger.call_args_list
            if "available RAM" in str(call.args[0])
        ]

    def _assert_light_off_once(self, instance):
        instance.start_imaging.assert_called_once()
        instance.stop_imaging.assert_called_once()

    # --- Moved from test_motor_exposure_series.py, rewritten for the series path ---

    def test_starts_one_series_and_applies_exposure_after_objective(self, tmp_path):
        instance = self._make_motor(tmp_path)
        call_order = []
        instance.start_imaging.side_effect = lambda: call_order.append("light_on")
        instance.move_carousel.side_effect = lambda position: call_order.append(("carousel", position))
        instance.imager.set_exposure_time.side_effect = lambda value: call_order.append(("exposure", value))
        instance.imager.start_series.side_effect = (
            lambda *a, **k: call_order.append("start_series") or dict(ACCEPTED)
        )

        result = self._run(instance, exposure_time=25000)

        assert result["series_id"] == SERIES_ID
        assert result["status"]["state"] == "complete"
        assert result["status"]["written_images"] == 3
        assert call_order == ["light_on", ("carousel", "3"), ("exposure", 25000), "start_series"]
        # The exposure override is followed by a settling delay.
        assert 2 in self.clock.sleeps
        instance.imager.start_series.assert_called_once()
        call = instance.imager.start_series.call_args
        assert call.args == (3, 4, BASENAME, str(tmp_path))
        assert call.kwargs["z_height"] == 350
        assert call.kwargs["magnification"] == 40
        assert call.kwargs["metadata"] == {
            "x_pos": 130, "y_pos": 15, "z_pos": 350,
            "smear_id": "SM2", "fov": 2, "barcode": "TEST001",
        }
        instance.imager.take_rpi_image.assert_not_called()
        instance.imager.cancel_series.assert_not_called()
        self.generate_folders.assert_called_once_with("TEST001", ["SM2"], [2], run_date="20260101")
        self.create_manifest.assert_called_once_with(instance.filename)
        self.create_zstack.assert_called_once_with(str(tmp_path), 130, 15, 2, 40, "SM2")
        # Per-image sidecars are kept; no z-stack JSON consolidation.
        self.populate_zstack.assert_not_called()
        instance.set_smear_id.assert_called_once_with("SM2")
        self._assert_light_off_once(instance)

    def test_omitted_coordinates_use_na_names_null_metadata_and_skip_exposure(self, tmp_path):
        instance = self._make_motor(tmp_path, statuses=[status("complete", 1, 1, requested=1)])
        result = self._run(instance, x_pos=None, y_pos=None, z_pos=None,
                           fov_number=1, nframes=1, image_count=1)

        assert result["status"]["state"] == "complete"
        instance.filename.data_filename_generator.assert_called_once_with(1, 40, "NA", "NA", "NA")
        call = instance.imager.start_series.call_args
        assert call.args[:2] == (1, 1)
        assert call.args[2].endswith("_NAx_NAy_NAz")
        assert call.kwargs["z_height"] is None
        metadata = call.kwargs["metadata"]
        assert metadata["x_pos"] is None
        assert metadata["y_pos"] is None
        assert metadata["z_pos"] is None
        instance.imager.set_exposure_time.assert_not_called()
        assert 2 not in self.clock.sleeps
        self.create_zstack.assert_called_once_with(str(tmp_path), None, None, 1, 40, "SM2")
        self._assert_light_off_once(instance)

    @pytest.mark.parametrize("kwargs, match", [
        ({"nframes": 0}, "nframes"),
        ({"nframes": True}, "nframes"),
        ({"image_count": 0}, "image_count"),
        ({"fov_number": 0}, "fov_number"),
        ({"magnification": 10}, "20x and 40x"),
        ({"smear_id": ""}, "smear_id"),
        ({"exposure_time": 0}, "exposure_time"),
        ({"exposure_time": 1.5}, "exposure_time"),
        ({"queue_capacity": 0}, "queue_capacity"),
        ({"queue_capacity": "3"}, "queue_capacity"),
        ({"queue_capacity": True}, "queue_capacity"),
        ({"width": 0}, "width"),
        ({"width": 4056.0}, "width"),
    ])
    def test_rejects_invalid_capture_parameters_before_imaging(self, tmp_path, kwargs, match):
        instance = self._make_motor(tmp_path)
        with pytest.raises(ValueError, match=match):
            self._run(instance, **kwargs)
        instance.start_imaging.assert_not_called()
        instance.stop_imaging.assert_not_called()
        instance.imager.start_series.assert_not_called()
        self.generate_folders.assert_not_called()

    # --- Series request ---

    def test_twenty_x_selects_carousel_position_two(self, tmp_path):
        instance = self._make_motor(tmp_path)
        self._run(instance, magnification=20)
        instance.move_carousel.assert_called_once_with("2")
        assert instance.imager.start_series.call_args.kwargs["magnification"] == 20

    def test_does_not_move_stage_axes(self, tmp_path):
        instance = self._make_motor(tmp_path)
        self._run(instance)
        instance.move_x_axis.assert_not_called()
        instance.move_y_axis.assert_not_called()
        instance.move_z_axis.assert_not_called()

    def test_optional_series_args_omitted_when_unset(self, tmp_path):
        instance = self._make_motor(tmp_path)
        self._run(instance)
        kwargs = instance.imager.start_series.call_args.kwargs
        assert "queue_capacity" not in kwargs
        assert "width" not in kwargs

    def test_optional_series_args_passed_when_set(self, tmp_path):
        instance = self._make_motor(tmp_path)
        self._run(instance, queue_capacity=5, width=4000)
        instance.imager.start_series.assert_called_once()
        kwargs = instance.imager.start_series.call_args.kwargs
        assert kwargs["queue_capacity"] == 5
        assert kwargs["width"] == 4000

    # --- Polling and progress ---

    def test_polls_status_until_complete(self, tmp_path):
        instance = self._make_motor(tmp_path)
        self._run(instance)
        assert instance.imager.series_status.call_count == len(HAPPY_STATUSES)
        assert all(c.args == (SERIES_ID,) for c in instance.imager.series_status.call_args_list)
        poll_sleeps = [s for s in self.clock.sleeps if s == motor.MOTION_BLUR_POLL_INTERVAL_S]
        assert len(poll_sleeps) == len(HAPPY_STATUSES) - 1
        # check_stop() logs on every call, so polling reads the flag directly.
        instance.check_stop.assert_not_called()

    def test_progress_logs_are_rate_limited(self, tmp_path):
        # 30 s (60 polls) in one state with steady progress, then complete.
        running = [
            status("capturing", i // 20, i // 30, frames_in_group=i % 20)
            for i in range(60)
        ]
        instance = self._make_motor(tmp_path, statuses=running + [status("complete", 3, 3)])
        self._run(instance)

        logs = self._progress_logs(instance)
        assert instance.imager.series_status.call_count == 61
        # First status, about one line every 5 s over 30 s, and the final
        # status; nowhere near one line per poll.
        assert 6 <= len(logs) <= 9
        assert "state=capturing" in logs[0]
        assert "state=complete" in logs[-1]
        assert "written 3/3" in logs[-1]
        assert "queue 1/3 (peak 2)" in logs[0]
        assert "process RSS 200 MiB" in logs[0]
        assert "available RAM 3072 MiB" in logs[0]

    def test_progress_logged_on_state_change(self, tmp_path):
        instance = self._make_motor(tmp_path, statuses=[
            status("capturing", 0, 0),
            status("capturing", 1, 0),
            status("draining", 3, 2),
            status("complete", 3, 3),
        ])
        self._run(instance)
        states = [log.split("state=")[1].split(",")[0] for log in self._progress_logs(instance)]
        assert states == ["capturing", "draining", "complete"]

    def test_single_timeout_status_reply_is_tolerated(self, tmp_path):
        instance = self._make_motor(tmp_path, statuses=[
            status("capturing", 1, 0),
            {"timeout": "controller is busy"},
            status("capturing", 2, 1),
            status("complete", 3, 3),
        ])
        result = self._run(instance)
        assert result["status"]["state"] == "complete"
        instance.imager.cancel_series.assert_not_called()
        self._assert_light_off_once(instance)

    def test_frame_progress_within_long_group_is_not_a_stall(self, tmp_path):
        # Image counts stay at 0 for 90 s, but frames keep arriving in the group.
        slow = [status("capturing", 0, 0, frames_in_group=i) for i in range(180)]
        instance = self._make_motor(tmp_path, statuses=slow + [status("complete", 3, 3)])
        result = self._run(instance)
        assert result["status"]["state"] == "complete"

    # --- Failures ---

    def test_complete_with_too_few_written_images_is_failure(self, tmp_path):
        instance = self._make_motor(tmp_path, statuses=[status("complete", 3, 2)])
        with pytest.raises(RuntimeError, match=r"did not complete.*state=complete.*written 2/3"):
            self._run(instance)
        instance.imager.cancel_series.assert_called_once_with(SERIES_ID)
        self._assert_light_off_once(instance)

    def test_rejected_start_raises_and_turns_light_off(self, tmp_path):
        instance = self._make_motor(tmp_path)
        instance.imager.start_series.return_value = {
            "ok": False,
            "error": {"code": "BUSY", "message": "A continuous series is already in progress"},
        }
        with pytest.raises(RuntimeError, match="rejected.*BUSY.*already in progress"):
            self._run(instance)
        instance.imager.series_status.assert_not_called()
        instance.imager.cancel_series.assert_not_called()
        self._assert_light_off_once(instance)

    def test_timeout_start_reply_warns_series_may_still_run(self, tmp_path):
        instance = self._make_motor(tmp_path)
        instance.imager.start_series.return_value = {"timeout": "controller is busy"}
        with pytest.raises(RuntimeError, match="may still be running"):
            self._run(instance)
        instance.imager.series_status.assert_not_called()
        instance.imager.cancel_series.assert_not_called()
        self._assert_light_off_once(instance)

    def test_writer_failure_raises_with_error_details(self, tmp_path):
        error = {"code": "WRITE_FAILED", "message": "Could not write image 2: OSError: disk full",
                 "image_index": 2, "filename": f"{BASENAME}_image2"}
        instance = self._make_motor(tmp_path, statuses=[
            status("capturing", 1, 0),
            status("failed", 2, 1, error=error),
        ])
        with pytest.raises(RuntimeError) as excinfo:
            self._run(instance)
        message = str(excinfo.value)
        assert "state=failed" in message
        assert "captured 2/3" in message and "written 1/3" in message
        assert "WRITE_FAILED" in message and "disk full" in message
        assert f"failing image=2 ({BASENAME}_image2)" in message
        instance.imager.cancel_series.assert_called_once_with(SERIES_ID)
        self._assert_light_off_once(instance)

    def test_overload_raises(self, tmp_path):
        error = {"code": "QUEUE_OVERLOADED", "message": "Writer queue full",
                 "image_index": 3, "filename": f"{BASENAME}_image3"}
        instance = self._make_motor(tmp_path, statuses=[status("overloaded", 3, 2, error=error)])
        with pytest.raises(RuntimeError, match="state=overloaded.*QUEUE_OVERLOADED"):
            self._run(instance)
        self._assert_light_off_once(instance)

    @pytest.mark.parametrize("bad_reply", [
        {"timeout": "controller is busy"},
        None,
        "not a dict",
        {"ok": True, "series_id": SERIES_ID},
        {"ok": False, "error": {"code": "UNKNOWN_SERIES", "message": "No series"}},
        status("capturing", "1", 0),
        status("capturing", 1, 0, series_id="other-series"),
    ])
    def test_consecutive_bad_status_replies_raise(self, tmp_path, bad_reply):
        instance = self._make_motor(tmp_path, statuses=[status("capturing", 1, 0), bad_reply])
        with pytest.raises(RuntimeError, match="consecutive unusable status replies"):
            self._run(instance)
        assert instance.imager.series_status.call_count == 1 + motor.MOTION_BLUR_MAX_BAD_REPLIES
        instance.imager.cancel_series.assert_called_once_with(SERIES_ID)
        self._assert_light_off_once(instance)

    def test_stall_timeout_raises(self, tmp_path):
        instance = self._make_motor(
            tmp_path, statuses=[status("capturing", 1, 0, frames_in_group=2)]
        )
        with pytest.raises(RuntimeError, match=r"no progress for 60 s.*captured 1/3, written 0/3"):
            self._run(instance)
        expected = motor.MOTION_BLUR_STALL_TIMEOUT_S / motor.MOTION_BLUR_POLL_INTERVAL_S
        assert expected <= instance.imager.series_status.call_count <= expected + 2
        instance.imager.cancel_series.assert_called_once_with(SERIES_ID)
        self._assert_light_off_once(instance)
        # About one progress line per 5 s, not one per poll.
        assert len(self._progress_logs(instance)) <= 14

    def test_error_applying_exposure_turns_light_off(self, tmp_path):
        instance = self._make_motor(tmp_path)
        instance.imager.set_exposure_time.side_effect = OSError("camera unreachable")
        with pytest.raises(OSError):
            self._run(instance, exposure_time=25000)
        instance.imager.start_series.assert_not_called()
        instance.imager.cancel_series.assert_not_called()
        self._assert_light_off_once(instance)

    def test_carousel_error_turns_light_off(self, tmp_path):
        instance = self._make_motor(tmp_path)
        instance.move_carousel.side_effect = RuntimeError("carousel jammed")
        with pytest.raises(RuntimeError, match="carousel jammed"):
            self._run(instance)
        instance.imager.start_series.assert_not_called()
        self._assert_light_off_once(instance)

    def test_unexpected_exception_while_polling_cancels_and_turns_light_off(self, tmp_path):
        instance = self._make_motor(tmp_path)
        instance.imager.series_status.side_effect = ValueError("bad JSON")
        with pytest.raises(ValueError):
            self._run(instance)
        instance.imager.cancel_series.assert_called_once_with(SERIES_ID)
        self._assert_light_off_once(instance)

    # --- Stop ---

    def test_stop_cancels_waits_for_terminal_and_reports_partial_counts(self, tmp_path):
        instance = self._make_motor(tmp_path)
        replies = [
            status("capturing", 1, 0),
            status("capturing", 2, 1),
            status("draining", 2, 1),
            status("draining", 2, 1),
            status("cancelled", 2, 2),
        ]
        calls = {"n": 0}

        def series_status(series_id):
            reply = replies[min(calls["n"], len(replies) - 1)]
            calls["n"] += 1
            if calls["n"] == 2:
                instance.stop_requested = True  # operator presses stop mid-series
            return reply

        instance.imager.series_status.side_effect = series_status
        with pytest.raises(RuntimeError) as excinfo:
            self._run(instance)
        message = str(excinfo.value)
        assert "cancelled by stop request" in message
        assert "state=cancelled" in message
        assert "captured 2/3, written 2/3" in message
        instance.imager.cancel_series.assert_called_once_with(SERIES_ID)
        # Polling continued after the cancel until the series was terminal.
        assert instance.imager.series_status.call_count == 5
        instance.check_stop.assert_not_called()
        self._assert_light_off_once(instance)

    def test_stop_that_ends_as_complete_is_still_reported_as_stopped(self, tmp_path):
        # Cancelling after every image was captured can still end "complete";
        # the operator asked to stop, so this must not read as a normal success.
        instance = self._make_motor(tmp_path, statuses=[status("complete", 3, 3)])
        instance.imager.start_series.side_effect = (
            lambda *a, **k: setattr(instance, "stop_requested", True) or dict(ACCEPTED)
        )
        with pytest.raises(RuntimeError, match=r"cancelled by stop request.*written 3/3"):
            self._run(instance)
        instance.imager.cancel_series.assert_called_once_with(SERIES_ID)
        self._assert_light_off_once(instance)

    def test_stop_before_series_start_does_not_start_series(self, tmp_path):
        instance = self._make_motor(tmp_path)
        instance.move_carousel.side_effect = lambda pos: setattr(instance, "stop_requested", True)
        with pytest.raises(RuntimeError, match="stop requested"):
            self._run(instance)
        instance.imager.start_series.assert_not_called()
        instance.imager.cancel_series.assert_not_called()
        self._assert_light_off_once(instance)

    def test_stop_retries_unacknowledged_cancel(self, tmp_path):
        instance = self._make_motor(tmp_path, statuses=[
            status("capturing", 1, 0),
            status("draining", 1, 1),
            status("cancelled", 1, 1),
        ])
        instance.imager.start_series.side_effect = (
            lambda *a, **k: setattr(instance, "stop_requested", True) or dict(ACCEPTED)
        )
        instance.imager.cancel_series.side_effect = [
            {"timeout": "controller is busy"},
            {"ok": True, "series_id": SERIES_ID, "state": "draining"},
        ]
        with pytest.raises(RuntimeError, match=r"cancelled by stop request.*written 1/3"):
            self._run(instance)
        assert instance.imager.cancel_series.call_count == 2
        self._assert_light_off_once(instance)

    def test_stop_wait_for_terminal_is_bounded(self, tmp_path):
        instance = self._make_motor(tmp_path)
        polls = {"n": 0}

        def series_status(series_id):
            instance.stop_requested = True
            polls["n"] += 1
            # Keeps reporting progress, so only the cancel wait bound ends it.
            return status("draining", 2, 1, frames_in_group=polls["n"])

        instance.imager.series_status.side_effect = series_status
        with pytest.raises(RuntimeError, match="did not report a terminal state"):
            self._run(instance)
        instance.imager.cancel_series.assert_called_once_with(SERIES_ID)
        expected = motor.MOTION_BLUR_CANCEL_WAIT_S / motor.MOTION_BLUR_POLL_INTERVAL_S
        assert instance.imager.series_status.call_count <= expected + 2
        self._assert_light_off_once(instance)


# ----------------------------------------------------------------------------
# Repeated series: series_count > 1 uses one camera-side batch request
# ----------------------------------------------------------------------------

BATCH_ID = "batch-456"


def batch_status(state, captured, written, series_count=3, per_series=2, is_terminal=None,
                 error=None, skipped=0, boundaries=None, batch_id=BATCH_ID, awaiting=False):
    snap = status(state, captured, written, requested=series_count * per_series,
                  is_terminal=is_terminal, error=error, series_id=batch_id)
    current = min(captured // per_series + 1, series_count)
    snap.update({
        "batch_id": batch_id,
        "series_ids": [f"{batch_id}-s{n}" for n in range(1, series_count + 1)],
        "series_count": series_count,
        "images_per_series": per_series,
        "inter_series_pause_us": 500_000,
        "current_series_index": current,
        "series_captured_images": captured - (current - 1) * per_series,
        "completed_series": captured // per_series,
        "awaiting_next_series": awaiting,
        "frames_skipped_in_pauses": skipped,
        "boundaries": boundaries or [],
    })
    return snap


BATCH_ACCEPTED = {"ok": True, "batch_id": BATCH_ID, "state": "capturing",
                  "series_ids": [f"{BATCH_ID}-s{n}" for n in (1, 2, 3)],
                  "requested_images": 6}

BOUNDARIES = [
    {"before_series_index": 2, "previous_last_timestamp_us": 100_000,
     "first_timestamp_us": 600_000, "observed_gap_us": 500_000, "frames_skipped": 4},
    {"before_series_index": 3, "previous_last_timestamp_us": 700_000,
     "first_timestamp_us": 1_200_000, "observed_gap_us": 500_000, "frames_skipped": 4},
]

HAPPY_BATCH = [
    batch_status("capturing", 0, 0),
    batch_status("capturing", 2, 1, awaiting=True, skipped=2),
    batch_status("capturing", 4, 3, awaiting=True, skipped=6),
    batch_status("draining", 6, 5, skipped=8),
    batch_status("complete", 6, 6, skipped=8, boundaries=BOUNDARIES),
]


class TestCollectMotionBlurBatch:
    def setup_method(self):
        # Reuse the single-series fixtures without rerunning their tests.
        self.base = TestCollectMotionBlurData()

    @property
    def clock(self):
        return self.base.clock

    def _make_batch_motor(self, tmp_path, statuses=HAPPY_BATCH):
        instance = self.base._make_motor(tmp_path)
        instance.imager.start_series_batch.return_value = dict(BATCH_ACCEPTED)
        instance.imager.series_batch_status.side_effect = statuses_then_repeat(statuses)
        instance.imager.cancel_series_batch.return_value = {
            "ok": True, "batch_id": BATCH_ID, "state": "draining"}
        return instance

    def _run_batch(self, instance, **kwargs):
        kwargs.setdefault("series_count", 3)
        kwargs.setdefault("image_count", 2)
        kwargs.setdefault("nframes", 1)
        kwargs.setdefault("inter_series_pause_s", 0.5)
        return self.base._run(instance, **kwargs)

    def _assert_light_off_once(self, instance):
        self.base._assert_light_off_once(instance)

    def _logs(self, instance):
        return [str(c.args[0]) for c in instance.logger.call_args_list]

    def test_one_batch_request_with_camera_side_pause(self, tmp_path):
        instance = self._make_batch_motor(tmp_path)
        call_order = []
        instance.start_imaging.side_effect = lambda: call_order.append("light_on")
        instance.move_carousel.side_effect = lambda p: call_order.append(("carousel", p))
        instance.imager.set_exposure_time.side_effect = lambda v: call_order.append(("exposure", v))
        instance.imager.start_series_batch.side_effect = (
            lambda *a, **k: call_order.append("start_batch") or dict(BATCH_ACCEPTED))

        result = self._run_batch(instance, exposure_time=30000)

        assert call_order == ["light_on", ("carousel", "3"), ("exposure", 30000), "start_batch"]
        instance.imager.start_series_batch.assert_called_once()
        call = instance.imager.start_series_batch.call_args
        assert call.args == (2, 3, 1, BASENAME, str(tmp_path))
        assert call.kwargs["inter_series_pause_us"] == 500_000
        assert call.kwargs["z_height"] == 350
        assert call.kwargs["magnification"] == 40
        assert call.kwargs["metadata"]["barcode"] == "TEST001"
        assert "queue_capacity" not in call.kwargs and "width" not in call.kwargs
        # No single-series commands, and the only sleeps are the exposure
        # settle and status polling: Motor never waits out the pause itself.
        instance.imager.start_series.assert_not_called()
        instance.imager.series_status.assert_not_called()
        assert set(self.clock.sleeps) <= {2, motor.MOTION_BLUR_POLL_INTERVAL_S}
        assert result == {"batch_id": BATCH_ID, "series_ids": BATCH_ACCEPTED["series_ids"],
                          "status": HAPPY_BATCH[-1]}
        instance.imager.cancel_series_batch.assert_not_called()
        self._assert_light_off_once(instance)

    def test_polls_batch_status_and_logs_series_progress_and_gaps(self, tmp_path):
        instance = self._make_batch_motor(tmp_path)
        self._run_batch(instance)
        calls = instance.imager.series_batch_status.call_args_list
        assert len(calls) == len(HAPPY_BATCH)
        assert all(c.args == (BATCH_ID,) for c in calls)
        progress = [m for m in self._logs(instance) if "available RAM" in m]
        assert progress[0].startswith(f"Motion-blur batch {BATCH_ID}: state=capturing, series 1/3")
        assert "total captured 0/6" in progress[0]
        assert "written 6/6" in progress[-1]
        assert len(progress) < len(HAPPY_BATCH)  # rate-limited, not one line per poll
        gaps = [m for m in self._logs(instance) if "gap before series" in m]
        assert gaps == [
            f"Motion-blur batch {BATCH_ID}: gap before series 2 = 500.0 ms "
            "(requested minimum 500.0 ms, 4 frame(s) skipped)",
            f"Motion-blur batch {BATCH_ID}: gap before series 3 = 500.0 ms "
            "(requested minimum 500.0 ms, 4 frame(s) skipped)",
        ]

    def test_optional_batch_args_passed_when_set(self, tmp_path):
        instance = self._make_batch_motor(tmp_path)
        self._run_batch(instance, queue_capacity=6, width=4056, inter_series_pause_s=0)
        kwargs = instance.imager.start_series_batch.call_args.kwargs
        assert kwargs["queue_capacity"] == 6
        assert kwargs["width"] == 4056
        assert kwargs["inter_series_pause_us"] == 0

    def test_series_count_one_uses_the_single_series_protocol(self, tmp_path):
        instance = self._make_batch_motor(tmp_path)
        result = self.base._run(instance, series_count=1, inter_series_pause_s=2.0)
        assert result["series_id"] == SERIES_ID
        instance.imager.start_series.assert_called_once()
        instance.imager.start_series_batch.assert_not_called()
        instance.imager.series_batch_status.assert_not_called()

    @pytest.mark.parametrize("kwargs, match", [
        ({"series_count": 0}, "series_count"),
        ({"series_count": True}, "series_count"),
        ({"series_count": 2.0}, "series_count"),
        ({"inter_series_pause_s": -0.1}, "inter_series_pause_s"),
        ({"inter_series_pause_s": float("nan")}, "inter_series_pause_s"),
        ({"inter_series_pause_s": float("inf")}, "inter_series_pause_s"),
        ({"inter_series_pause_s": "0.5"}, "inter_series_pause_s"),
        ({"inter_series_pause_s": True}, "inter_series_pause_s"),
    ])
    def test_rejects_invalid_batch_parameters_before_imaging(self, tmp_path, kwargs, match):
        instance = self._make_batch_motor(tmp_path)
        with pytest.raises(ValueError, match=match):
            self._run_batch(instance, **kwargs)
        instance.start_imaging.assert_not_called()
        instance.imager.start_series_batch.assert_not_called()

    def test_progress_line_shows_a_pause_between_series(self, tmp_path):
        instance = self._make_batch_motor(tmp_path)
        instance._log_series_progress(
            BATCH_ID, batch_status("capturing", 2, 1, awaiting=True), 6, batch=True)
        line = instance.logger.call_args.args[0]
        assert "series 2/3 (0/2 captured, pausing), total captured 2/6, written 1/6" in line

    # --- Batch failures ---

    def test_rejected_batch_raises_and_turns_light_off(self, tmp_path):
        instance = self._make_batch_motor(tmp_path)
        instance.imager.start_series_batch.return_value = {
            "ok": False,
            "error": {"code": "BUSY", "message": "A continuous series is already in progress"},
        }
        with pytest.raises(RuntimeError, match="rejected the motion-blur batch: BUSY"):
            self._run_batch(instance)
        instance.imager.series_batch_status.assert_not_called()
        instance.imager.cancel_series_batch.assert_not_called()
        self._assert_light_off_once(instance)

    def test_timeout_start_warns_batch_may_still_run(self, tmp_path):
        instance = self._make_batch_motor(tmp_path)
        instance.imager.start_series_batch.return_value = {"timeout": "controller is busy"}
        with pytest.raises(RuntimeError, match="batch may still be running.*no batch ID"):
            self._run_batch(instance)
        instance.imager.cancel_series_batch.assert_not_called()
        self._assert_light_off_once(instance)

    def test_overload_in_a_later_series_reports_series_and_cancels(self, tmp_path):
        error = {"code": "QUEUE_OVERLOADED", "message": "Writer queue full",
                 "image_index": 4, "filename": f"{BASENAME}_series2_image2",
                 "series_index": 2, "series_image_index": 2}
        instance = self._make_batch_motor(tmp_path, statuses=[
            batch_status("capturing", 2, 1),
            batch_status("overloaded", 3, 3, error=error),
        ])
        with pytest.raises(RuntimeError) as excinfo:
            self._run_batch(instance)
        message = str(excinfo.value)
        assert f"batch {BATCH_ID} did not complete" in message
        assert "state=overloaded" in message and "QUEUE_OVERLOADED" in message
        assert "captured 3/6, written 3/6" in message
        assert "completed series 1/3" in message
        assert "series 2 image 2" in message
        instance.imager.cancel_series_batch.assert_called_once_with(BATCH_ID)
        instance.imager.cancel_series.assert_not_called()
        self._assert_light_off_once(instance)

    def test_complete_with_missing_images_is_failure(self, tmp_path):
        instance = self._make_batch_motor(tmp_path, statuses=[batch_status("complete", 6, 5)])
        with pytest.raises(RuntimeError, match="did not complete.*written 5/6"):
            self._run_batch(instance)
        self._assert_light_off_once(instance)

    @pytest.mark.parametrize("bad_reply", [
        {"timeout": "controller is busy"},
        {"ok": False, "error": {"code": "UNKNOWN_SERIES", "message": "No batch"}},
        # A single-series status has no batch_id, so it is not a valid batch reply.
        status("capturing", 1, 0, series_id=BATCH_ID),
        batch_status("capturing", 1, 0, batch_id="other-batch"),
    ])
    def test_consecutive_bad_batch_status_replies_raise(self, tmp_path, bad_reply):
        instance = self._make_batch_motor(
            tmp_path, statuses=[batch_status("capturing", 1, 0), bad_reply])
        with pytest.raises(RuntimeError, match=f"batch {BATCH_ID}: 5 consecutive unusable"):
            self._run_batch(instance)
        instance.imager.cancel_series_batch.assert_called_once_with(BATCH_ID)
        self._assert_light_off_once(instance)

    def test_long_pause_with_skipped_frames_is_not_a_stall(self, tmp_path):
        # 90 s with no new images, but frames keep being skipped in the pause.
        pausing = [batch_status("capturing", 2, 2, awaiting=True, skipped=i) for i in range(180)]
        instance = self._make_batch_motor(
            tmp_path, statuses=pausing + [batch_status("complete", 6, 6, skipped=180)])
        result = self._run_batch(instance, inter_series_pause_s=120)
        assert result["status"]["state"] == "complete"
        kwargs = instance.imager.start_series_batch.call_args.kwargs
        assert kwargs["inter_series_pause_us"] == 120_000_000

    def test_batch_stall_still_times_out(self, tmp_path):
        instance = self._make_batch_motor(
            tmp_path, statuses=[batch_status("capturing", 2, 2, awaiting=True, skipped=3)])
        with pytest.raises(RuntimeError, match=f"batch {BATCH_ID} made no progress for 60 s"):
            self._run_batch(instance)
        instance.imager.cancel_series_batch.assert_called_once_with(BATCH_ID)
        self._assert_light_off_once(instance)

    def test_unexpected_exception_while_polling_cancels_batch(self, tmp_path):
        instance = self._make_batch_motor(tmp_path)
        instance.imager.series_batch_status.side_effect = ValueError("bad JSON")
        with pytest.raises(ValueError):
            self._run_batch(instance)
        instance.imager.cancel_series_batch.assert_called_once_with(BATCH_ID)
        self._assert_light_off_once(instance)

    # --- Batch stop ---

    def test_stop_cancels_batch_waits_and_reports_partial_series(self, tmp_path):
        instance = self._make_batch_motor(tmp_path)
        replies = [
            batch_status("capturing", 2, 1),
            batch_status("capturing", 3, 2),
            batch_status("draining", 3, 2),
            batch_status("cancelled", 3, 3),
        ]
        calls = {"n": 0}

        def series_batch_status(batch_id):
            reply = replies[min(calls["n"], len(replies) - 1)]
            calls["n"] += 1
            if calls["n"] == 2:
                instance.stop_requested = True  # operator presses stop mid-batch
            return reply

        instance.imager.series_batch_status.side_effect = series_batch_status
        with pytest.raises(RuntimeError) as excinfo:
            self._run_batch(instance)
        message = str(excinfo.value)
        assert f"batch {BATCH_ID} cancelled by stop request" in message
        assert "state=cancelled" in message
        assert "captured 3/6, written 3/6" in message
        assert "completed series 1/3" in message
        instance.imager.cancel_series_batch.assert_called_once_with(BATCH_ID)
        instance.imager.cancel_series.assert_not_called()
        assert instance.imager.series_batch_status.call_count == 4
        instance.check_stop.assert_not_called()
        self._assert_light_off_once(instance)

    def test_stop_before_batch_start_does_not_start_it(self, tmp_path):
        instance = self._make_batch_motor(tmp_path)
        instance.move_carousel.side_effect = lambda pos: setattr(instance, "stop_requested", True)
        with pytest.raises(RuntimeError, match="stop requested before the motion-blur batch"):
            self._run_batch(instance)
        instance.imager.start_series_batch.assert_not_called()
        instance.imager.cancel_series_batch.assert_not_called()
        self._assert_light_off_once(instance)

