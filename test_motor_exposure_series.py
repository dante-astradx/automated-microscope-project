"""Tests for manually collecting single-frame exposure series."""

import pytest
from unittest.mock import MagicMock, patch

import motor


class TestManualFovExposureSeries:
    def _make_motor(self, tmp_path):
        instance = motor.Motor.__new__(motor.Motor)
        instance.filename = MagicMock()
        instance.filename.barcode = "TEST001"
        instance.filename.date = "20260101"
        instance.filename.data_filename_generator.return_value = (
            "TEST001_20260101_M1_unstained_SM1_40x_1_130x_15y_350z",
            str(tmp_path),
        )
        instance.imager = MagicMock()
        instance.logger = MagicMock()
        instance.check_stop = MagicMock()
        instance.start_imaging = MagicMock()
        instance.stop_imaging = MagicMock()
        instance.set_smear_id = MagicMock()
        instance.move_carousel = MagicMock()
        instance.move_x_axis = MagicMock()
        instance.move_y_axis = MagicMock()
        instance.move_z_axis = MagicMock()

        def capture_one_frame(nframes, filename, file_path, **kwargs):
            assert nframes == 1
            (tmp_path / f"{filename}.tif").write_bytes(b"tif")
            (tmp_path / f"{filename}.json").write_text("{}")

        instance.imager.take_rpi_image.side_effect = capture_one_frame
        return instance

    def test_captures_100_one_frame_images_with_suffixes_and_metadata(self, tmp_path):
        instance = self._make_motor(tmp_path)
        with patch("motor.generate_barcode_folders") as generate_folders, \
             patch("motor.create_manifest_json") as create_manifest, \
             patch("motor.time.sleep"):
            names = instance.collect_exposure_series_manual_fov(
                130, 15, 350, 40, "SM1", 1
            )

        assert len(names) == 100
        assert names[0].endswith("_image1")
        assert names[-1].endswith("_image100")
        assert instance.imager.take_rpi_image.call_count == 100
        assert all(call.args[0] == 1 for call in instance.imager.take_rpi_image.call_args_list)
        assert all(call.kwargs["z_height"] == 350 for call in instance.imager.take_rpi_image.call_args_list)
        assert all(call.kwargs["magnification"] == 40 for call in instance.imager.take_rpi_image.call_args_list)
        generate_folders.assert_called_once_with(
            "TEST001", ["SM1"], [1], run_date="20260101"
        )
        create_manifest.assert_called_once_with(instance.filename)
        instance.move_carousel.assert_called_once_with("3")
        instance.move_x_axis.assert_not_called()
        instance.move_y_axis.assert_not_called()
        instance.move_z_axis.assert_not_called()
        instance.stop_imaging.assert_called_once()

    def test_twenty_x_selects_twenty_x_carousel_position(self, tmp_path):
        instance = self._make_motor(tmp_path)
        with patch("motor.generate_barcode_folders"), \
             patch("motor.create_manifest_json"), \
             patch("motor.time.sleep"):
            instance.collect_exposure_series_manual_fov(
                130, 15, 350, 20, "SM1", 1
            )
        instance.move_carousel.assert_called_once_with("2")

    def test_rejects_unsupported_magnification_without_starting_imaging(self, tmp_path):
        instance = self._make_motor(tmp_path)
        with pytest.raises(ValueError, match="20x and 40x"):
            instance.collect_exposure_series_manual_fov(
                130, 15, 350, 10, "SM1", 1
            )
        instance.start_imaging.assert_not_called()


class TestCollectMotionBlurData(TestManualFovExposureSeries):
    def _make_motion_blur_motor(self, tmp_path):
        instance = self._make_motor(tmp_path)
        instance.filename.data_path_generator.return_value = str(tmp_path)
        instance.filename.data_filename_generator.side_effect = (
            lambda fov, objective, x, y, z: (
                f"TEST001_20260101_M1_unstained_SM2_{objective}x_{fov}_"
                f"{x}x_{y}y_{z}z",
                str(tmp_path),
            )
        )

        def capture_multiple_frames(nframes, filename, file_path, **kwargs):
            assert nframes > 0
            (tmp_path / f"{filename}.tif").write_bytes(b"tif")
            (tmp_path / f"{filename}.json").write_text('{"frames": %d}' % nframes)

        instance.imager.take_rpi_image.side_effect = capture_multiple_frames
        return instance

    def test_captures_requested_accumulations_and_applies_exposure_after_objective(self, tmp_path):
        instance = self._make_motion_blur_motor(tmp_path)
        call_order = []
        instance.move_carousel.side_effect = lambda position: call_order.append(("carousel", position))
        instance.imager.set_exposure_time.side_effect = lambda value: call_order.append(("exposure", value))

        with patch("motor.generate_barcode_folders") as generate_folders, \
             patch("motor.create_manifest_json") as create_manifest, \
             patch("motor.create_zstack_json") as create_zstack, \
             patch("motor.time.sleep"):
            names = instance.collect_motion_blur_data(
                x_pos=130,
                y_pos=15,
                z_pos=350,
                magnification=40,
                smear_id="SM2",
                fov_number=2,
                nframes=4,
                image_count=3,
                exposure_time=25000,
            )

        assert len(names) == 3
        assert names[0].endswith("_image1")
        assert names[-1].endswith("_image3")
        assert instance.imager.take_rpi_image.call_count == 3
        assert all(call.args[0] == 4 for call in instance.imager.take_rpi_image.call_args_list)
        assert all(call.kwargs["z_height"] == 350 for call in instance.imager.take_rpi_image.call_args_list)
        assert all(call.kwargs["magnification"] == 40 for call in instance.imager.take_rpi_image.call_args_list)
        assert call_order == [("carousel", "3"), ("exposure", 25000)]
        generate_folders.assert_called_once_with(
            "TEST001", ["SM2"], [2], run_date="20260101"
        )
        create_manifest.assert_called_once_with(instance.filename)
        create_zstack.assert_called_once_with(str(tmp_path), 130, 15, 2, 40, "SM2")
        assert (tmp_path / f"{names[0]}.json").exists()

    def test_omitted_coordinates_use_na_in_names_and_skip_exposure_override(self, tmp_path):
        instance = self._make_motion_blur_motor(tmp_path)
        with patch("motor.generate_barcode_folders"), \
             patch("motor.create_manifest_json"), \
             patch("motor.create_zstack_json") as create_zstack, \
             patch("motor.time.sleep"):
            names = instance.collect_motion_blur_data(
                smear_id="SM2", image_count=1
            )

        assert "_NAx_NAy_NAz_image1" in names[0]
        assert instance.imager.take_rpi_image.call_args.args[0] == 1
        instance.imager.set_exposure_time.assert_not_called()
        create_zstack.assert_called_once_with(
            str(tmp_path), None, None, 1, 40, "SM2"
        )
        assert (tmp_path / f"{names[0]}.json").read_text() == '{"frames": 1}'

    def test_rejects_invalid_capture_parameters_before_imaging(self, tmp_path):
        instance = self._make_motion_blur_motor(tmp_path)
        with pytest.raises(ValueError, match="nframes"):
            instance.collect_motion_blur_data(smear_id="SM2", nframes=0)
        instance.start_imaging.assert_not_called()