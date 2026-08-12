import json
import os
from datetime import datetime
import config as c

def read_json(json_path):
    #JSON_DIR = f"/home/{c.MICROSCOPE_USERNAME}/json_results"
    #json_path = os.path.join(JSON_DIR, filename)

    try:
        with open(json_path, 'r') as file:
            data = json.load(file)
        tile_1 = data['tile_1']
        tile_2 = data['tile_2']
        tile_3 = data['tile_3']
        tile_4 = data['tile_4']
        tile_5 = data['tile_5']
        tile_6 = data['tile_6']
        tile_7 = data['tile_7']
        tile_8 = data['tile_8']
        tile_9 = data['tile_9']
        x_coord = data['x_coord']
        y_coord = data['y_coord']

        print(f"Data found in json file: tile_5 = {tile_5}, at {x_coord},{y_coord}")

        #os.remove(json_path)
        print(f"Successfully processed and removed file at path: {json_path}")
        return tile_5, x_coord, y_coord

    except FileNotFoundError:
        print(f"Error: The file {json_path} was not found.")
    except KeyError as e:
        print(f"Error: Key {e} missing in the JSON data. File kept for debugging.")
    except json.JSONDecodeError:
        print(f"Error: {json_path} is not valid JSON. File kept for debugging.")

    return None, None, None

def create_correction_json(correction):
    today = datetime.now().strftime("%Y%m%d")
    json_name = f"{correction}.json"
    json_absolute_path = os.path.join(c.PI_IMAGE_DIR, f"{correction}_{today}_{c.MICROSCOPE_ID}", json_name)
    json_relative_path = os.path.relpath(json_absolute_path, c.PI_IMAGE_DIR)

    data = {
        f"{correction}_json_path": json_relative_path,
        f"{correction}_correction_images": {

        }
    }

    with open(json_absolute_path, 'w') as file:
        json.dump(data, file, indent=2)

def create_zstack_json(working_directory, x_pos, y_pos, fov, obj, smear_id):
    zstack_name = os.path.basename(working_directory)

    json_name = f"{zstack_name}.json"
    json_absolute_path = os.path.join(working_directory, json_name)

    json_relative_path = os.path.relpath(json_absolute_path, c.PI_IMAGE_DIR)
    zstack_relative_path = os.path.relpath(working_directory, c.PI_IMAGE_DIR)

    data = {
        "zstack_name": zstack_name,
        "zstack_json_path": json_relative_path,
        "zstack_path": zstack_relative_path,
        "x_pos": x_pos,
        "y_pos": y_pos,
        "fov": fov,
        "smear_id": smear_id,
        "magnification": obj,
        "microscope_id": c.MICROSCOPE_ID,
        "zstack_images": {
            
        }
    }

    with open(json_absolute_path, 'w') as file:
        json.dump(data, file, indent=2)

def create_manifest_json(file_transfer):
    working_directory = os.path.join(c.PI_IMAGE_DIR, file_transfer.slide_case_folder)
    json_absolute_path = os.path.join(working_directory, "manifest.json")
    json_relative_path = os.path.relpath(json_absolute_path, c.PI_IMAGE_DIR)

    data = {
        "manifest_json_path": json_relative_path,
        "barcode": file_transfer.barcode,
        "slide_case_folder": file_transfer.slide_case_folder,
        "zstack_metadata": [],
        "no-slide_correction": {},
        "no-light_correction": {}
    }

    with open(json_absolute_path, 'w') as file:
        json.dump(data, file, indent=2)

def update_failed_qc_zstack_json(new_zstack_name, new_zstack_json_absolute_path, new_folder_absolute_path):
    new_zstack_json_relative_path = os.path.relpath(new_zstack_json_absolute_path, c.PI_IMAGE_DIR)
    new_folder_relative_path = os.path.relpath(new_folder_absolute_path, c.PI_IMAGE_DIR)
    
    try:
        with open(new_zstack_json_absolute_path, 'r') as f:
            zstack_data = json.load(f)

        # Update the zstack name and path
        zstack_data["zstack_name"] = new_zstack_name
        zstack_data["zstack_json_path"] = new_zstack_json_relative_path
        zstack_data["zstack_path"] = new_folder_relative_path

        # Write the updated data back to the JSON file
        with open(new_zstack_json_absolute_path, 'w') as f:
            json.dump(zstack_data, f, indent=2)

    except FileNotFoundError as e:
        print(f"Error: File not found - {e}")
    except KeyError as e:
        print(f"Error: Expected key {e} missing in JSON data.")
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON - {e}")

def append_correction_image_to_json(correction_type, correction_json_path, image_json_path):
    try:
        with open(correction_json_path, 'r') as f:
            correction_data = json.load(f)

        with open(image_json_path, 'r') as f:
            image_data = json.load(f)

        image_key = image_data.get("image_filename", os.path.basename(image_json_path))
        correction_data[f"{correction_type}_correction_images"][image_key] = image_data

        with open(correction_json_path, 'w') as f:
            json.dump(correction_data, f, indent=2)

        os.remove(image_json_path)

    except FileNotFoundError as e:
        print(f"Error: File not found - {e}")
    except KeyError as e:
        print(f"Error: Expected key {e} missing in JSON data.")
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON - {e}")

def populate_zstack_json_from_folder(zstack_folder_path):
    """
    After image_cleanup, scan the zstack folder for remaining image JSONs,
    append them into the zstack JSON in ascending z-order, then delete each one.
    The zstack JSON itself (named <folder>.json) is excluded from processing.
    """
    zstack_name = os.path.basename(zstack_folder_path)
    zstack_json_filename = f"{zstack_name}.json"
    zstack_json_path = os.path.join(zstack_folder_path, zstack_json_filename)

    try:
        image_json_files = [
            f for f in os.listdir(zstack_folder_path)
            if f.endswith(".json") and f != zstack_json_filename
        ]

        def extract_z(filename):
            # Filenames end in ..._{z}z.json — extract the integer z value for sorting
            name = os.path.splitext(filename)[0]
            z_part = name.rsplit("_", maxsplit=1)[-1]
            return int(z_part.rstrip("z"))

        image_json_files.sort(key=extract_z)

        with open(zstack_json_path, 'r') as f:
            zstack_data = json.load(f)

        for image_json_filename in image_json_files:
            image_json_path = os.path.join(zstack_folder_path, image_json_filename)
            try:
                with open(image_json_path, 'r') as f:
                    image_data = json.load(f)
                image_key = image_data.get("image_filename", image_json_filename)
                zstack_data["zstack_images"][image_key] = image_data
                os.remove(image_json_path)
            except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
                print(f"Warning: could not process {image_json_filename} - {e}")

        with open(zstack_json_path, 'w') as f:
            json.dump(zstack_data, f, indent=2)

    except FileNotFoundError as e:
        print(f"Error: File not found - {e}")
    except KeyError as e:
        print(f"Error: Expected key {e} missing in zstack JSON.")
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON - {e}")

def append_image_data_to_zstack_json(zstack_json_path, image_json_path):
    try:
        with open(zstack_json_path, 'r') as f:
            zstack_data = json.load(f)

        with open(image_json_path, 'r') as f:
            image_data = json.load(f)

        image_key = image_data.get("image_filename", os.path.basename(image_json_path))
        zstack_data["zstack_images"][image_key] = image_data

        with open(zstack_json_path, 'w') as f:
            json.dump(zstack_data, f, indent=2)

        os.remove(image_json_path)

    except FileNotFoundError as e:
        print(f"Error: File not found - {e}")
    except KeyError as e:
        print(f"Error: Expected key {e} missing in JSON data.")
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON - {e}")

def append_zstack_metadata_to_manifest(manifest_json_path, zstack_json_path):
    try:
        with open(manifest_json_path, 'r') as f:
            manifest_data = json.load(f)

        with open(zstack_json_path, 'r') as f:
            zstack_data = json.load(f)

        manifest_data["zstack_metadata"].append(zstack_data)

        with open(manifest_json_path, 'w') as f:
            json.dump(manifest_data, f, indent=2)

    except FileNotFoundError as e:
        print(f"Error: File not found - {e}")
    except KeyError as e:
        print(f"Error: Expected key {e} missing in JSON data.")
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON - {e}")

def append_correction_metadata_to_manifest(correction_type, manifest_json_path, correction_json_path):
    try:
        with open(manifest_json_path, "r") as f:
            manifest_data = json.load(f)

        with open(correction_json_path, "r") as f:
            correction_data = json.load(f)

        manifest_data[f"{correction_type}_correction"] = correction_data

        with open(manifest_json_path, "w") as f:
            json.dump(manifest_data, f, indent=2)

    except FileNotFoundError as e:
        print(f"Error: File not found - {e}")
    except KeyError as e:
        print(f"Error: Expected key {e} missing in JSON data.")
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON - {e}")

if __name__ == "__main__":
    pass
    read_json("/home/microscope_auto/json_results/M5FJMD_20260220_M1_unstained_SM1_10x_1_149x_13y_323z.json")
    