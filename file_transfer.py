from datetime import date
import config as c
import os
import paramiko
import fnmatch
import re
import subprocess
import time
import shutil
from microscope_log import log_output, log_to_file_only, update_status

class FileTransfer:
    def __init__(self, logger=print):
        self.base_file_path = None
        self.code = None
        self.slide_date = None
        self.todays_date = date.today().strftime("%Y%m%d")

        self.first_folder = None
        self.second_folder = None
        self.third_folder = None

        self.hostname = c.HOSTNAME_IP
        self.username = c.USERNAME
        self.port = c.PORT
        self.laptop_upload_dir = None
        self.pi_image_dir = c.PI_IMAGE_DIR

        self.ssh = None
        self.sftp = None

        self.logger = logger

    def set_filename(self, folder_name, date = None):
        self.third_folder = folder_name
        file_name_parts = folder_name.split("_")

        self.code = file_name_parts[0]
        self.slide_date = file_name_parts[1]
        self.antibiotic = file_name_parts[2]
        self.concentration = file_name_parts[3]
        self.well = file_name_parts[4]
        self.slide_number = file_name_parts[5]
        self.microscope_id = file_name_parts[6]

        self.first_folder = self.code

        if date is not None:
            self.second_folder = f"{self.code}_{date}"
        else:
            self.second_folder = f"{self.code}_{self.todays_date}"

    # Filename generator
    def data_filename_generator(self, focus_view, obj, x_pos, y_pos, z_pos):
        insert_part = f"{focus_view}_{obj}x_unstained"
        first_part, rest = self.third_folder.split("_", 1)
        filename = f"{first_part}_{insert_part}_{rest}_{x_pos}x_{y_pos}y_{z_pos}z"
        return filename

    def background_filename_generator(self, obj):
        filename = f"{self.code}_{obj}x_background_{self.todays_date}_{self.microscope_id}"
        return filename

    def darkfield_filename_generator(self, obj):
        filename = f"{self.code}_{obj}x_darkfield_{self.todays_date}_{self.microscope_id}"
        return filename

    def scanning_filename_generator(self, x_pos, y_pos, z_pos):
        filename = f"scanning_{self.third_folder}_{x_pos}x_{y_pos}y_{z_pos}z"
        return filename

    # File path generator
    def data_path_generator(self, index : str):
        insert_part = f"{index}_unstained"
        first_part, rest = self.third_folder.split("_", 1)
        data_folder = f"{first_part}_{insert_part}_{rest}"
        laptop_upload_dir = os.path.join(c.LAPTOP_UPLOAD_DIR, self.first_folder, self.second_folder, self.third_folder, data_folder) 
        self.logger(f"Sending file to: {laptop_upload_dir}")
        return laptop_upload_dir

    def background_path_generator(self, obj : str, date = None):
        if date is not None:
            background_folder = f"no-slide_{obj}_{date}_{self.microscope_id}"
        else:
            background_folder = f"no-slide_{obj}_{self.todays_date}_{self.microscope_id}"

        laptop_upload_dir = os.path.join(c.LAPTOP_UPLOAD_DIR, self.first_folder, self.second_folder, background_folder)
        self.logger(f"Sending background images to: {laptop_upload_dir}")
        return laptop_upload_dir

    def darkfield_path_generator(self, obj : str, date = None):
        if date is not None:
            darkfield_folder = f"no-light_{obj}_{date}_{self.microscope_id}"
        else:
            darkfield_folder = f"no-light_{obj}_{self.todays_date}_{self.microscope_id}"

        laptop_upload_dir = os.path.join(c.LAPTOP_UPLOAD_DIR, self.first_folder, self.second_folder, darkfield_folder)
        self.logger(f"Sending darkfield images to: {laptop_upload_dir}")
        return laptop_upload_dir

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

    def save_all_data(self, folder_name_dict):
        # move all "scanning" images to archive folder
        self.move_scanning_images()

        for entry in folder_name_dict:
            folder_name = entry["folder_name"]
            date = entry["date"]

            self.set_filename(folder_name, date)

            self.save_background(date)
            self.save_darkfield(date)
            self.save_data()

    # Saving images
    def save_data(self):
        first_part, rest = self.third_folder.split("_", 1)
        pattern = f"{first_part}_*_{rest}_*.*"
        pi_files = os.listdir(self.pi_image_dir)

        matching_files = fnmatch.filter(pi_files, pattern)

        for filename in matching_files:
            match = re.search(r'(\d+_(10x|20x|40x))', filename)
            if not match:
                print(f"Identifier not found in {filename}")
                continue
            identifier = match.group(1)
            print(f"The identifier is: {identifier}")

            laptop_upload_dir = self.data_path_generator(identifier)
            self.upload_to_laptop_rsync(filename, laptop_upload_dir, True)

    def save_background(self, date):
        pattern = f"{self.code}_*_background_{date}_{self.microscope_id}.*"
        pi_files = os.listdir(self.pi_image_dir)

        matching_files = fnmatch.filter(pi_files, pattern)

        for filename in matching_files:
            match = re.search(r'_(10x|20x|40x)_', filename)
            if not match:
                print(f"Identifier not found in {filename}")
                continue
            identifier = match.group(1)
            print(f"The identifier is: {identifier}")

            laptop_upload_dir = self.background_path_generator(identifier, date)
            self.upload_to_laptop_rsync(filename, laptop_upload_dir, True)

    def save_darkfield(self, date):
        pattern = f"{self.code}_*_darkfield_{date}_{self.microscope_id}.*"
        pi_files = os.listdir(self.pi_image_dir)

        matching_files = fnmatch.filter(pi_files, pattern)

        laptop_upload_dir = self.darkfield_path_generator(date)
        for filename in matching_files:
            match = re.search(r'_(10x|20x|40x)_', filename)
            if not match:
                print(f"Identifier not found in {filename}")
                continue
            identifier = match.group(1)
            print(f"The identifier is: {identifier}")

            laptop_upload_dir = self.darkfield_path_generator(identifier, date)
            self.upload_to_laptop_rsync(filename, laptop_upload_dir, True)

    def upload_to_laptop_rsync(self, filename, laptop_upload_dir, delete_after_transfer=False):
        local_path = os.path.join(self.pi_image_dir, filename)
        remote_path = os.path.join(laptop_upload_dir, filename)

        print(f"Uploading with rsync: local_path = {local_path}, remote_path = {remote_path}")

        remote = f"{self.username}@{self.hostname}:{remote_path}"

        result = subprocess.run(["rsync", "-avz", local_path, remote])

        if result.returncode == 0:
            self.logger(f"Transferred {filename} to {remote_path}")
            if delete_after_transfer:
                os.remove(local_path)
                self.logger(f"Deleted {filename} from Pi")
        else:
            self.logger(f"rsync failed with code {result.returncode}")

    def upload_to_dante_laptop(self, filename_pattern):
        laptop_upload_dir = "/Users/dantemuzila"
        pi_files = os.listdir(self.pi_image_dir)
        pattern = filename_pattern
        matching_files = fnmatch.filter(pi_files, pattern)

        for filename in matching_files:
            local_path = os.path.join(self.pi_image_dir, filename)
            remote_path = os.path.join(laptop_upload_dir, filename)

            remote = f"dantemuzila@192.168.50.3:{remote_path}"
            result = subprocess.run(["rsync", "-avz", local_path, remote])

            if result.returncode == 0:
                print("Success")
            else:
                print(f"rsync failed with code {result.returncode}")

    def image_cleanup(self, focus_view, obj, z_focus, current_x, current_y, points_before, points_after):
        self.logger("Removing extra images from zstack")
        keep_range = range(z_focus - points_before, z_focus + points_after + 1)

        pi_files = os.listdir(self.pi_image_dir)
        pattern = f"{self.code}_{focus_view}_{obj}x_*_{self.slide_date}_{self.antibiotic}_{self.concentration}_{self.well}_*_{current_x}x_{current_y}y_*.*"
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
                file_path = os.path.join(self.pi_image_dir, filename)
                os.remove(file_path)
                self.logger(f"Deleted: {filename}")

if __name__ == "__main__":
    pass
    #file = FileTransfer()
    #file.get_filename()
    #laptop_dir = "/Users/dantemuzila"
    #pi_image_dir = "/home/microscope_auto/Images"
    #pattern = f"scanning_AR0020_20250724_F2_*.tif"
    #pi_files = os.listdir(pi_image_dir)

    #matching_files = fnmatch.filter(pi_files, pattern)
    #print(matching_files)
    #print("hello")
    #for filename in matching_files:
        #file.upload_to_laptop_rsync(filename, laptop_dir, True)
