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
import csv
import json

class FileTransfer5:
    def __init__(self, logger=print, run_date=None):
        self.base_file_path = None
        self.barcode = None
        self.date = run_date if run_date is not None else date.today().strftime("%Y%m%d")
        self.smear_id = None

        self.first_folder = None
        self.second_folder = None
        self.third_folder = None
        self.microscope_id = c.MICROSCOPE_ID
        self.csv_filename = None

        self.hostname = c.HOSTNAME_IP
        self.username = c.USERNAME
        self.port = c.PORT
        self.laptop_upload_dir = None

        self.pi_image_dir = c.PI_IMAGE_DIR

        self.ssh = None
        self.sftp = None

        self.logger = logger

        self.milestone_list = []

    def set_barcode(self, barcode):
        self.barcode = barcode

        self.first_folder = self.barcode
        self.second_folder = f"{self.barcode}_{self.date}"
        self.third_folder = f"{self.barcode}_{self.date}_{self.microscope_id}"

        self.csv_filename = f"{barcode}_10x_quality.csv"

    def set_smear_id(self, smear_id):
        self.smear_id = f"{smear_id}"

    # Appending to csv file
    def append_csv(self, x_coord: float, y_coord: float, z_coord: float, good_fov: bool | str):
        # 1. Resolve the CSV path
        csv_path = (
            Path(self.pi_image_dir) /
            self.first_folder /
            self.second_folder /
            self.third_folder /
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
            self.microscope_id,
            dt,
            good_fov_str,
        ]

        # 4. Append the row
        with csv_path.open(mode="a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(row)

    # Filename generator
    def data_filename_generator(self, focus_view, obj, x_pos, y_pos, z_pos):
        filename = f"{self.third_folder}_unstained_{self.smear_id}_{obj}x_{focus_view}_{x_pos}x_{y_pos}y_{z_pos}z"
        file_path = self.data_path_generator(focus_view, obj)
        return filename, file_path

    def background_filename_generator(self, obj):
        filename = f"no-slide_{self.date}_{self.microscope_id}_{obj}x"
        file_path = self.background_path_generator(obj)
        return filename, file_path

    def darkfield_filename_generator(self, obj):
        filename = f"no-light_{self.date}_{self.microscope_id}_{obj}x"
        file_path = self.darkfield_path_generator(obj)
        return filename, file_path

    def scanning_filename_generator(self, x_pos, y_pos, z_pos):
        time = datetime.now().time()
        filename = f"{self.barcode}_{time}_{self.date}_{self.microscope_id}_{self.smear_id}_{x_pos}x_{y_pos}y_{z_pos}z_fov_detection"
        return filename


    # File path generator
    def data_path_generator(self, focus_view, obj):
        data_folder = f"{self.third_folder}_unstained_{self.smear_id}_{obj}x_{focus_view}"
        file_path = os.path.join(self.pi_image_dir, self.first_folder, self.second_folder, self.third_folder, data_folder)
        self.logger(f"Image will be saved to: {file_path}")

        return file_path

    def failed_qc_path_generator(self, focus_view, obj):
        data_folder = f"{datetime.now()}_{self.third_folder}_unstained_{self.smear_id}_{obj}x_{focus_view}_FAILED_QC"
        file_path = os.path.join(self.pi_image_dir, self.first_folder, self.second_folder, self.third_folder, data_folder)
        self.logger(f"Creating new filepath for failed QC zstack: {file_path}")

        return file_path

    def background_path_generator(self, obj):
        background_folder = f"no-slide_{self.date}_{self.microscope_id}_{obj}x"
        file_path = os.path.join(self.pi_image_dir, f"no-slide_{self.date}_{self.microscope_id}", background_folder)
        self.logger(f"Background image saved to: {file_path}")

        return file_path

    def darkfield_path_generator(self, obj):
        darkfield_folder = f"no-light_{self.date}_{self.microscope_id}_{obj}x"
        file_path = os.path.join(self.pi_image_dir, f"no-light_{self.date}_{self.microscope_id}", darkfield_folder)
        self.logger(f"Darkfield image saved to: {file_path}")

        return file_path

    # Moving and finding images
    def move_scanning_images(self):
        pi_files = os.listdir(self.pi_image_dir)
        pattern = f"scanning_*"
        matching_files = fnmatch.filter(pi_files, pattern)

        for filename in matching_files:
            source_path = os.path.join(self.pi_image_dir, filename)
            destination_path = os.path.join(self.pi_image_dir, "scanning_image_archive", filename)

            if os.path.isfile(source_path):
                shutil.move(source_path, destination_path)
                print(f"Moved: {filename}")

    def extract_prefix(self, s):
        match = re.match(r"(WBC|RA|ID|M\d)", s)
        if not match:
            return None

        return match.group(1) or match.group(2)

    def get_rsync_path(self, milestone_prefix):
        if milestone_prefix == "M1":
            rsync_path = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_1/Data_Collection_7"
        elif milestone_prefix == "M2":
            rsync_path = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_2/Data_Collection_6"
        elif milestone_prefix == "M3":
            rsync_path = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_3/Data_Collection_7"
        elif milestone_prefix == "M7":
            rsync_path = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_7/Data_Collection_3"
        elif milestone_prefix == "M8":
            rsync_path = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_8/Data_Collection_1"
        elif milestone_prefix == "ID":
            rsync_path = f"/Volumes/{c.EXTERNAL_SSD}/ID/Data_Collection_4"
        elif milestone_prefix == "WBC":
            rsync_path = f"/Volumes/{c.EXTERNAL_SSD}/WBC/Data_Collection_1"
        else: # Milestone 5 and RA condition
            rsync_path = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_5/Data_Collection_10"

        if milestone_prefix not in self.milestone_list:
            self.milestone_list.append(milestone_prefix)
            print(self.milestone_list)

        return rsync_path

    def get_correction_rsync_path(self, milestone_prefix, corr_type):
        if corr_type not in ["no-slide", "no-light"]:
            raise ValueError("corr_type must be 'no-slide' or 'no-light'")

        if milestone_prefix == "RA":
            base = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_5/{corr_type}"
        elif milestone_prefix == "ID":
            base = f"/Volumes/{c.EXTERNAL_SSD}/ID/{corr_type}"
        elif milestone_prefix == "WBC":
            base = f"/Volumes/{c.EXTERNAL_SSD}/WBC/{corr_type}"
        else:
            milestone_number = milestone_prefix[1] if len(milestone_prefix) > 1 else "5"
            base = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_{milestone_number}/{corr_type}"

        return base

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
        base_path = Path(self.pi_image_dir)

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

        if not self.milestone_list:
            self.milestone_list = self.derive_milestones_from_log()

        success_all = True
        for folder_name in folders:
            correction_type = "no-slide" if folder_name.startswith("no-slide_") else "no-light"
            all_success = True

            for milestone in self.milestone_list:
                rsync_path = self.get_correction_rsync_path(milestone, correction_type)
                self.logger(f"Uploading correction folder {folder_name} to {rsync_path} (milestone {milestone})")
                transferred = self.upload_to_laptop_rsync(folder_name, rsync_path, delete_files=False)
                if not transferred:
                    all_success = False
                    self.logger(f"Failed to transfer {folder_name} to {rsync_path}")

            if all_success:
                local_folder = Path(self.pi_image_dir) / folder_name
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

            milestone_prefix = self.extract_prefix(folder_name)
            rsync_path = self.get_rsync_path(milestone_prefix)

            self.upload_to_laptop_rsync(folder_name, rsync_path, True)

        self.upload_background()
        self.upload_darkfield()

    #def save_all_data(self, folder_name_dict):
        #self.move_scanning_images()

        #self.upload_background()
        #self.upload_darkfield()

        #for entry in folder_name_dict:
            #folder_name = entry["folder_name"]
            #date = entry["date"]

            #self.upload_to_dropbox(folder_name, self.rclone_remote_zstack, True)
            #self.upload_to_laptop_rsync(folder_name, self.rsync_remote, True)

    def upload_background(self):
        pattern = "no-slide_*"
        pi_folders = os.listdir(self.pi_image_dir)

        matching_folders = fnmatch.filter(pi_folders, pattern)
        print(matching_folders)

        for folder in matching_folders:
            for milestone in self.milestone_list:
                print(f"Saving background images for Milestone: {milestone}")
                if milestone == "RA":
                    rsync_no_slide = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_5/no-slide"
                elif milestone == "ID":
                    rsync_no_slide = f"/Volumes/{c.EXTERNAL_SSD}/ID/no-slide"
                elif milestone == "WBC":
                    rsync_no_slide = f"/Volumes/{c.EXTERNAL_SSD}/WBC/no-slide"
                else:
                    milestone_number = milestone[1]
                    rsync_no_slide = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_{milestone_number}/no-slide"

                print(f"Saving background images to path: {rsync_no_slide}")

                if milestone == self.milestone_list[-1]:
                    self.upload_to_laptop_rsync(folder, rsync_no_slide, True)
                else:
                    self.upload_to_laptop_rsync(folder, rsync_no_slide, False)

    def upload_darkfield(self):
        pattern = "no-light_*"
        pi_folders = os.listdir(self.pi_image_dir)

        matching_folders = fnmatch.filter(pi_folders, pattern)
        print(matching_folders)

        for folder in matching_folders:
            for milestone in self.milestone_list:
                print(f"Saving darkfield images for Milestone: {milestone}")
                if milestone == "RA":
                    rsync_no_light = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_5/no-light"
                elif milestone == "ID":
                    rsync_no_light = f"/Volumes/{c.EXTERNAL_SSD}/ID/no-light"
                elif milestone == "WBC":
                    rsync_no_light = f"/Volumes/{c.EXTERNAL_SSD}/WBC/no-light"
                else:
                    milestone_number = milestone[1]
                    rsync_no_light = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_{milestone_number}/no-light"

                print(f"Saving darkfield images to path: {rsync_no_light}")

                if milestone == self.milestone_list[-1]:
                    self.upload_to_laptop_rsync(folder, rsync_no_light, True)
                else:
                    self.upload_to_laptop_rsync(folder, rsync_no_light, False)

    def upload_to_laptop_rsync(self, folder_name, remote_path, delete_files = False):
        local_path = Path(self.pi_image_dir) / folder_name
        if not local_path.exists():
            self.logger(f"Folder {folder_name} does not exist in {self.pi_image_dir}.")
            return False

        remote = f"{self.username}@{self.hostname}:{remote_path}"
        rsync_cmd = ["rsync", "-avz", str(local_path), remote]

        self.logger(f"Starting rsync to laptop: {rsync_cmd}")
        try:
            subprocess.run(rsync_cmd, check=True)
            self.logger(f"Successfully transfered {folder_name} to laptop")

            if delete_files:
                shutil.rmtree(local_path)
                self.logger(f"Deleted local folder {folder_name} after transfer")
            return True
        except subprocess.CalledProcessError as e:
            self.logger(f"Error during rsync copy: {e}")
            return False

    def upload_to_dropbox(self, folder_name, remote_path, delete_files = False):
        local_path = Path(self.pi_image_dir) / folder_name
        if not local_path.exists():
            self.logger(f"Folder {folder_name} does not exist in {self.pi_image_dir}.")
            return False

        rclone_cmd = ["rclone", "copy", str(local_path), f"{remote_path}/{folder_name}", "--create-empty-src-dirs", "--progress"]
        self.logger(f"Starting rclone copy to Dropbox: {rclone_cmd}")
        try:
            subprocess.run(rclone_cmd, check=True)
            self.logger(f"Successfully copied {folder_name} to Dropbox")

            if delete_files:
                shutil.rmtree(local_path)
                self.logger(f"Deleted local folder {local_path}")
            return True
        except subprocess.CalledProcessError as e:
            self.logger(f"Error during rclone copy: {e}")
            return False

    def image_cleanup(self, focus_view, obj, z_focus, current_x, current_y, points_before, points_after):
        self.logger("Removing extra images from zstack")
        keep_range = range(z_focus - points_before, z_focus + points_after + 1)

        folder_path = self.data_path_generator(focus_view, obj)
        pi_files = os.listdir(folder_path)
        pattern = f"{self.barcode}_{self.date}_{self.microscope_id}_unstained_{self.smear_id}_{obj}x_{focus_view}_{current_x}x_{current_y}y_*.*"
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
    #file.upload_to_dropbox("M5AAAA", c.RCLONE_REMOTE_ZSTACK)
    #file.upload_to_laptop_rsync("M5AAAA", True)

    #file.set_barcode("M5I2UQ")
    #file.set_smear_id("SM2")
    #file.image_cleanup(1, 40, 393, 130, 13, 15, 5)

    #rsync_remote = c.RSYNC_REMOTE
    #file.upload_to_laptop_rsync("M5RCT6", rsync_remote, True)

    path = f"/Volumes/{c.EXTERNAL_SSD}/Milestone_8/no-slide"
    file.upload_to_laptop_rsync("no-slide_20260422_M1", path, False)
