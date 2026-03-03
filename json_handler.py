import json
import os
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
        print(f"Error: {filename} is not valid JSON. File kept for debugging.")

    return None, None, None

if __name__ == "__main__":
    pass
    read_json("/home/microscope_auto/json_results/M5FJMD_20260220_M1_unstained_SM1_10x_1_149x_13y_323z.json")
