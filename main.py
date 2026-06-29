# -*- coding: utf-8 -*-
import os
import sys
import glob
import re
import requests
import time
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

# Load environment variables from a local .env file if python-dotenv is installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Third-party ---
from google.cloud import storage
import gspread
from google.oauth2.service_account import Credentials
from photoshop import Session
from PIL import Image, ImageOps

# =========================
#       CONFIG
# =========================

BASE_DIR = Path(__file__).resolve().parent

# Relative working folders (independent of the Windows user)
PHOTOSHOP_TEMPLATES_FOLDER = str(BASE_DIR / "Templates")
OUTPUT_FOLDER              = str(BASE_DIR / "Exported")
TEMP_STOCKS_FOLDER         = str(BASE_DIR / "Stocks_Temp")

# Use the same Service Account for both GCS and Sheets
# Place service_account.json next to main.py or set SERVICE_ACCOUNT_JSON in the environment
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_JSON", "service_account.json")

# New Google Sheet
SHEET_URL   = os.getenv(
    "GOOGLE_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit?gid=0#gid=0"
)

# Check if sheet name is passed via environment variable from task kwargs
SHEET_NAME = os.getenv("SHEET_NAME", "YOUR_SHEET_NAME")  # Check SHEET_NAME first, otherwise use default
print(f"Using sheet name: {SHEET_NAME}")

# Google Cloud Storage bucket
BUCKET_NAME = os.getenv("GCS_BUCKET", "your-gcs-bucket")

# PSD layer names
PLACEHOLDER_LAYER_NAME      = "Temp_Img_Placeholder"
MAIN_TEXT_LAYER_NAME        = "Temp_Text_Layer"
CTA_TEXT_LAYER_NAME         = "Temp_Cta_Layer"
TEMP_BUTTON_TEXT_LAYER_NAME = "Temp_Button_Text"

# Sheet column layout
OUTPUT_START_COL = 25  # Column Y: first Output# column written back to the sheet
MAX_STOCK_COLS   = 10  # Stock#1 .. Stock#10 input columns
MAX_OUTPUT_COLS  = 5   # Output#1 .. Output#5 columns

# Helper functions (defined in the accompanying modules)
from color_automation import (
    is_valid_hex_color,
    select_bg_color_layer,
    fill_active_selection_with_color,
    set_text_layer_color,
    clear_bg_color_layer,
    select_btn_color_layer,
    clear_btn_color_layer
)
from text_size_automation import adjust_text_sizes, prevent_text_overflow

# =========================
#   UTILS
# =========================

def safe_name(s: str) -> str:
    # Remove or replace characters invalid in Windows filenames: < > : " / \ | ? * and other non-ASCII
    s = s or 'out'
    # Replace problematic characters with underscores
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]', '_', s)
    # Keep only safe ASCII alphanumerics, dots, dashes, underscores
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', s)
    # Remove leading/trailing underscores and dots
    s = s.strip('._')
    return s[:150] if s else 'out'

def get_with_retry(url, tries=3, timeout=25):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            return r.content
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last

# =========================
#   CLOUD / SHEETS
# =========================

def initialize_storage():
    """Initialize GCS with the same Service Account; avoid get_bucket so we don't require buckets.get"""
    storage_client = storage.Client.from_service_account_json(SERVICE_ACCOUNT_FILE)
    bucket = storage_client.bucket(BUCKET_NAME)  # Does not read metadata
    return bucket

def authenticate_google_sheet():
    """Authenticate to Google Sheets using google.oauth2.service_account"""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return gspread.authorize(creds)

def open_worksheet(gc, url_or_id, sheet_name):
    """Open a spreadsheet by URL/ID; try the tab with the given name, and if absent fall back to fuzzy matching (ignoring spaces/case/dashes)."""
    if url_or_id.startswith("http"):
        ss = gc.open_by_url(url_or_id)
    else:
        ss = gc.open_by_key(url_or_id)

    # 1) Attempt an exact match
    try:
        return ss.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        pass

    # 2) Fuzzy match: ignore spaces/dashes/underscores and case, and add a space before trailing digits if missing
    def canon(s: str) -> str:
        s = re.sub(r"\s+", " ", s or "").strip()
        # Add a space before consecutive trailing digits if absent (e.g., "YOUR_SHEET_NAME2" -> "YOUR_SHEET_NAME 2")
        s = re.sub(r"(?<=\D)(\d+)$", r" \1", s)
        return ''.join(ch for ch in s.lower() if ch.isalnum())

    desired = canon(sheet_name)
    for ws in ss.worksheets():
        if canon(ws.title) == desired:
            print(f"Matched worksheet by fuzzy title: '{ws.title}'")
            return ws

    # 3) If not found, fall back to the first tab
    print(f"Worksheet '{sheet_name}' not found — using first sheet instead.")
    return ss.sheet1

def generate_image_name(base_name, template_name, index):
    current_date = datetime.now().strftime("%m%d%y")
    return f"{safe_name(base_name)}-{current_date}-{safe_name(template_name)}-{index}"

def upload_to_storage(bucket, image_path, destination_name):
    """Upload to GCS; make public if possible, otherwise return a 30-day Signed URL"""
    blob = bucket.blob(f'fb_ui_assets/{destination_name}.jpg')
    blob.upload_from_filename(image_path, content_type='image/jpeg')
    try:
        blob.cache_control = "public, max-age=31536000, immutable"
        blob.patch()
    except Exception:
        pass
    try:
        blob.make_public()
        return blob.public_url
    except Exception:
        # If the bucket is private/UBLA, return a signed link
        return blob.generate_signed_url(expiration=timedelta(days=30))

# =========================
#   PHOTOSHOP HELPERS
# =========================

def fix_missing_fonts(doc, ps, fallback_font="ArialMT"):
    """
    Automatically replaces missing fonts in all text layers with a fallback font.
    This prevents the Missing Fonts dialog from appearing.
    """
    try:
        for i in range(len(doc.artLayers)):
            layer = doc.artLayers[i]
            if layer.kind == ps.LayerKind.TextLayer:
                try:
                    # Try to access the font - if it's missing, an exception will be raised
                    current_font = layer.textItem.font
                    layer.textItem.font = current_font
                except Exception:
                    # Font is missing - replace with fallback
                    try:
                        layer.textItem.font = fallback_font
                        print(f"  [FONT] Replaced missing font in layer '{layer.name}' with {fallback_font}")
                    except Exception as e:
                        print(f"  [WARN] Could not fix font in layer '{layer.name}': {e}")
    except Exception as e:
        print(f"  Warning: Error during font fix: {e}")

def clean_placed_images(doc, placeholder_layer):
    """
    Removes all layers above the placeholder that were created from previous stock images.
    This prevents old images from appearing in new renders.
    """
    try:
        layers_to_delete = []
        # Find all layers above the placeholder that start with "Placed_"
        for i in range(len(doc.artLayers)):
            layer = doc.artLayers[i]
            if layer.name.startswith("Placed_"):
                layers_to_delete.append(layer)
        
        # Delete found layers
        deleted_count = 0
        for layer in layers_to_delete:
            try:
                layer.Delete()
                deleted_count += 1
            except Exception as e:
                print(f"  [WARN] Could not delete old layer '{layer.name}': {e}")
        
        if deleted_count > 0:
            print(f"  [CLEAN] Cleaned {deleted_count} old image layer(s)")
    except Exception as e:
        print(f"  Warning: Error during cleanup: {e}")

def place_and_fit_image(doc, placeholder_layer, stock_path, ps):
    """
    1) Open the stock image and copy it
    2) Paste into the active document
    3) Move above the placeholder + clipping mask
    4) Scale to fit and center
    """
    app = ps.app
    stock_doc = app.Open(stock_path)
    stock_doc.selection.selectAll()
    stock_doc.selection.copy()
    stock_doc.Close(ps.SaveOptions.DoNotSaveChanges)

    app.activeDocument = doc
    doc.Paste()
    new_layer = doc.ActiveLayer

    new_layer.move(placeholder_layer, ps.ElementPlacement.PlaceBefore)
    new_layer.name = f"Placed_{os.path.basename(stock_path)}"
    new_layer.grouped = True  # clipping mask

    p_left, p_top, p_right, p_bottom = placeholder_layer.bounds
    p_width = p_right - p_left
    p_height = p_bottom - p_top

    n_left, n_top, n_right, n_bottom = new_layer.bounds
    n_width = n_right - n_left
    n_height = n_bottom - n_top

    if n_width == 0 or n_height == 0:
        return new_layer

    scale_ratio = max(p_width / n_width, p_height / n_height) * 100
    new_layer.resize(scale_ratio, scale_ratio, 5)

    # Center
    n_left, n_top, n_right, n_bottom = new_layer.bounds
    n_width = n_right - n_left
    n_height = n_bottom - n_top
    placeholder_center_x = p_left + (p_width / 2)
    placeholder_center_y = p_top + (p_height / 2)
    new_center_x = n_left + (n_width / 2)
    new_center_y = n_top + (n_height / 2)
    delta_x = placeholder_center_x - new_center_x
    delta_y = placeholder_center_y - new_center_y
    new_layer.translate(delta_x, delta_y)

    return new_layer

# =========================
#       MAIN WORK
# =========================

def process_and_upload_images():
    bucket = initialize_storage()
    client = authenticate_google_sheet()
    sheet = open_worksheet(client, SHEET_URL, SHEET_NAME)

    # Read all values and filter out empty headers to avoid duplicate header errors
    all_values = sheet.get_all_values()
    if not all_values:
        print("No data found in sheet")
        return
    
    headers = all_values[0]
    # Filter out empty headers and keep track of valid column indices
    valid_indices = [i for i, h in enumerate(headers) if h and h.strip()]
    filtered_headers = [headers[i] for i in valid_indices]
    
    # Build records dictionary from filtered columns
    rows = []
    for row_data in all_values[1:]:
        row_dict = {}
        for i, header in zip(valid_indices, filtered_headers):
            if i < len(row_data):
                row_dict[header] = row_data[i]
            else:
                row_dict[header] = ""
        rows.append(row_dict)
    
    url_start_col = OUTPUT_START_COL  # Column Y (Output#1 if present, but kept fixed here)

    for row_idx, row in enumerate(rows, start=2):
        try:
            # Skip empty rows (rows where all values are empty)
            if not any(str(v).strip() for v in row.values()):
                continue
            
            # Skip rows without primary text (essential field)
            primary_text = row.get("Primary Text", "").strip()
            if not primary_text:
                print(f"Skipping Row {row_idx}: No Primary Text found")
                continue

            # Skip rows where any Output column already has a link
            output_col_names = [f"Output#{i}" for i in range(1, MAX_OUTPUT_COLS + 1)]
            if any(row.get(col, "").strip().startswith("http") for col in output_col_names):
                print(f"Skipping Row {row_idx}: Output already has links → skipping.")
                continue

            exported_name = row.get("Exported Name") or f"Row{row_idx}"
            template_name = row.get("Template") or "Temp1"

            print(f"\n{'='*60}")
            print(f"Processing Row {row_idx}: {exported_name} (Template: {template_name})")
            print(f"{'='*60}")

            # Helper function to clean color values from Google Sheets
            def clean_color_value(color_str, color_name):
                """Clean and validate color input - always returns valid value"""
                try:
                    if not color_str:
                        return "Default"
                    
                    cleaned = str(color_str).strip()
                    
                    # If empty after strip (was only spaces/tabs/newlines)
                    if not cleaned:
                        return "Default"
                    
                    # If user explicitly wrote "default"
                    if cleaned.lower() == "default":
                        return "Default"
                    
                    # Remove common issues
                    cleaned = cleaned.replace(" ", "").replace("\n", "").replace("\t", "")
                    
                    # Add # if missing (user pasted FF0000 instead of #FF0000)
                    if not cleaned.startswith("#"):
                        cleaned = "#" + cleaned
                        print(f"  [INFO] Added # to {color_name}: {cleaned}")
                    
                    return cleaned
                except Exception as e:
                    print(f"  [WARN] Error cleaning {color_name} '{color_str}': {e} - using Default")
                    return "Default"
            
            # Primary text size - always returns valid number
            text_size_raw = str(row.get("Text Size Percentage", "100")).strip()
            if not text_size_raw:
                text_size_input = 100
                print(f"  [SIZE] Text Size: 100% (empty - using default)")
            else:
                try:
                    # Remove common issues: %, commas, spaces
                    text_size_cleaned = text_size_raw.replace("%", "").replace(",", "").replace(" ", "").strip()
                    text_size_input = int(float(text_size_cleaned))
                    if text_size_input <= 0 or text_size_input > 500:
                        print(f"  [WARN] Text Size {text_size_input}% out of range (1-500) - using 100%")
                        text_size_input = 100
                    else:
                        print(f"  [SIZE] Text Size: {text_size_input}%")
                except Exception as e:
                    print(f"  [WARN] Invalid Text Size '{text_size_raw}' ({e}) - using 100%")
                    text_size_input = 100

            # Colors and additional values - always valid
            bg_color_input = clean_color_value(row.get("Background Color", ""), "Background Color")
            primary_text_color_input = clean_color_value(row.get("Primary Text Color", ""), "Primary Text Color")
            cta_color_input = clean_color_value(row.get("CTA Color", ""), "CTA Color")
            button_text_input = row.get("Button Text", "")
            button_text_color_input = clean_color_value(row.get("Button Text Color", ""), "Button Text Color")
            button_color_input = clean_color_value(row.get("Button Color", ""), "Button Color")
            
            # Debug output - show what was read
            print(f"  [COLOR] BG: {bg_color_input}, Text: {primary_text_color_input}, CTA: {cta_color_input}")
            print(f"  [COLOR] Button BG: {button_color_input}, Button Text: {button_text_color_input}")

            with Session() as ps:
                template_psd = os.path.join(PHOTOSHOP_TEMPLATES_FOLDER, f"{template_name}.psd")
                if not os.path.isfile(template_psd):
                    print(f"ERROR: Template not found: {template_psd}")
                    continue

                # DISABLE ALL DIALOGS (including Missing Fonts dialog)
                # 3 = DisplayNoDialogs constant in Photoshop API
                ps.app.displayDialogs = 3
                
                ps.app.Open(template_psd)
                time.sleep(0.5)  # Let Photoshop finish opening
                doc = ps.app.activeDocument
                
                # FIX MISSING FONTS AUTOMATICALLY
                fix_missing_fonts(doc, ps, fallback_font="ArialMT")

                # Text layers
                try:
                    main_text_layer = doc.ArtLayers[MAIN_TEXT_LAYER_NAME]
                    cta_text_layer  = doc.ArtLayers[CTA_TEXT_LAYER_NAME]
                except Exception as e:
                    print(f"ERROR Row {row_idx}: Missing '{MAIN_TEXT_LAYER_NAME}' or '{CTA_TEXT_LAYER_NAME}' in template '{template_name}'")
                    print(f"       Skipping this row. Please fix the PSD template: {template_psd}")
                    doc.Close(ps.SaveOptions.DoNotSaveChanges)
                    continue

                # Primary text + font logic
                main_text = row.get("Primary Text", "")
                main_text_layer.TextItem.contents = main_text
                
                # Try to set BebasNeue font if conditions are met
                try:
                    if len(main_text) > 54 and text_size_input > 90:
                        main_text_layer.TextItem.font = "BebasNeue"
                    if text_size_input == 101:
                        main_text_layer.TextItem.font = "BebasNeue"
                except Exception as e:
                    print(f"  [WARN] Could not set BebasNeue font: {e}. Using default font.")

                # CTA
                cta_text = row.get("CTA", "")
                cta_text_layer.TextItem.contents = cta_text

                # Button text if present
                btn_exists = False
                try:
                    btn_text_layer = doc.ArtLayers[TEMP_BUTTON_TEXT_LAYER_NAME]
                    btn_text_layer.TextItem.contents = button_text_input
                    btn_exists = True
                except Exception:
                    print("Notice: 'Temp_Button_Text' layer not found. Skipping button text update.")

                # Resize the primary text only
                if text_size_input != 100:
                    adjust_text_sizes(ps, text_size_input)
                
                # Prevent text overflow - auto-shrink if text is too long
                prevent_text_overflow(ps, MAIN_TEXT_LAYER_NAME)

                # Placeholder
                try:
                    placeholder_layer = doc.ArtLayers[PLACEHOLDER_LAYER_NAME]
                except Exception:
                    print(f"ERROR Row {row_idx}: Missing placeholder layer '{PLACEHOLDER_LAYER_NAME}' in template '{template_name}'")
                    print(f"       Skipping this row. Please fix the PSD template: {template_psd}")
                    doc.Close(ps.SaveOptions.DoNotSaveChanges)
                    continue

                # Colors - Apply colors with clear feedback
                if is_valid_hex_color(bg_color_input):
                    print(f"  [OK] Applying background color: {bg_color_input}")
                    _ = select_bg_color_layer(ps)
                    try:
                        doc.selection.selectAll()
                    except Exception as e:
                        print(f"  [WARN] Error selecting canvas for BG fill: {e}")
                    else:
                        fill_active_selection_with_color(ps, bg_color_input, fill_opacity=100)
                else:
                    print(f"  [DEFAULT] Background color '{bg_color_input}' - using template default")
                    clear_bg_color_layer(ps)

                if is_valid_hex_color(primary_text_color_input):
                    print(f"  [OK] Applying primary text color: {primary_text_color_input}")
                    set_text_layer_color(ps, MAIN_TEXT_LAYER_NAME, primary_text_color_input)
                else:
                    print(f"  [DEFAULT] Primary text color '{primary_text_color_input}' - using template default")

                if is_valid_hex_color(cta_color_input):
                    print(f"  [OK] Applying CTA color: {cta_color_input}")
                    set_text_layer_color(ps, CTA_TEXT_LAYER_NAME, cta_color_input)
                else:
                    print(f"  [DEFAULT] CTA color '{cta_color_input}' - using template default")

                if btn_exists:
                    if is_valid_hex_color(button_text_color_input):
                        print(f"  [OK] Applying button text color: {button_text_color_input}")
                        set_text_layer_color(ps, TEMP_BUTTON_TEXT_LAYER_NAME, button_text_color_input)
                    else:
                        print(f"  [DEFAULT] Button text color '{button_text_color_input}' - using template default")

                    if is_valid_hex_color(button_color_input):
                        print(f"  [OK] Applying button BG color: {button_color_input}")
                        try:
                            _ = doc.ArtLayers["Btn_Color"]
                            btn_bg_exists = True
                        except Exception:
                            btn_bg_exists = False

                        if btn_bg_exists:
                            _ = select_btn_color_layer(ps)
                            try:
                                doc.selection.selectAll()
                            except Exception as e:
                                print(f"  [WARN] Error selecting button fill: {e}")
                            else:
                                fill_active_selection_with_color(ps, button_color_input, fill_opacity=100)
                        else:
                            print("  [WARN] No 'Btn_Color' layer found - skipping button BG fill")
                    else:
                        print(f"  [DEFAULT] Button BG color '{button_color_input}' - using template default")
                        clear_btn_color_layer(ps)

                # Stock images
                uploaded_urls = []
                
                # CLEAN OLD PLACED IMAGES BEFORE PROCESSING NEW ONES
                clean_placed_images(doc, placeholder_layer)

                for i in range(1, MAX_STOCK_COLS + 1):
                    # Support flexible column names: Stock#1, Stock #1, Stock 1, stock#1, etc.
                    def canon_col(s):
                        return ''.join(ch for ch in s.lower() if ch.isalnum() or ch == '#')
                    
                    col_name_target = f"stock#{i}"
                    col_name = next((k for k in row.keys() if canon_col(k) == col_name_target), None)
                    if not col_name:
                        continue
                    
                    cell_value = row.get(col_name)
                    if not cell_value:
                        continue
                    
                    # Extract all URLs using regex to handle complex URLs with semicolons, commas, etc.
                    # This regex finds URLs starting with http:// or https:// and ending at whitespace/newline
                    url_pattern = r'https?://[^\s]+'
                    urls = re.findall(url_pattern, str(cell_value))
                    
                    # Fallback: if no URLs found with regex, try old split method
                    if not urls:
                        urls = [u.strip() for u in re.split(r'[\s,]+', str(cell_value).strip()) if u.strip().startswith('http')]
                    
                    for url in urls:
                        try:
                            image_index = len(uploaded_urls) + 1
                            image_name  = generate_image_name(exported_name, template_name, image_index)

                            print(f"Processing URL: {url[:80]}...")
                            content = get_with_retry(url)
                            print(f"Downloaded {len(content)} bytes")
                            
                            img = Image.open(BytesIO(content))
                            img = ImageOps.exif_transpose(img)
                            print(f"Image opened: size={img.size}, mode={img.mode}")
                            
                            # Convert RGBA/P to RGB for JPEG compatibility
                            if img.mode in ('RGBA', 'LA', 'P'):
                                print(f"Converting {img.mode} to RGB...")
                                rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                                if img.mode == 'P':
                                    img = img.convert('RGBA')
                                rgb_img.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                                img = rgb_img
                                print("Conversion complete")

                            temp_path = os.path.join(TEMP_STOCKS_FOLDER, f"temp_{image_name}.jpg")
                            img.save(temp_path, "JPEG", quality=95, optimize=True)
                            print(f"Saved temp JPEG: {os.path.basename(temp_path)}")

                            time.sleep(0.3)  # Let Photoshop breathe before paste
                            placed_layer = place_and_fit_image(doc, placeholder_layer, temp_path, ps)
                            time.sleep(0.3)  # Let Photoshop finish paste/resize

                            output_path = os.path.join(OUTPUT_FOLDER, f"{image_name}.jpg")
                            jpg_options = ps.JPEGSaveOptions(quality=12)
                            doc.saveAs(output_path, jpg_options, asCopy=True)

                            storage_url = upload_to_storage(bucket, output_path, image_name)
                            uploaded_urls.append(storage_url)

                            # Delete the placed layer immediately after saving
                            try:
                                placed_layer.Delete()
                                print(f"  [DELETE] Deleted placed layer: {placed_layer.name}")
                            except Exception as e:
                                print(f"  [WARN] Could not delete placed layer: {e}")
                            
                            # Remove temporary file
                            try:
                                os.remove(temp_path)
                            except Exception as e:
                                print(f"  [WARN] Could not remove temp file: {e}")

                            print(f"[OK] Completed: {image_name}")

                        except Exception as e:
                            print(f"Error processing image '{url}': {e}")
                            continue

                # Write back to the sheet (columns starting from Y)
                for idx, url in enumerate(uploaded_urls):
                    col = url_start_col + idx
                    sheet.update_cell(row_idx, col, url)

                print(f"Completed row {row_idx}: {exported_name}")

                # Reset color layers
                try:
                    clear_bg_color_layer(ps)
                except Exception as e:
                    print("Error clearing Bg_Color:", e)
                try:
                    clear_btn_color_layer(ps)
                except Exception as e:
                    print("Error clearing Btn_Color:", e)

                # Close the document without saving
                try:
                    doc.Close(ps.SaveOptions.DoNotSaveChanges)
                except Exception as e:
                    print("Error closing PSD:", e)
        
        except Exception as e:
            print(f"\n{'='*60}")
            print(f"ERROR processing row {row_idx}: {exported_name if 'exported_name' in locals() else 'Unknown'}")
            print(f"Error: {str(e)}")
            print(f"Skipping to next row...")
            print(f"{'='*60}\n")
            continue

def main():
    os.makedirs(PHOTOSHOP_TEMPLATES_FOLDER, exist_ok=True)
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    os.makedirs(TEMP_STOCKS_FOLDER, exist_ok=True)

    print("=== Run Info ===")
    print("Python exe:", sys.executable)
    print("Working dir:", os.getcwd())
    print("Templates :", PHOTOSHOP_TEMPLATES_FOLDER)
    print("Output    :", OUTPUT_FOLDER)
    print("Temp      :", TEMP_STOCKS_FOLDER)

    psds = glob.glob(os.path.join(PHOTOSHOP_TEMPLATES_FOLDER, "*.psd"))
    print("Found PSDs:", [os.path.basename(p) for p in psds])

    print("Service account file exists?:", os.path.isfile(SERVICE_ACCOUNT_FILE), "->", SERVICE_ACCOUNT_FILE)
    try:
        import json
        print("SA email (both Sheets+GCS):", json.load(open(SERVICE_ACCOUNT_FILE, "r", encoding="utf-8")).get("client_email"))
        print("Bucket name               :", BUCKET_NAME)
    except Exception:
        pass

    print("\nStarting image processing and upload...")
    process_and_upload_images()

    print("\nCleaning up temporary files...")
    for filename in os.listdir(TEMP_STOCKS_FOLDER):
        file_path = os.path.join(TEMP_STOCKS_FOLDER, filename)
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

    print("Process completed successfully!")

if __name__ == "__main__":
    main()
