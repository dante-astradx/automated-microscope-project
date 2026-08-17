from datetime import date, datetime
import config as c
import os
import paramiko
import fnmatch
import re
import subprocess
import time
import shutil
import logging
from pathlib import Path
from microscope_log import log_output, log_to_file_only, update_status
from folder_name_logger import clear_log
from json_handler import append_correction_metadata_to_manifest
import csv
import json

class FileTransfer5:
    def __init__(self, logger=print, run_date=None):
        self.base_file_path = None
        self.barcode = None
        self.date = run_date if run_date is not None else date.today().strftime("%Y%m%d")
        self.smear_id = None

        self.slide_case_folder = None
        self.csv_filename = None

        self.hostname = c.HOSTNAME_IP
        self.username = c.USERNAME
        self.port = c.PORT
        self.laptop_upload_dir = None

        self.ssh = None
        self.sftp = None

        self.logger = logger

        self.milestone_list = []

    def set_barcode(self, barcode):
        self.barcode = barcode

        self.slide_case_folder = f"{self.barcode}_{self.date}_{c.MICROSCOPE_ID}"
        self.csv_filename = f"{barcode}_10x_quality.csv"

    def set_smear_id(self, smear_id):
        self.smear_id = f"{smear_id}"

    # Appending to csv file
    def append_csv(self, x_coord: float, y_coord: float, z_coord: float, good_fov: bool | str):
        # 1. Resolve the CSV path
        csv_path = (
            Path(c.PI_IMAGE_DIR) /
            self.slide_case_folder /
            self.csv_filename
        )

        dt = datetime.now().isoformat(timespec="seconds")

        # Normalise the "Good FOV?" value – we keep whatever the user passes
        good_fov_str = str(good_fov)

        row = [
            str(x_coord),
            str(y_coord),
            str(z_coord),
            self.smear_id,
            c.MICROSCOPE_ID,
            dt,
            good_fov_str,
        ]

        # 4. Append the row
        with csv_path.open(mode="a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(row)

    # Filename generator
    def data_filename_generator(self, focus_view, obj, x_pos, y_pos, z_pos):
        filename = f"{self.slide_case_folder}_unstained_{self.smear_id}_{obj}x_{focus_view}_{x_pos}x_{y_pos}y_{z_pos}z"
        file_path = self.data_path_generator(focus_view, obj)
        return filename, file_path

    def background_filename_generator(self, obj):
        filename = f"no-slide_{self.date}_{c.MICROSCOPE_ID}_{obj}x"
        file_path = os.path.join(c.PI_IMAGE_DIR, f"no-slide_{self.date}_{c.MICROSCOPE_ID}")
        return filename, file_path

    def darkfield_filename_generator(self, obj):
        filename = f"no-light_{self.date}_{c.MICROSCOPE_ID}_{obj}x"
        file_path = os.path.join(c.PI_IMAGE_DIR, f"no-light_{self.date}_{c.MICROSCOPE_ID}")
        return filename, file_path

    def scanning_filename_generator(self, x_pos, y_pos, z_pos):
        time = datetime.now().time()
        filename = f"{self.barcode}_{time}_{self.date}_{c.MICROSCOPE_ID}_{self.smear_id}_{x_pos}x_{y_pos}y_{z_pos}z_fov_detection"
        return filename

    # File path generator
    def data_path_generator(self, focus_view, obj):
        data_folder = f"{self.slide_case_folder}_unstained_{self.smear_id}_{obj}x_{focus_view}"
        file_path = os.path.join(c.PI_IMAGE_DIR, self.slide_case_folder, data_folder)

        return file_path

    def failed_qc_path_generator(self, focus_view, obj):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        data_folder = f"{timestamp}_{self.slide_case_folder}_unstained_{self.smear_id}_{obj}x_{focus_view}_FAILED_QC"
        file_path = os.path.join(c.PI_IMAGE_DIR, self.slide_case_folder, data_folder)
        self.logger(f"Creating new filepath for failed QC zstack: {file_path}")

        return file_path

    # Moving and finding images
    def move_scanning_images(self):
        pi_files = os.listdir(c.PI_IMAGE_DIR)
        pattern = f"scanning_*"
        matching_files = fnmatch.filter(pi_files, pattern)

        for filename in matching_files:
            source_path = os.path.join(c.PI_IMAGE_DIR, filename)
            destination_path = os.path.join(c.PI_IMAGE_DIR, "scanning_image_archive", filename)

            if os.path.isfile(source_path):
                shutil.move(source_path, destination_path)
                print(f"Moved: {filename}")

    def extract_prefix(self, s):
        match = re.match(r"(WBC|RA|ID|M\d)", s)
        if not match:
            return None

        return match.group(1) or match.group(2)


    def get_correction_rsync_path(self, corr_type):
        if corr_type not in ["no-slide", "no-light"]:
            raise ValueError("corr_type must be 'no-slide' or 'no-light'")

        if corr_type == "no-slide":
            remote_path = f"{c.REMOTE_RSYNC_PATH}/no-slide"
        if corr_type == "no-light":
            remote_path = f"{c.REMOTE_RSYNC_PATH}/no-light"

        return remote_path

    def derive_milestones_from_log(self):
        try:
            from folder_name_logger import log as folder_name_log
        except ImportError:
            folder_name_log = []

        milestone_set = set()
        for entry in folder_name_log:
            folder_name = entry.get("folder_name", "")
            prefix = self.extract_prefix(folder_name)
            if prefix:
                milestone_set.add(prefix)

        # If no data found, fall back to default milestone
        if not milestone_set:
            milestone_set = {"M5"}

        return sorted(milestone_set)

    def get_old_correction_folders(self, date=None):
        if date is None:
            date = datetime.today().strftime("%Y%m%d")

        folders = []
        base_path = Path(c.PI_IMAGE_DIR)

        for child in base_path.iterdir():
            if not child.is_dir():
                continue

            if child.name.startswith("no-slide_") or child.name.startswith("no-light_"):
                # folder name format: no-slide_YYYYMMDD_M1
                parts = child.name.split("_")
                if len(parts) >= 3 and parts[1] != date:
                    folders.append(child.name)

        return folders

    def upload_previous_correction_images(self, date=None):
        folders = self.get_old_correction_folders(date)

        if not folders:
            self.logger("No previous correction folders found to transfer.")
            return True

        success_all = True
        for folder_name in folders:
            correction_type = "no-slide" if folder_name.startswith("no-slide_") else "no-light"
            all_success = True

            rsync_path = self.get_correction_rsync_path(correction_type)

            self.logger(f"Uploading correction folder {folder_name} to {rsync_path}")
            folder_path = os.path.join(c.PI_IMAGE_DIR, folder_name)
            transferred = self.upload_to_network(folder_path, rsync_path, delete_files=False)

            if not transferred:
                all_success = False
                self.logger(f"Failed to transfer {folder_name} to {rsync_path}")

            if all_success:
                local_folder = Path(c.PI_IMAGE_DIR) / folder_name
                try:
                    shutil.rmtree(local_folder)
                    self.logger(f"Deleted local correction folder {folder_name} after successful transfer")
                except Exception as e:
                    self.logger(f"Could not delete local correction folder {folder_name}: {e}")
                    success_all = False
            else:
                success_all = False

        clear_log()
        return success_all

    def save_all_data(self, folder_name_dict):
        self.move_scanning_images()

        for entry in folder_name_dict:
            folder_name = entry["folder_name"]
            date = entry["date"]

            folder_path = os.path.join(c.PI_IMAGE_DIR, folder_name)
            self.upload_to_network(folder_path, c.REMOTE_RSYNC_PATH, delete_files=True)

        self.upload_background()
        self.upload_darkfield()

    def upload_background(self):
        pattern = "no-slide_*"
        pi_folders = os.listdir(c.PI_IMAGE_DIR)

        matching_folders = fnmatch.filter(pi_folders, pattern)
        print(matching_folders)

        rsync_path = self.get_correction_rsync_path("no-slide")

        for folder in matching_folders:
            self.logger(f"Saving background images to path: {rsync_path}")
            folder_path = os.path.join(c.PI_IMAGE_DIR, folder)
            self.upload_to_network(folder_path, rsync_path, delete_files=True)

    def upload_darkfield(self):
        pattern = "no-light_*"
        pi_folders = os.listdir(c.PI_IMAGE_DIR)

        matching_folders = fnmatch.filter(pi_folders, pattern)
        print(matching_folders)

        rsync_path = self.get_correction_rsync_path("no-light")

        for folder in matching_folders:
            self.logger(f"Saving darkfield images to path: {rsync_path}")
            folder_path = os.path.join(c.PI_IMAGE_DIR, folder)
            self.upload_to_network(folder_path, rsync_path, delete_files=True)

    def copy_correction_folders_to_slide_case(self):
        no_slide_src = os.path.join(c.PI_IMAGE_DIR, f"no-slide_{self.date}_{c.MICROSCOPE_ID}")
        no_light_src = os.path.join(c.PI_IMAGE_DIR, f"no-light_{self.date}_{c.MICROSCOPE_ID}")

        manifest_dest = os.path.join(c.PI_IMAGE_DIR, self.slide_case_folder)
        no_slide_dest = os.path.join(manifest_dest, f"no-slide_{self.date}_{c.MICROSCOPE_ID}")
        no_light_dest = os.path.join(manifest_dest, f"no-light_{self.date}_{c.MICROSCOPE_ID}")

        manifest_json_path = os.path.join(manifest_dest, "manifest.json")
        no_slide_json_path = os.path.join(no_slide_dest, "no-slide.json")
        no_light_json_path = os.path.join(no_light_dest, "no-light.json")

        # no-slide
        shutil.copytree(no_slide_src, no_slide_dest, dirs_exist_ok=True)
        append_correction_metadata_to_manifest("no-slide", manifest_json_path, no_slide_json_path)

        # no-light
        shutil.copytree(no_light_src, no_light_dest, dirs_exist_ok=True)
        append_correction_metadata_to_manifest("no-light", manifest_json_path, no_light_json_path)

    def upload_to_network(self, folder_absolute_path, rsync_path, delete_files=False, sentinel=False):
        folder_name = os.path.basename(folder_absolute_path)
        if not os.path.exists(folder_absolute_path):
            self.logger(f"Folder {folder_absolute_path} does not exist.")
            return False

        remote_path = f"{self.username}@{self.hostname}:{rsync_path}"
        rsync_cmd = ["rsync", "-avz", str(folder_absolute_path), remote_path]

        self.logger(f"Starting rsync to network: {rsync_cmd}")
        try:
            subprocess.run(rsync_cmd, check=True)
            self.logger(f"Successfully transfered {folder_name} to network")

            if delete_files:
                shutil.rmtree(folder_absolute_path)
                self.logger(f"Deleted local folder {folder_name} after transfer")

            if sentinel:
                touch_cmd = ["ssh", f"{self.username}@{self.hostname}", f"touch {rsync_path}/{folder_name}/.transfer_complete"]
                self.logger(f"Sending sentinel file...")
                try:
                    subprocess.run(touch_cmd, check=True)
                    self.logger(f"Sentinel file sent to {rsync_path}/{folder_name}/.transfer_complete")
                except subprocess.CalledProcessError as e:
                    self.logger(f"Error sending sentinel file: {e}")

            return True
        except subprocess.CalledProcessError as e:
            self.logger(f"Error during rsync copy: {e}")
            return False

    def image_cleanup(self, focus_view, obj, z_focus, current_x, current_y, points_before, points_after):
        self.logger("Removing extra images from zstack")
        keep_range = range(z_focus - points_before, z_focus + points_after + 1)

        folder_path = self.data_path_generator(focus_view, obj)
        pi_files = os.listdir(folder_path)
        pattern = f"{self.barcode}_{self.date}_{c.MICROSCOPE_ID}_unstained_{self.smear_id}_{obj}x_{focus_view}_{current_x}x_{current_y}y_*.*"
        matching_files = fnmatch.filter(pi_files, pattern)
        if not matching_files:
            print("Error: no files found to delete")

        for filename in matching_files:
            parts = filename.rsplit("_", maxsplit=3)
            try:
                z_part = os.path.splitext(parts[-1])[0]  # removes '.tif' or '.json'
                z = int(z_part.rstrip("z"))  # strip trailing 'z'
            except (ValueError, IndexError):
                continue  # skip malformed filenames

            if z not in keep_range:
                file_path = os.path.join(folder_path, filename)
                os.remove(file_path)
                self.logger(f"Deleted: {filename}")

if __name__ == "__main__":
    pass
    file = FileTransfer5()

    #--- Test File Transfer ---
    #file.upload_background()
    #file.upload_darkfield()
    #file.upload_to_network("M5AAAA", True)

    #file.set_barcode("M5I2UQ")
    #file.set_smear_id("SM2")
    #file.image_cleanup(1, 40, 393, 130, 13, 15, 5)

    #rsync_remote = c.RSYNC_REMOTE
    #file.upload_to_network("M5RCT6", rsync_remote, True)

    path = f"{c.REMOTE_RSYNC_PATH}"
    file.upload_to_network(f"{c.PI_IMAGE_DIR}/TEST019_20260817_M1", path, False, True)
