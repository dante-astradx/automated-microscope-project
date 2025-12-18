import json
from datetime import datetime
from pathlib import Path
import config as c
import csv
import requests

# URL to spreadsheet
# Milestone 5
#CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ-18yNc7T6yJ79Gg8bbdWbB53foW-MTEX78LxqIHkHyF5xVFW_b1yPWI5K-vfrsDtZIp8NOsDTUxfh/pub?gid=963897326&single=true&output=csv"

# Milestone 3
CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ-18yNc7T6yJ79Gg8bbdWbB53foW-MTEX78LxqIHkHyF5xVFW_b1yPWI5K-vfrsDtZIp8NOsDTUxfh/pub?gid=1804909362&single=true&output=csv"

# Milestone 1
#CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ-18yNc7T6yJ79Gg8bbdWbB53foW-MTEX78LxqIHkHyF5xVFW_b1yPWI5K-vfrsDtZIp8NOsDTUxfh/pub?gid=1771529884&single=true&output=csv"

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
    Convert a cell like "105.2, 80.3" into [105.2, 80.3].
    Returns None if the cell is empty or malformed.
    """
    if not cell_value:
        return None

    cell_value = cell_value.strip()
    if cell_value == "":
        return None

    try:
        x_str, y_str = cell_value.split(",")

        # Convert as float first
        x_val = float(x_str.strip())
        y_val = float(y_str.strip())

        # Convert to int when possible
        if x_val.is_integer():
            x_val = int(x_val)

        if y_val.is_integer():
            y_val = int(y_val)

        return [x_val, y_val]

    except Exception:
        return None

def csv_lookup(barcode):
    """
    Looks up a barcode in Column C of the CSV.
    Extracts SM1/SM2/SM3 coordinates from columns K, L, M.
    Returns smear_ids, coords
    """
    # Download sheet as CSV text
    response = requests.get(CSV_URL)
    response.raise_for_status()  # fail fast on error

    rows = list(csv.reader(response.text.splitlines()))

    # --- Identify column indices based on headers ---
    header = rows[0]

    # Find column indexes explicitly
    try:
        col_barcode = header.index("Barcode")           # Column C
        col_SM1 = header.index("SM1 X, Y")              # Column K
        col_SM2 = header.index("SM2 X, Y")              # Column L
        col_SM3 = header.index("SM3 X, Y")              # Column M
    except ValueError:
        raise Exception("One or more required columns not found in CSV header.")

    # --- Search rows for this barcode ---
    for row in rows[1:]:
        if len(row) <= col_barcode:
            continue  # skip incomplete rows

        if row[col_barcode] == barcode:  # EXACT MATCH
            # Parse each smear coordinate if present
            smear_ids = []
            coords = []

            sm1 = parse_coord(row[col_SM1])
            sm2 = parse_coord(row[col_SM2])
            sm3 = parse_coord(row[col_SM3])

            if sm1 is not None:
                smear_ids.append("SM1")
                coords.append(sm1)

            if sm2 is not None:
                smear_ids.append("SM2")
                coords.append(sm2)

            if sm3 is not None:
                smear_ids.append("SM3")
                coords.append(sm3)

            # If no coordinates found → error
            if not smear_ids:
                raise Exception(
                    f"Barcode {barcode} found, but no SM1/SM2/SM3 coordinates are populated."
                )

            return smear_ids, coords

    # If loop completes with no match → barcode not found
    raise Exception(f"Barcode {barcode} not found in spreadsheet.")


if __name__ == "__main__":
    pass
    clear_log()
    #clear_last_entry()
    #add_entry("M5C976")
