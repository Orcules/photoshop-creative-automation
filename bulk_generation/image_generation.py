import os
import json
import re
import random
import gspread
from google.oauth2.service_account import Credentials
import openai
from gspread.utils import rowcol_to_a1

# ——— CONFIGURATION ———
SERVICE_ACCOUNT_FILE = "service_account.json"
SPREADSHEET_ID       = os.environ.get("SHEET_ID", "YOUR_SHEET_ID")
TEXTOS_SHEET         = "Textos"
IMGEBULK_SHEET       = "ImgenBulk"
OPENAI_API_KEY       = os.environ.get("OPENAI_API_KEY", "")

# Hard-coded policy for Midjourney image prompts
policy_image_rules = """
MIDJOURNEY PROMPT RULES (Goal: Create visually compelling, on-brand graphics)
1. Focus on key themes from the article query and article text: be as descriptive as possible.
2. If a person is required in the image, describe how they look/feel and always make them experience positive emotions.
3. Tone: aspirational, positive, clear.
4. Make longer prompts always more than 100 characters
5. If a person is inside the stock, specify how they look and how old they are. Include ages 30 and above.
"""

# Predefined options for 'Description and Action'
description_actions = [
    "Photo taken in the style of Martin Parr and Martha Cooper, documentary photography, using a Canon EOS R6 Mark II Mirrorless camera with natural light",
    "Shot by an award-winning British fashion editorial photographer, moody, early morning light, with a Canon DSLR, prime lens",
    "Shallow-focus, 35mm, photorealistic, Canon EOS 5D Mark IV DSLR, f/5.6 aperture, 1/125 second shutter speed, ISO 100",
    "in the style of Annie Leibovitz, studio lighting, cinematic tone, editorial realism"
]


def authorize_gsheets(keyfile):
    creds = Credentials.from_service_account_file(keyfile, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ])
    return gspread.authorize(creds)


def get_sheet(client, ssid, name):
    return client.open_by_key(ssid).worksheet(name)


def call_openai(prompt, system_role, model="gpt-4.1-mini", temp=0.7):
    resp = openai.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_role},
            {"role": "user",   "content": prompt},
        ],
        max_tokens=300
    )
    return resp.choices[0].message.content


def parse_json_response(text, keys):
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON found in response:\n{text}")
    obj = json.loads(m.group(0))
    return tuple(obj.get(k, "") for k in keys)


def sanitize_custom_name(query):
    # Replace spaces with underscores
    return query.replace(' ', '_')


def main():
    openai.api_key = OPENAI_API_KEY
    client = authorize_gsheets(SERVICE_ACCOUNT_FILE)
    sheet_textos = get_sheet(client, SPREADSHEET_ID, TEXTOS_SHEET)
    sheet_imgen  = get_sheet(client, SPREADSHEET_ID, IMGEBULK_SHEET)

    # Prepare header indexes for Textos only
    headers_textos = sheet_textos.row_values(1)
    idx_textos    = {h: i+1 for i, h in enumerate(headers_textos)}

    updates_textos = []
    new_imgen_rows = []

    # Process each row in Textos
    for row_i, row in enumerate(sheet_textos.get_all_records(), start=2):
        query    = row.get("Article Query", "").strip()
        text     = row.get("Article Text",  "").strip()
        vertical = row.get("Vertical",      "").strip()
        if not query or not text:
            print(f"Row {row_i}: missing Article Query/Text → skipping.")
            continue

        # Build prompts
        sys_role = "You are an expert Midjourney prompt engineer."
        user_prompt = (
            f"Article Query: {query}\n"
            f"Article Text: {text}\n"
            f"Image Policy Rules: {policy_image_rules}\n\n"
            "Instruction: Generate JSON with keys 'mj_prompt_1' and 'mj_prompt_2',"
            " each a concise Midjourney prompt (≤100 characters) for compelling ad visuals."
        )
        print(f"Row {row_i} Midjourney prompts input:\n{user_prompt}\n")

        # Generate with OpenAI
        resp = call_openai(user_prompt, sys_role)
        p1, p2 = parse_json_response(resp, ["mj_prompt_1", "mj_prompt_2"])

        # Queue Textos updates
        def q_textos(col, val):
            a1 = rowcol_to_a1(row_i, idx_textos[col])
            updates_textos.append({"range": a1, "values": [[val]]})
        q_textos("MJ Prompt #1", p1)
        q_textos("MJ Prompt #2", p2)
        print(f"Row {row_i} queued with MJ Prompt #1 and #2.")

        # Sanitize custom name
        custom_name = sanitize_custom_name(query)

        # Prepare 2 rows for ImgenBulk, only columns A–F in order: Vertical, Custom Name Addition, Aspect Ratio, Subject, Description and Action, Style
        for prompt_val in (p1, p2):
            action = random.choice(description_actions)
            new_imgen_rows.append([
                vertical,         # Column A: Vertical
                custom_name,      # Column B: Custom Name Addition
                "3:2",           # Column C: Aspect Ratio
                prompt_val,       # Column D: Subject
                action,           # Column E: Description and Action
                "Photorealistic"  # Column F: Style
            ])

    # Write back to Textos
    if updates_textos:
        sheet_textos.batch_update(updates_textos)
        print("All Midjourney prompts written to Textos.")

    # Append to ImgenBulk (only A–F)
    if new_imgen_rows:
        sheet_imgen.append_rows(new_imgen_rows, value_input_option="USER_ENTERED")
        print(f"{len(new_imgen_rows)} rows appended to ImgenBulk (cols A–F).")


if __name__ == "__main__":
    main()
