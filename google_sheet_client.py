import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

class GoogleSheetClient:
    def __init__(self, service_account_path, spreadsheet_id):
        self.service_account_path = service_account_path
        self.spreadsheet_id = spreadsheet_id

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(
            self.service_account_path, scopes=scopes
        )
        self.gc = gspread.authorize(creds)

        self.sh = self.gc.open_by_key(self.spreadsheet_id)

    def ws(self, tab_name):
        """Return worksheet object for the tab."""
        return self.sh.worksheet(tab_name)

    def find_row_by_barcode(self, ws, barcode, barcode_col_name="Barcode"):
        """Find a row index matching the barcode in the given worksheet."""
        header = ws.row_values(1)
        try:
            col_idx = header.index(barcode_col_name) + 1
        except ValueError:
            raise Exception(f"Barcode column '{barcode_col_name}' not found.")

        col_values = ws.col_values(col_idx)

        for idx, val in enumerate(col_values, start=1):
            if val.strip() == barcode:
                return idx

        raise Exception(f"Barcode '{barcode}' not found in worksheet '{ws.title}'.")

    def overwrite_cells(self, ws, row_idx, updates: dict):
        """
        updates = { "Column Header": "Value", ... }
        Writes values based on header names.
        """
        header = ws.row_values(1)

        for col_name, value in updates.items():
            try:
                col_idx = header.index(col_name) + 1
            except ValueError:
                raise Exception(f"Column '{col_name}' not found in worksheet '{ws.title}'.")

            ws.update_cell(row_idx, col_idx, value)

    def append_row(self, ws, row_list):
        """Append a row to a worksheet."""
        ws.append_row(row_list, value_input_option="USER_ENTERED")
