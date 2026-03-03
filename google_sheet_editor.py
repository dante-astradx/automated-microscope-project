import config as c
from google_sheet_client import GoogleSheetClient
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import re

def extract_prefix(s):
    match = re.match(r"(RA|ID|M\d)", s)
    if not match:
        return None

    return match.group(1) or match.group(2)

def get_spreadsheet_tab_names(barcode):
    print(f"The barcode we're trying to log is: {barcode}")
    barcode_prefix = extract_prefix(barcode)
    print(f"The barcode's prefix is {barcode_prefix}")
    if barcode_prefix == "M1":
        tab_master = "Milestone 1 - Imaging Master List"
        tab_log = "Milestone 1 - Imaging Log"
    elif barcode_prefix == "M2":
        tab_master = "Milestone 2 - Imaging Master List"
        tab_log = "Milestone 2 - Imaging Log"
    elif barcode_prefix == "M3":
        tab_master = "Milestone 3 - Imaging Master List"
        tab_log = "Milestone 3 - Imaging Log"
    elif barcode_prefix == "M7":
        tab_master = "Milestone 7 - Imaging Master List"
        tab_log = "Milestone 7 - Imaging Log"
    elif barcode_prefix == "ID":
        tab_master = "ID SMEARS - Imaging Master List"
        tab_log = "ID SMEARS - Imaging Log"
    else:
        tab_master = "Milestone 5 - Imaging Master List"
        tab_log = "Milestone 5 - Imaging Log"

    return tab_master, tab_log

def log_milestone_run(
    barcode,
    imaging_type,      # "10x scan" or "10, 20, 40x zstack"
    service_account_file=c.SERVICE_ACCOUNT_FILE,
    spreadsheet_id=c.SPREADSHEET_ID
):

    gs = GoogleSheetClient(service_account_file, spreadsheet_id)

    tab_master, tab_log = get_spreadsheet_tab_names(barcode)

    # ---- 1) UPDATE MASTER LIST TAB ---- #
    ws_master = gs.ws(tab_master)

    # find matching barcode row
    row_idx = gs.find_row_by_barcode(ws_master, barcode, barcode_col_name="Barcode")

    # get date
    today_str = datetime.now().strftime("%m/%d/%Y")  # e.g. 02/03/2025

    # overwrite columns in master list
    gs.overwrite_cells(ws_master, row_idx, {
        "Imaging Type": imaging_type,
        "Date Imaged": today_str,
        "Microscope": c.MICROSCOPE_ID
    })


    # ---- 2) UPDATE LOG TAB (append new row) ---- #
    ws_log = gs.ws(tab_log)

    # Read full row from master list so we can extract columns for log tab
    master_row = ws_master.row_values(row_idx)
    master_header = ws_master.row_values(1)

    # columns we want in the log, in this exact order:
    LOG_COLUMNS = [
        "Slide Box",
        "Location",
        "Barcode",
        "Strain",
        "Organism",
        "Boxrun Metadata",
        "Date Imaged",
        "Microscope",
        "Imaging Type"
    ]

    # extract values from master list row
    log_row = []
    for col_name in LOG_COLUMNS:
        try:
            col_idx = master_header.index(col_name)
        except ValueError:
            raise Exception(f"Column '{col_name}' not found in Master List.")
        value = master_row[col_idx] if col_idx < len(master_row) else ""
        log_row.append(value)

    # append row to imaging log
    gs.append_row(ws_log, log_row)

    print(f"Successfully logged imaging run for barcode {barcode}.")

if __name__ == "__main__":
    pass
    s = extract_prefix("IDI777")
    print(s)
    log_milestone_run("IDI777", "10, 20, 40x zstack")
