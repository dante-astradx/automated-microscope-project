import json
from datetime import datetime
from pathlib import Path
import config as c
import csv
import requests
import re

# File path to persist the log
LOG_FILE = Path(f"/home/{c.MICROSCOPE_USERNAME}/project_files/folder_name_log.json")

# In-memory log
log = []

# Load existing log from disk at startup
if LOG_FILE.exists():
    try:
        with open(LOG_FILE, "r") as f:
            log = json.load(f)
    except json.JSONDecodeError:
        log = []

def save_log():
    """Save the in-memory log to disk."""
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)

def check_barcode(barcode: str):
    return isinstance(barcode, str) and len(barcode) == 6

def add_entry(folder_name):
    """Add a folder name with current date to the log."""
    entry = {"folder_name": folder_name, "date": datetime.today().strftime("%Y%m%d")}
    log.insert(0, entry)  # newest first
    save_log()

def clear_last_entry():
    """Remove the most recent entry from the log."""
    global log
    if log:
        log.pop(0)  # remove the first (newest) entry
        save_log()

def clear_log():
    """Empty the log."""
    global log
    log = []
    save_log()

import json

def lookup_smear_coordinates(barcode, db_path="slides_to_image.json"):
    # Load database
    with open(db_path, "r") as f:
        data = json.load(f)

    # Iterate through slides
    for entry in data["slides"]:
        if entry["barcode"] == barcode:
            smears_dict = entry["smears"]

            # Convert {"SM1": [x,y], "SM2": [x,y], ...} → list of lists [[x,y], [x,y], [x,y]]
            smear_ids = list(smears_dict.keys())
            coordinates = list(smears_dict.values())

            return smear_ids, coordinates

    # If barcode not found
    raise ValueError(f"Barcode {barcode} not found in database.")

def parse_coord(cell_value):
    """
    Parse coordinate cell values.

    Examples:
        "110, 15" → [[110, 15]]
        "(110, 15), (120, 10)" → [[110, 15], [120, 10]]

    Returns None if empty or malformed.
    """
    if not cell_value:
        return None

    cell_value = cell_value.strip()
    if cell_value == "":
        return None

    coords = []

    try:
        # Case 1: multiple coordinates like "(110, 15), (120, 10)"
        if "(" in cell_value and ")" in cell_value:
            matches = re.findall(r"\(([^)]+)\)", cell_value)

            for match in matches:
                x_str, y_str = match.split(",")

                x_val = float(x_str.strip())
                y_val = float(y_str.strip())

                if x_val.is_integer():
                    x_val = int(x_val)
                if y_val.is_integer():
                    y_val = int(y_val)

                coords.append([(x_val + c.X_OFFSET), (y_val + c.Y_OFFSET)])

        # Case 2: single coordinate like "110, 15"
        else:
            x_str, y_str = cell_value.split(",")

            x_val = float(x_str.strip())
            y_val = float(y_str.strip())

            if x_val.is_integer():
                x_val = int(x_val)
            if y_val.is_integer():
                y_val = int(y_val)

            coords.append([(x_val + c.X_OFFSET), (y_val + c.Y_OFFSET)])

        return coords if coords else None

    except Exception:
        return None

def extract_prefix(s):
    match = re.match(r"(WBC|RA|ID|M\d)", s)
    if not match:
        return None

    return match.group(1) or match.group(2)

def get_spreadsheet_csv(barcode):
    barcode_prefix = extract_prefix(barcode)
    if barcode_prefix == "M1":
        csv_url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ-18yNc7T6yJ79Gg8bbdWbB53foW-MTEX78LxqIHkHyF5xVFW_b1yPWI5K-vfrsDtZIp8NOsDTUxfh/pub?gid=1771529884&single=true&output=csv"
    elif barcode_prefix == "M2":
        csv_url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ-18yNc7T6yJ79Gg8bbdWbB53foW-MTEX78LxqIHkHyF5xVFW_b1yPWI5K-vfrsDtZIp8NOsDTUxfh/pub?gid=1903681075&single=true&output=csv"
    elif barcode_prefix == "M3":
        csv_url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ-18yNc7T6yJ79Gg8bbdWbB53foW-MTEX78LxqIHkHyF5xVFW_b1yPWI5K-vfrsDtZIp8NOsDTUxfh/pub?gid=1804909362&single=true&output=csv"
    elif barcode_prefix == "M5":
        csv_url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ-18yNc7T6yJ79Gg8bbdWbB53foW-MTEX78LxqIHkHyF5xVFW_b1yPWI5K-vfrsDtZIp8NOsDTUxfh/pub?gid=963897326&single=true&output=csv"
    elif barcode_prefix == "M7":
        csv_url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ-18yNc7T6yJ79Gg8bbdWbB53foW-MTEX78LxqIHkHyF5xVFW_b1yPWI5K-vfrsDtZIp8NOsDTUxfh/pub?gid=1253573499&single=true&output=csv"
    elif barcode_prefix == "M8":
        csv_url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ-18yNc7T6yJ79Gg8bbdWbB53foW-MTEX78LxqIHkHyF5xVFW_b1yPWI5K-vfrsDtZIp8NOsDTUxfh/pub?gid=1360243811&single=true&output=csv"
    elif barcode_prefix == "WBC":
        csv_url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ-18yNc7T6yJ79Gg8bbdWbB53foW-MTEX78LxqIHkHyF5xVFW_b1yPWI5K-vfrsDtZIp8NOsDTUxfh/pub?gid=656506404&single=true&output=csv"
    else:
        csv_url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ-18yNc7T6yJ79Gg8bbdWbB53foW-MTEX78LxqIHkHyF5xVFW_b1yPWI5K-vfrsDtZIp8NOsDTUxfh/pub?gid=1807521073&single=true&output=csv"

    return csv_url

def csv_lookup(barcode, selected_smears):
    csv_url = get_spreadsheet_csv(barcode)
    response = requests.get(csv_url)
    response.raise_for_status()
    rows = list(csv.reader(response.text.splitlines()))
    # --- Identify column indices ---
    header = rows[0]
    try:
        col_barcode = header.index("Barcode")
        col_smear_number = header.index("Smear No.")
        col_coordinate = header.index("X, Y Coord.")
    except ValueError:
        raise Exception("One or more required columns not found in CSV header.")

    smear_ids = []
    coords = []
    found_barcode = False
    # Normalize selected smears → {1, 3} etc
    selected_smear_numbers = {
        int(sm.replace("SM", "")) for sm in selected_smears
    }
    # --- Scan rows ---
    for row in rows[1:]:
        if len(row) <= max(col_barcode, col_smear_number, col_coordinate):
            continue
        row_barcode = row[col_barcode]
        # Haven't found barcode yet
        if not found_barcode:
            if row_barcode != barcode:
                continue
            found_barcode = True
        # Barcode block ended
        elif row_barcode != barcode:
            break
        # --- Parse smear number ---
        try:
            smear_no = int(row[col_smear_number])
        except ValueError:
            continue
        # Skip smears we don't want
        if smear_no not in selected_smear_numbers:
            continue
        coord = parse_coord(row[col_coordinate])
        if coord is None:
            continue
        smear_ids.append(f"SM{smear_no}")
        coords.append(coord)

    if not smear_ids:
        raise Exception(
            f"Barcode {barcode} found, but no selected smear coordinates were populated."
        )

    return smear_ids, coords

if __name__ == "__main__":
    pass
    clear_log()
    #clear_last_entry()
    #s = extract_prefix("WBCWWWW")
    #print(s)
    #smear_id, coords = csv_lookup("M8AAAA", ["SM1", "SM2", "SM3"])
    #print(smear_id)
    #print(coords)
