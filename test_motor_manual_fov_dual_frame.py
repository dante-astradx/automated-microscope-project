"""Tests for dual-frame (``_1frame`` / ``_100frames``) manual FOV z-stack collection."""

import os

import pytest
from unittest.mock import MagicMock, patch

import motor
from milestone5_file_transfer import FileTransfer5


BASE = "TEST001_20260101_M1_unstained_SM1_40x_1_130x_15y_350z"


class TestCaptureImageDualFrame:
    def _make_motor(self, tmp_path):
        instance = motor.Motor.__new__(motor.Motor)
        instance.filename = MagicMock()
        instance.filename.data_filename_generator.return_value = (
            BASE,
            str(tmp_path),
        )
        instance.imager = MagicMock()
        instance.logger = MagicMock()
        instance.check_stop = MagicMock()
        instance.focus_view = 1
        instance.obj = 40
        instance.current_x = 130
        instance.current_y = 15
        instance.capture_frame_variants = [(100, "")]
        instance.populate_zstack_json = True

        def write_outputs(nframes, filename, file_path, **kwargs):
            (tmp_path / f"{filename}.tif").write_bytes(b"tif")
            (tmp_path / f"{filename}.json").write_text("{}")

        instance.imager.take_rpi_image.side_effect = write_outputs
        return instance

    def test_dual_mode_captures_one_frame_then_hundred_frames(self, tmp_path):
        instance = self._make_motor(tmp_path)
        instance.capture_frame_variants = [(1, "_1frame"), (100, "_100frames")]

        with patch("motor.time.sleep"):
            name, path = instance.capture_image(350)

        calls = instance.imager.take_rpi_image.call_args_list
        assert [call.args[0] for call in calls] == [1, 100]
        assert calls[0].args[1] == f"{BASE}_1frame"
        assert calls[1].args[1] == f"{BASE}_100frames"
        assert [call.args[2] for call in calls] == [str(tmp_path), str(tmp_path)]
        assert all(call.kwargs["z_height"] == 350 for call in calls)
        assert all(call.kwargs["magnification"] == 40 for call in calls)

        assert (tmp_path / f"{BASE}_1frame.tif").exists()
        assert (tmp_path / f"{BASE}_1frame.json").exists()
        assert (tmp_path / f"{BASE}_100frames.tif").exists()
        assert (tmp_path / f"{BASE}_100frames.json").exists()

        # The web UI preview comes from the 100-frame image.
        instance.imager.update_latest_image_to_jpg.assert_called_once_with(
            str(tmp_path / f"{BASE}_100frames.tif")
        )
        assert (name, path) == (BASE, str(tmp_path))

    def test_default_mode_keeps_single_unsuffixed_hundred_frame_capture(self, tmp_path):
        instance = self._make_motor(tmp_path)

        with patch("motor.time.sleep") as sleep:
            name, path = instance.capture_image(350)

        call = instance.imager.take_rpi_image.call_args
        assert call.args[0] == 100
        assert call.args[1] == BASE
        sleep.assert_called_once_with(15)
        assert (name, path) == (BASE, str(tmp_path))
        instance.imager.update_latest_image_to_jpg.assert_called_once_with(
            str(tmp_path / f"{BASE}.tif")
        )

    def test_dual_mode_raises_when_camera_output_never_arrives(self, tmp_path):
        instance = self._make_motor(tmp_path)
        instance.capture_frame_variants = [(1, "_1frame"), (100, "_100frames")]
        instance.imager.take_rpi_image.side_effect = lambda *args, **kwargs: None

        with patch("motor.time.sleep"), \
             patch("motor.time.monotonic", side_effect=[0.0, 61.0]):
            with pytest.raises(TimeoutError, match="_1frame"):
                instance.capture_image(350)

        # The second variant is never requested after the first one times out.
        assert instance.imager.take_rpi_image.call_count == 1


class TestCollectDataManualFovDualFrame:
    def _make_motor(self):
        instance = motor.Motor.__new__(motor.Motor)
        instance.filename = MagicMock()
        instance.filename.barcode = "TEST001"
        instance.filename.date = "20260101"
        instance.logger = MagicMock()
        instance.collect_data_manual_fov = MagicMock()
        instance.capture_frame_variants = [(100, "")]
        instance.populate_zstack_json = True
        return instance

    def test_uses_dual_variants_and_skips_zstack_population_during_run(self):
        instance = self._make_motor()
        seen = {}

        def record(x_pos, y_pos, objectives, smear_id, fov_target, transfer):
            seen["variants"] = list(instance.capture_frame_variants)
            seen["populate"] = instance.populate_zstack_json

        instance.collect_data_manual_fov.side_effect = record

        instance.collect_data_manual_fov_dual_frame(130, 15, [20, 40], "SM1", 1, transfer=False)

        assert seen["variants"] == [(1, "_1frame"), (100, "_100frames")]
        assert seen["populate"] is False
        instance.collect_data_manual_fov.assert_called_once_with(
            130, 15, [20, 40], "SM1", 1, False
        )

    def test_restores_default_capture_settings_after_run(self):
        instance = self._make_motor()
        instance.collect_data_manual_fov_dual_frame(130, 15, [20], "SM1", 1)
        assert instance.capture_frame_variants == [(100, "")]
        assert instance.populate_zstack_json is True

    def test_restores_default_capture_settings_when_run_raises(self):
        instance = self._make_motor()
        instance.collect_data_manual_fov.side_effect = RuntimeError("stop requested")
        with pytest.raises(RuntimeError, match="stop requested"):
            instance.collect_data_manual_fov_dual_frame(130, 15, [20], "SM1", 1)
        assert instance.capture_frame_variants == [(100, "")]
        assert instance.populate_zstack_json is True


class TestZstackJsonPopulationGuard:
    def _make_motor(self, tmp_path):
        instance = motor.Motor.__new__(motor.Motor)
        instance.filename = MagicMock()
        instance.filename.smear_id = "SM1"
        instance.filename.data_path_generator.return_value = str(tmp_path)
        instance.imager = MagicMock()
        instance.logger = MagicMock()
        instance.check_stop = MagicMock()
        instance.move_carousel = MagicMock()
        instance.scan_z_axis_for_focus = MagicMock(
            return_value=(350, 10.0, [(349, 1.0), (350, 10.0), (351, 2.0)])
        )
        instance.focus_view = 1
        instance.obj = 40
        instance.current_x = 130
        instance.current_y = 15
        instance.zstack_folder_path = str(tmp_path)
        instance.points_before = 1
        instance.points_after = 1
        instance.capture_frame_variants = [(100, "")]
        instance.populate_zstack_json = True
        return instance

    def test_collect_data_with_10x_populates_by_default(self, tmp_path):
        instance = self._make_motor(tmp_path)
        with patch("motor.create_zstack_json"), \
             patch("motor.populate_zstack_json_from_folder") as populate:
            instance.collect_data_with_10x()
        populate.assert_called_once_with(str(tmp_path))
        instance.filename.image_cleanup.assert_called_once()

    def test_collect_data_with_10x_skips_population_but_still_cleans_up(self, tmp_path):
        instance = self._make_motor(tmp_path)
        instance.populate_zstack_json = False
        with patch("motor.create_zstack_json"), \
             patch("motor.populate_zstack_json_from_folder") as populate:
            instance.collect_data_with_10x()
        populate.assert_not_called()
        instance.filename.image_cleanup.assert_called_once_with(1, 40, 350, 130, 15, 1, 1)

    def test_complete_zstack_populates_by_default(self, tmp_path):
        instance = self._make_motor(tmp_path)
        with patch("motor.populate_zstack_json_from_folder") as populate:
            instance.complete_zstack([(349, 1.0), (350, 10.0), (351, 2.0)])
        populate.assert_called_once_with(str(tmp_path))

    def test_complete_zstack_skips_population_when_disabled(self, tmp_path):
        instance = self._make_motor(tmp_path)
        instance.populate_zstack_json = False
        with patch("motor.populate_zstack_json_from_folder") as populate:
            instance.complete_zstack([(349, 1.0), (350, 10.0), (351, 2.0)])
        populate.assert_not_called()


class TestImageCleanupDualFrameNames:
    def test_deletes_out_of_range_dual_pairs_and_keeps_in_range(self, tmp_path):
        with patch("config.MICROSCOPE_ID", "M1"), \
             patch("config.PI_IMAGE_DIR", str(tmp_path)):
            ft = FileTransfer5(logger=MagicMock(), run_date="20260101")
            ft.set_barcode("TEST001")
            ft.set_smear_id("SM1")
            folder = ft.data_path_generator(1, 40)
            os.makedirs(folder, exist_ok=True)

            base = "TEST001_20260101_M1_unstained_SM1_40x_1_130x_15y"

            def touch(name):
                with open(os.path.join(folder, name), "wb") as fh:
                    fh.write(b"x")

            # z_focus=350 with points_before=1, points_after=1 keeps 349..351.
            kept = []
            for z in (349, 350, 351):
                for suffix in ("", "_1frame", "_100frames"):
                    for ext in (".tif", ".json"):
                        name = f"{base}_{z}z{suffix}{ext}"
                        touch(name)
                        kept.append(name)

            removed = []
            for z in (340, 360):
                for suffix in ("", "_1frame", "_100frames"):
                    for ext in (".tif", ".json"):
                        name = f"{base}_{z}z{suffix}{ext}"
                        touch(name)
                        removed.append(name)

            ft.image_cleanup(1, 40, 350, 130, 15, 1, 1)

            remaining = os.listdir(folder)
            assert sorted(remaining) == sorted(kept)
            assert not any(name in remaining for name in removed)