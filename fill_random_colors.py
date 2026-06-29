# -*- coding: utf-8 -*-
"""
Script to fill random hex colors in Google Sheets for the creative pipeline
Fills Background Color, Primary Text Color, CTA Color, Button Text Color, Button Color
"""
import os
import random
import gspread
from google.oauth2.service_account import Credentials

# Configuration
SERVICE_ACCOUNT_FILE = "service_account.json"
SHEET_URL = "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit?gid=0#gid=0"
SHEET_GID = "0"

# Color columns to fill
COLOR_COLUMNS = [
    "Background Color",
    "Primary Text Color", 
    "CTA Color",
    "Button Text Color",
    "Button Color"
]

# Rows to fill (1-16, but in sheet that's rows 2-17 if row 1 is headers)
START_ROW = 2
END_ROW = 17

# Predefined color palette (beautiful, professional colors)
COLOR_PALETTE = [
    # Blues
    "#103249", "#1E88E5", "#2196F3", "#00BCD4", "#4FC3F7",
    # Reds/Pinks
    "#F72358", "#E91E63", "#FF5252", "#FF1744", "#D32F2F",
    # Purples
    "#9C27B0", "#7B1FA2", "#673AB7", "#5E35B1", "#AB47BC",
    # Greens
    "#4CAF50", "#66BB6A", "#00C853", "#00E676", "#2E7D32",
    # Oranges
    "#FF9800", "#FB8C00", "#F57C00", "#FF6F00", "#FF5722",
    # Yellows
    "#FFC107", "#FFB300", "#FFA000", "#FF8F00", "#FFCA28",
    # Teals
    "#009688", "#00897B", "#26A69A", "#00BFA5", "#1DE9B6",
    # Grays/Neutrals
    "#607D8B", "#455A64", "#37474F", "#263238", "#90A4AE",
    # Deep colors
    "#C62828", "#AD1457", "#6A1B9A", "#4527A0", "#283593",
    "#1565C0", "#0277BD", "#00695C", "#2E7D32", "#558B2F",
    # Bright accents
    "#00E5FF", "#1DE9B6", "#76FF03", "#FFEA00", "#FF9100"
]

def get_random_color():
    """Get a random color from the palette"""
    return random.choice(COLOR_PALETTE)

def authenticate_google_sheet():
    """Authenticate with Google Sheets API"""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return gspread.authorize(creds)

def main():
    print("=" * 60)
    print("Random Color Filler")
    print("=" * 60)
    
    # Authenticate
    print("\n[1/4] Authenticating with Google Sheets...")
    gc = authenticate_google_sheet()
    
    # Open spreadsheet by URL
    print("[2/4] Opening spreadsheet...")
    spreadsheet = gc.open_by_url(SHEET_URL)
    
    # Get worksheet by gid
    print(f"[3/4] Finding worksheet with gid={SHEET_GID}...")
    worksheet = None
    for ws in spreadsheet.worksheets():
        if str(ws.id) == SHEET_GID:
            worksheet = ws
            break
    
    if not worksheet:
        print(f"ERROR: Could not find worksheet with gid={SHEET_GID}")
        return
    
    print(f"    Found worksheet: '{worksheet.title}'")
    
    # Get all values to find column indices
    print("[4/4] Reading current data and filling colors...")
    all_values = worksheet.get_all_values()
    if not all_values:
        print("ERROR: No data in worksheet")
        return
    
    headers = all_values[0]
    print(f"    Headers: {headers}")
    
    # Find column indices for color columns
    color_col_indices = {}
    for color_col in COLOR_COLUMNS:
        try:
            idx = headers.index(color_col)
            color_col_indices[color_col] = idx + 1  # gspread uses 1-based indexing
            print(f"    Found '{color_col}' at column {idx + 1}")
        except ValueError:
            print(f"    WARNING: Column '{color_col}' not found in headers")
    
    if not color_col_indices:
        print("ERROR: No color columns found!")
        return
    
    # Fill random colors
    print(f"\n    Filling colors for rows {START_ROW} to {END_ROW}...")
    updates = []
    
    for row in range(START_ROW, END_ROW + 1):
        colors_for_row = {}
        for color_col, col_idx in color_col_indices.items():
            random_color = get_random_color()
            colors_for_row[color_col] = random_color
            # Add to batch update
            cell_address = gspread.utils.rowcol_to_a1(row, col_idx)
            updates.append({
                'range': cell_address,
                'values': [[random_color]]
            })
        
        print(f"    Row {row}: {colors_for_row}")
    
    # Batch update all cells at once
    print(f"\n    Updating {len(updates)} cells in Google Sheets...")
    worksheet.batch_update(updates)
    
    print("\n" + "=" * 60)
    print("SUCCESS! Random colors filled successfully!")
    print(f"Updated {len(updates)} cells across {END_ROW - START_ROW + 1} rows")
    print("=" * 60)

if __name__ == "__main__":
    main()

