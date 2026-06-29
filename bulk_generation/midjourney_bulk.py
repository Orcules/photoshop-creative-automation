import os
import requests
import time
import datetime
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google.cloud import storage
from PIL import Image
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

# ----------------------------------------------------------------
# 1) Google Sheets and GCS Setup
# ----------------------------------------------------------------
SHEET_ID = os.environ.get('SHEET_ID', 'YOUR_SHEET_ID')
SHEET_NAME = 'ImgenBulk'
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

# Credentials for Sheets
creds = Credentials.from_service_account_file(
    'service_account.json',
    scopes=SCOPES
)
service = build('sheets', 'v4', credentials=creds)
client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID)
worksheet = sheet.worksheet(SHEET_NAME)

# Credentials for Google Cloud Storage
def initialize_storage():
    storage_client = storage.Client.from_service_account_json('service_account.json')
    return storage_client.get_bucket('your-gcs-bucket')  # Replace with your bucket name

bucket = initialize_storage()

# ----------------------------------------------------------------
# 2) Midjourney API Key
# ----------------------------------------------------------------
API_KEY = os.environ.get("MIDJOURNEY_API_KEY", "")

# ----------------------------------------------------------------
# 3) Utility: Find a column by header text in row 1
# ----------------------------------------------------------------
def get_column_index_by_header(header_name: str) -> int:
    """
    Looks at row 1 of the worksheet for the exact header_name (case-insensitive).
    Returns its 1-based column index if found, else 0.
    """
    headers = worksheet.row_values(1)  # row #1
    for idx, val in enumerate(headers, start=1):
        if val.strip().lower() == header_name.strip().lower():
            return idx
    return 0

# ----------------------------------------------------------------
# 4) Check for banned content
# ----------------------------------------------------------------
def is_banned_prompt(prompt: str) -> bool:
    banned_terms = ["lingerie"]
    return any(term.lower() in prompt.lower() for term in banned_terms)

# ----------------------------------------------------------------
# 5) Call Midjourney (via goapi.ai) to generate the 2×2 collage
# ----------------------------------------------------------------
def call_midjourney_api(prompt: str) -> str:
    """
    Submits an 'imagine' task to the Midjourney API, polls until done.
    Returns the final collage URL if successful, otherwise None.
    """
    url = 'https://api.goapi.ai/api/v1/task'
    headers = {'X-API-KEY': API_KEY}

    # Example: remove specific MJ flags if you want
    cleaned_prompt = prompt.replace('--ar 1.91:1', '').strip()

    data = {
        "model": "midjourney",
        "task_type": "imagine",
        "input": {
            "prompt": cleaned_prompt,
            "process_mode": "relax",
            "skip_prompt_check": False
        }
    }

    try:
        # 1) Start the task
        resp = requests.post(url, json=data, headers=headers)
        resp_json = resp.json()

        if resp.status_code != 200 or 'data' not in resp_json:
            print(f"[Midjourney] Error starting task: {resp_json}")
            return None

        task_id = resp_json['data']['task_id']
        print(f"[Midjourney] Task ID for prompt '{prompt}': {task_id}")

        # 2) Poll for completion
        while True:
            time.sleep(30)  # poll every 30s
            status_url = f"https://api.goapi.ai/api/v1/task/{task_id}"
            st_resp = requests.get(status_url, headers=headers)
            st_json = st_resp.json()

            if st_resp.status_code == 200 and 'data' in st_json:
                status = st_json['data']['status']
                print(f"[Midjourney] Status for prompt '{prompt}': {status}")

                if status == 'completed':
                    # Usually in output.image_url or .image_urls
                    output = st_json['data'].get('output', {})
                    mj_url = output.get('image_url')
                    if not mj_url:
                        # fallback
                        mj_urls = output.get('image_urls', [])
                        mj_url = mj_urls[0] if mj_urls else None
                    print(f"[Midjourney] Completed image URL: {mj_url}")
                    return mj_url
                elif status == 'failed':
                    print(f"[Midjourney] Task failed for '{prompt}': {st_json}")
                    return None
            else:
                print(f"[Midjourney] Error polling status: {st_resp.status_code}, {st_resp.text}")
                return None

    except Exception as e:
        print(f"[Midjourney] Exception: {e}")
        return None

# ----------------------------------------------------------------
# 6) Resize to ~250KB if needed
# ----------------------------------------------------------------
def resize_image_to_250kb(orig_buf: BytesIO) -> BytesIO:
    """
    Compress the image to ~250KB via JPEG quality adjustments.
    If original is already under 250KB, we skip. Always returns
    a valid BytesIO with the stream rewound to .seek(0).
    """
    TARGET_SIZE = 250 * 1024
    MIN_SIZE    = 230 * 1024

    orig_buf.seek(0)
    orig_data = orig_buf.getvalue()
    original_size = len(orig_data)

    if original_size == 0:
        print("[Resize] Warning: empty input stream.")
        orig_buf.seek(0)
        return orig_buf

    # If already small, no need to re-encode
    if original_size < TARGET_SIZE:
        print(f"[Resize] Image already <250KB: {original_size//1024}KB")
        orig_buf.seek(0)
        return orig_buf

    # Try re-encoding
    try:
        img = Image.open(BytesIO(orig_data))
    except Exception as e:
        print(f"[Resize] Can't open image: {e}")
        orig_buf.seek(0)
        return orig_buf

    if img.mode != 'RGB':
        img = img.convert('RGB')

    best_stream = None
    best_size = 0
    best_quality = None

    # Attempt qualities from 95 down to 60
    for quality in range(95, 59, -1):
        temp = BytesIO()
        img.save(temp, format='JPEG', quality=quality)
        size_now = len(temp.getvalue())

        if size_now <= TARGET_SIZE:
            # see how close to MIN_SIZE
            if best_stream is None or abs(size_now - MIN_SIZE) < abs(best_size - MIN_SIZE):
                best_stream = temp
                best_size = size_now
                best_quality = quality

            # If we are “close enough,” break
            if MIN_SIZE <= size_now <= TARGET_SIZE:
                break

    if best_stream is None:
        # fallback
        fallback_buf = BytesIO()
        img.save(fallback_buf, format='JPEG', quality=80)
        fallback_size = len(fallback_buf.getvalue())
        print(f"[Resize] Could not get under 250KB at higher Q. Using Q=80 => {fallback_size//1024}KB")
        fallback_buf.seek(0)
        return fallback_buf

    best_stream.seek(0)
    print(f"[Resize] final quality={best_quality}, size={best_size//1024}KB (orig {original_size//1024}KB)")
    return best_stream

# ----------------------------------------------------------------
# 7) Upload collage, split it, upload each quadrant
# ----------------------------------------------------------------
def upload_and_split_collage(
    image_url: str,
    row_idx: int,
    collage_col_idx: int,
    stock_col_start: int,
    stock_name_col_idx: int,
    date_str: str
):
    """
    Download the collage, resize if needed, upload to GCS named as:
      {DATE}-{StockFinalName}.jpg
    Then split into 4 quadrants, each:
      {DATE}-{StockFinalName}-1.jpg, etc.
    Updates the sheet for Collage URL and Stock Url #1..#4.

    Args:
      image_url: URL from Midjourney
      row_idx: row in the sheet
      collage_col_idx: column index for "Collage URL"
      stock_col_start: column index for "Stock Url #1"
      stock_name_col_idx: column index for "Stock Final Name"
      date_str: string like "2025-02-11"
    """
    if not image_url:
        return

    try:
        # 1) Read the "Stock Final Name" from the sheet
        stock_final_name = worksheet.cell(row_idx, stock_name_col_idx).value
        if not stock_final_name:
            # fallback
            stock_final_name = f"row{row_idx}"

        # 2) Download collage
        r = requests.get(image_url, stream=True)
        if r.status_code != 200:
            print(f"[Collage] Could not download collage => status {r.status_code}")
            return

        raw_bytes = BytesIO(r.content)
        raw_bytes.seek(0)
        if len(raw_bytes.getvalue()) == 0:
            print("[Collage] Download returned 0 bytes.")
            return

        # 3) Resize if needed
        resized = resize_image_to_250kb(raw_bytes)
        resized.seek(0)

        # 4) Open as PIL
        collage_img = Image.open(resized)

        # 5) Upload entire collage to GCS
        #    => name: {DATE}-{StockName}.jpg
        collage_filename = f"{date_str}-{stock_final_name}.jpg"
        collage_blob = bucket.blob(f"images/{collage_filename}")

        resized.seek(0)
        collage_blob.upload_from_file(resized, content_type='image/jpeg')
        collage_blob.make_public()
        collage_url = collage_blob.public_url

        # Update the sheet’s “Collage URL”
        worksheet.update_cell(row_idx, collage_col_idx, collage_url)

        # 6) Split into 4 quadrants
        width, height = collage_img.size
        quads = [
            (0,           0,           width // 2,      height // 2),
            (width // 2,  0,           width,           height // 2),
            (0,           height // 2, width // 2,      height),
            (width // 2,  height // 2, width,           height),
        ]

        for i, (l, t, r, b) in enumerate(quads):
            cropped = collage_img.crop((l, t, r, b))
            buf = BytesIO()
            cropped.save(buf, format='JPEG', quality=95)
            buf.seek(0)

            # Optional second pass for ~250KB
            buf = resize_image_to_250kb(buf)
            buf.seek(0)

            # naming each quadrant => {DATE}-{StockName}-i.jpg
            quadrant_filename = f"{date_str}-{stock_final_name}-{i+1}.jpg"
            q_blob = bucket.blob(f"images/{quadrant_filename}")

            buf.seek(0)
            q_blob.upload_from_file(buf, content_type='image/jpeg')
            q_blob.make_public()

            # Stock Url #1..#4 are presumably consecutive columns
            worksheet.update_cell(row_idx, stock_col_start + i, q_blob.public_url)

    except Exception as e:
        print(f"[Collage] Error processing row {row_idx}: {e}")

# ----------------------------------------------------------------
# 8) Main routine: read "Final Prompt" => generate => upload
# ----------------------------------------------------------------
def process_final_prompts():
    # 1) Identify columns
    final_prompt_col    = get_column_index_by_header("Final prompt")
    collage_col         = get_column_index_by_header("Collage URL")
    stock1_col          = get_column_index_by_header("Stock Url #1")
    stock2_col          = get_column_index_by_header("Stock Url #2")
    stock3_col          = get_column_index_by_header("Stock Url #3")
    stock4_col          = get_column_index_by_header("Stock Url #4")
    stock_final_name_col= get_column_index_by_header("Stock Final Name")

    # basic validation
    needed = [final_prompt_col, collage_col, stock1_col, stock2_col, stock3_col, stock4_col, stock_final_name_col]
    if not all(needed):
        print("ERROR: Missing a required column header. Check your sheet ('Final prompt', 'Collage URL', 'Stock Url #1..#4', 'Stock Final Name').")
        return

    # Output columns that mark a row as already processed
    output_cols = [collage_col, stock1_col, stock2_col, stock3_col, stock4_col]

    # 2) Read all rows (row 2 downward)
    all_values = worksheet.get_all_values()
    all_rows = all_values[1:]  # skip header row

    # 3) Prepare today's date string
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")

    # 4) Parallel generation
    futures = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        for i, row in enumerate(all_rows, start=2):  # row i
            prompt = row[final_prompt_col - 1].strip() if len(row) >= final_prompt_col else ""
            if not prompt:
                continue
            if is_banned_prompt(prompt):
                print(f"[Row {i}] Banned term => skipping.")
                continue

            # Skip if any output column already has a link
            if any(len(row) >= col and row[col - 1].strip() for col in output_cols):
                print(f"[Row {i}] Output already exists => skipping.")
                continue

            f = executor.submit(call_midjourney_api, prompt)
            futures.append((f, i))

        # Gather
        for f, row_idx in futures:
            try:
                collage_url = f.result()
                if collage_url:
                    upload_and_split_collage(
                        image_url       = collage_url,
                        row_idx         = row_idx,
                        collage_col_idx = collage_col,
                        stock_col_start = stock1_col,
                        stock_name_col_idx = stock_final_name_col,
                        date_str        = date_str
                    )
                else:
                    print(f"[Row {row_idx}] Midjourney returned no collage URL.")
            except Exception as e:
                print(f"[Row {row_idx}] Exception: {e}")

# ----------------------------------------------------------------
# 9) Run the script
# ----------------------------------------------------------------
if __name__ == "__main__":
    process_final_prompts()
    print("Done processing all Final Prompts.")
