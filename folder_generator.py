from pathlib import Path
import shutil
import os
from datetime import datetime
import config as c
import csv

microscope_id = c.MICROSCOPE_ID

def generate_barcode_folders(barcode: str, smear_list, fovs, run_date=None):

    # Create folder structure for given barcode.
    # Returns path to the microscope-level directory.

    # Home directory for the pi user
    home_dir = Path(c.PI_IMAGE_DIR)

    # Step 1: Root barcode folder
    root_dir = home_dir / barcode
    root_dir.mkdir(parents=True, exist_ok=True)

    # Step 2: Date subfolder
    today = run_date if run_date is not None else datetime.today().strftime("%Y%m%d")
    date_dir = root_dir / f"{barcode}_{today}"
    date_dir.mkdir(parents=True, exist_ok=True)

    # Step 3: Microscope-specific subfolder
    microscope_dir = date_dir / f"{barcode}_{today}_{microscope_id}"
    microscope_dir.mkdir(parents=True, exist_ok=True)

    # Step 4: Subfolders for SM1–SM3 and objectives 10,20,40 and fovs (1, 2, 3, ect...)
    smear_ids = smear_list
    #objectives = [10, 20, 40]
    objectives = [40]

    for smear_id, fov_count in zip(smear_ids, fovs):
        for obj in objectives:
            for fov in range(1, fov_count + 1):
                folder_name = f"{barcode}_{today}_{microscope_id}_unstained_{smear_id}_{obj}x_{fov}"
                subfolder = microscope_dir / folder_name
                subfolder.mkdir(parents=True, exist_ok=True)

    csv_name, csv_path = create_quality_csv(barcode, microscope_dir)

    return microscope_dir

def create_quality_csv(barcode, directory):
    # 1. Verify that the target directory exists
    if not directory.is_dir():
        raise FileNotFoundError(
            f"Microscope directory '{microscope_dir}' does not exist."
        )

    # 2. Build the destination path
    csv_name = f"{barcode}_10x_quality.csv"
    csv_path = directory / csv_name

    # 3. Define header columns
    header = [
        "x_coord",
        "y_coord",
        "z_coord",
        "Smear ID",
        "Microscope ID",
        "date/time",
        "Good FOV?",
    ]

    # 4. Write the header to the file
    with csv_path.open(mode="w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)

    return csv_name, csv_path

def generate_background_folders():
    home_dir = Path(c.PI_IMAGE_DIR)
    today = datetime.today().strftime("%Y%m%d")

    root_dir = home_dir / f"no-slide_{today}_{microscope_id}"
    root_dir.mkdir(parents=True, exist_ok=True)

    objectives = [10, 20, 40]
    for obj in objectives:
        folder_name = f"no-slide_{today}_{microscope_id}_{obj}x"
        subfolder = root_dir / folder_name
        subfolder.mkdir(parents=True, exist_ok=True)

def generate_darkfield_folders():
    home_dir = Path(c.PI_IMAGE_DIR)
    today = datetime.today().strftime("%Y%m%d")

    root_dir = home_dir / f"no-light_{today}_{microscope_id}"
    root_dir.mkdir(parents=True, exist_ok=True)

    objectives = [10, 20, 40]
    for obj in objectives:
        folder_name = f"no-light_{today}_{microscope_id}_{obj}x"
        subfolder = root_dir / folder_name
        subfolder.mkdir(parents=True, exist_ok=True)

def delete_barcode_folders(barcode: str):

    # Delete the entire folder structure for a given barcode.
    # Returns True if successful, False if not found.

    home_dir = Path(c.PI_IMAGE_DIR)
    root_dir = home_dir / barcode

    if root_dir.exists():
        try:
            shutil.rmtree(root_dir)
            return True
        except Exception as e:
            print(f"Error deleting {root_dir}: {e}")
            return False
    else:
        return False

def check_pre_imaging():
    today = datetime.now().strftime("%Y%m%d")
    base_dir = Path(c.PI_IMAGE_DIR)

    required_folders = [f"no-slide_{today}_{microscope_id}", f"no-light_{today}_{microscope_id}"]

    objectives = ["10x", "20x", "40x"]

    for folder in required_folders:
        folder_path = base_dir / folder
        print(f"folder_path is: {folder_path}")
        if not folder_path.exists():
            print(f"{folder_path} does not exist")
            return False

        print(f"{folder_path} exists")

        # Check each objective subfolder
        for obj in objectives:
            subfolder = folder_path / f"{folder}_{obj}"
            print(f"subfolder is: {subfolder}")
            if not subfolder.exists() or not any(subfolder.iterdir()):
                print(f"{subfolder} does not exist or it's empty")
                return False

    return True

if __name__ == '__main__':
    pass

    # --- Test Folder Generation ---
    generate_barcode_folders("WBCWWWW", ["SM1"], [1])
    #generate_barcode_folders("M3ABCD", ["SM1", "SM2", "SM3"])
    #generate_barcode_folders("M2ABCD", ["SM1", "SM2", "SM3"])
    #generate_barcode_folders("RA0000", ["SM1", "SM2", "SM3"])
    #generate_barcode_folders("M1ABCD", ["SM1", "SM2", "SM3"])
    #generate_barcode_folders("M5BCDE", ["SM1", "SM2", "SM3"])
    #generate_barcode_folders("M3BDCE", ["SM1", "SM2", "SM3"])


