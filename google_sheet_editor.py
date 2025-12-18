import config as c
from google_sheet_client import GoogleSheetClient
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

def log_milestone_run(
    barcode,
    imaging_type,      # "10x scan" or "10, 20, 40x zstack"
    service_account_file=c.SERVICE_ACCOUNT_FILE,
    spreadsheet_id=c.SPREADSHEET_ID
):

    gs = GoogleSheetClient(service_account_file, spreadsheet_id)

    # ---- 1) UPDATE MASTER LIST TAB ---- #
    ws_master = gs.ws(c.TAB_MASTER)

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
    ws_log = gs.ws(c.TAB_LOG)

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
    log_milestone_run("M13P7T", "10, 20, 40x zstack")
