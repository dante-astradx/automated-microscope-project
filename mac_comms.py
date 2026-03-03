import json
import subprocess
import time
import os
import config as c

def send_image_to_mac(filename):
    # --- Configuration ---
    MAC_USER = c.USERNAME
    MAC_IP = c.HOSTNAME_IP

    RASPI_SRC_DIR = c.PI_IMAGE_DIR
    MAC_DEST_DIR = f"/Users/{MAC_USER}/Documents/fov_detection/fov_upload_folder/"
    MAC_JSON_DIR = f"/Users/{MAC_USER}/Documents/fov_detection/json_outputs/"
    RASPI_JSON_DEST = f"/home/{c.MICROSCOPE_USERNAME}/json_results/"

    # Define file paths
    image_path = os.path.join(RASPI_SRC_DIR, filename)
    json_filename = filename.rsplit('.', 1)[0] + ".json"
    remote_json_path = f"{MAC_USER}@{MAC_IP}:{os.path.join(MAC_JSON_DIR, json_filename)}"
    local_json_path = os.path.join(RASPI_JSON_DEST, json_filename)

    # 1. Send the image to the Mac
    print(f"Sending {filename} to Mac...")
    rsync_send = ["rsync", "-avz", image_path, f"{MAC_USER}@{MAC_IP}:{MAC_DEST_DIR}"]

    try:
        result = subprocess.run(rsync_send, check=True)
        # If rsync succeeded, delete the local .tif file
        if result.returncode == 0:
            #os.remove(image_path)
            print(f"Success. {filename} moved to Mac and deleted from Raspi.")
    except subprocess.CalledProcessError as e:
        print(f"Failed to send image: {e}")
        return None

    # 2. Manual pause (10 seconds)
    print("Waiting 10 seconds for processing...")
    time.sleep(10)

    # 3. Repeatedly attempt to fetch the JSON file (Keep on Mac)
    print(f"Polling for {json_filename} every 2s (60s timeout)...")
    start_time = time.time()
    timeout = 60
    poll_interval = 2

    rsync_fetch = ["rsync", "-avz", remote_json_path, RASPI_JSON_DEST]

    while True:
        if time.time() - start_time > timeout:
            print("Timeout: JSON file not found on Mac.")
            return None

        # Attempt to fetch
        fetch_proc = subprocess.run(rsync_fetch, capture_output=True)

        if fetch_proc.returncode == 0:
            print(f"JSON received: {local_json_path}")
            return local_json_path

        time.sleep(poll_interval)

def send_background_image_to_mac(filename, raspi_folder_path):
    filename = f"{filename}.tif"
    local_image_path = os.path.join(raspi_folder_path, filename)
    MAC_DEST_DIR = f"/Users/{c.USERNAME}/Documents/fov_detection/no-slide/"

    print(f"Sending {filename} to Mac...")
    rsync_send = ["rsync", "-avz", local_image_path, f"{c.USERNAME}@{c.HOSTNAME_IP}:{MAC_DEST_DIR}"]

    try:
        result = subprocess.run(rsync_send, check=True)
        if result.returncode == 0:
            print(f"Success. {filename} moved to Mac.")
            return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to send image: {e}")
        return False

def send_darkfield_image_to_mac(filename, raspi_folder_path):
    filename = f"{filename}.tif"
    local_image_path = os.path.join(raspi_folder_path, filename)
    MAC_DEST_DIR = f"/Users/{c.USERNAME}/Documents/fov_detection/no-light/"

    print(f"Sending {filename} to Mac...")
    rsync_send = ["rsync", "-avz", local_image_path, f"{c.USERNAME}@{c.HOSTNAME_IP}:{MAC_DEST_DIR}"]

    try:
        result = subprocess.run(rsync_send, check=True)
        if result.returncode == 0:
            print(f"Success. {filename} moved to Mac.")
            return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to send image: {e}")
        return False


if __name__ == "__main__":
    pass
    send_image_to_mac("IDI777_16:37:07.372343_20260224_M1_SM1_146.0x_13.5y_217z_fov_detection.tif")
