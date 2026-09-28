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