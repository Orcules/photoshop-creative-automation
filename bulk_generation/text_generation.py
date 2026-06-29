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
BULKOS_SHEET         = "Bulkos"
IMAGOS_SHEET         = "ImgenBulk"   # your ImagenBulk sheet
OPENAI_API_KEY       = os.environ.get("OPENAI_API_KEY", "")

policy_rules = """
STRICT COMPLIANCE RULES (Goal: Inducing clicks to informational ARTICLES)
1. No false promises, no unfulfillable offers, no hard CTAs (e.g., "Apply now", "Get a free X").
2. Headline/body must accurately describe what users will find out on the destination page.
3. Stick to invite-to-learn verbs: Learn, Explore, Find out, See, See How it Works, See The Full Guide, This is How... , etc. 
4. No pricing or availability claims unless they are explicitly shown on the article page.
5. No deceptive urgency ("Only today!", "Hurry!").
6. No identity misrepresentation.
7. Localize copy so it reads naturally to natives in TARGET LANGUAGE.
8. Don't mention any brands in headline, body, or image overlay text.
9. Avoid hard, exaggerated claims such as: Life-Changing, Like Never Before, Revolutionary, Essential etc.
10. Keep all ads POSITIVE, flip focus from the pain to the desired outcome.
11. Avoid AI-like words such as UNLOCK, UNVEIL, UNCOVER, UNEARTH, REVEALED; write naturally.
12. Do not use “CLICK TO…”, “TAP TO…”, or “FOLLOW THE LINK…”. Keep CTAs natural.
13. Text on Image ≤ 52 characters.
14. Additional Txt on Image ≤ 45 characters.
15. Facebook Headline must NOT contain verbs.
16. No texts should give output with LINE BREAKS inside cells.
"""

def authorize_gsheets(keyfile):
    creds = Credentials.from_service_account_file(keyfile, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)

def get_sheet(client, ssid, name):
    return client.open_by_key(ssid).worksheet(name)

def call_openai(prompt, system_role, model="gpt-4.1-mini", temp=0.7):
    resp = openai.chat.completions.create(
        model=model,
        messages=[
            {"role":"system","content":system_role},
            {"role":"user","content":prompt},
        ],
        max_tokens=600,
    )
    return resp.choices[0].message.content

def parse_json_response(text, keys):
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON found in response:\n{text}")
    obj = json.loads(m.group(0))
    return tuple(obj.get(k, "") for k in keys)

def fits_two_lines(text, max_len=26):
    words = text.split()
    length = count = 0
    for w in words:
        inc = len(w) + (1 if length>0 else 0)
        if length + inc <= max_len:
            length += inc
            count += 1
        else:
            break
    line2 = " ".join(words[count:])
    return length <= max_len and len(line2) <= max_len

def main():
    openai.api_key = OPENAI_API_KEY
    client        = authorize_gsheets(SERVICE_ACCOUNT_FILE)
    sheet_txt     = get_sheet(client, SPREADSHEET_ID, TEXTOS_SHEET)
    sheet_bulk    = get_sheet(client, SPREADSHEET_ID, BULKOS_SHEET)
    sheet_img     = get_sheet(client, SPREADSHEET_ID, IMAGOS_SHEET)

    idx_txt  = {h: i+1 for i, h in enumerate(sheet_txt.row_values(1))}
    idx_bulk = {h: i+1 for i, h in enumerate(sheet_bulk.row_values(1))}

    # — Build lookup: for each Custom Name Addition, accumulate ALL Stock Urls from every row —
    imagen_map = {}
    for r in sheet_img.get_all_records():
        key = r.get("Custom Name Addition", "").strip()
        if not key:
            continue
        urls = [r.get(f"Stock Url #{i}", "").strip() for i in range(1,5)]
        imagen_map.setdefault(key, []).extend(u for u in urls if u)

    updates_txt  = []
    updates_bulk = []

    for row_i, row in enumerate(sheet_txt.get_all_records(), start=2):
        vertical = row.get("Vertical","").strip()
        language = row.get("Language","").strip()
        if not vertical or not language:
            print(f"Row {row_i}: blank Vertical/Language → stopping.")
            break

        aq = row.get("Article Query","").strip()
        at = row.get("Article Text","").strip()
        ip = row.get("Img Prompt","").strip()
        tp = row.get("Txt Prompt","").strip()
        if not ip or not tp:
            print(f"Row {row_i}: missing Img/Txt Prompt → skipping.")
            continue

        # — Image overlay generation —
        ov_sys  = "You are an expert Facebook ads image text generator."
        ov_base = (
            f"Language: {language}\n"
            f"Vertical: {vertical}\n"
            f"Article Query: {aq}\n"
            f"Article Text: {at}\n"
            f"Img Prompt: {ip}\n"
            f"Policy Rules: {policy_rules}\n\n"
            "Instruction: Generate JSON with keys: text_on_image, cta_on_image, additional_txt_on_image.\n"
        )
        toi = cta = add_txt = ""
        for _ in range(5):
            resp = call_openai(ov_base, ov_sys)
            raw, cta_, add_ = parse_json_response(resp, [
                "text_on_image","cta_on_image","additional_txt_on_image"
            ])
            if fits_two_lines(raw):
                toi, cta, add_txt = raw, cta_, add_
                break
            ov_base += "\nPlease shorten 'text_on_image' to two lines ≤26 chars.\n"
        else:
            print(f"Row {row_i}: overlay fallback → using Article Query.")
            toi = aq

        def q_txt(col, val):
            a1 = rowcol_to_a1(row_i, idx_txt[col])
            updates_txt.append({"range": a1, "values": [[val]]})

        q_txt("Text on Image",           toi)
        q_txt("CTA on Image",            cta)
        q_txt("Additional Txt on Image", add_txt)

        # — Ad copy generation —
        cp_sys = "You are an expert Facebook ads copywriter."
        cp_pr  = (
            f"Language: {language}\n"
            f"Vertical: {vertical}\n"
            f"Article Query: {aq}\n"
            f"Article Text: {at}\n"
            f"Txt Prompt: {tp}\n"
            f"Policy Rules: {policy_rules}\n\n"
            "Instruction: Generate JSON with keys: facebook_headline, facebook_body_text.\n"
        )
        fb_h, fb_b = parse_json_response(call_openai(cp_pr, cp_sys),
                                         ["facebook_headline","facebook_body_text"])
        q_txt("Facebook Headline",  fb_h)
        q_txt("Facebook Body Text", fb_b)

        def q_bulk(col, val):
            a1 = rowcol_to_a1(row_i, idx_bulk[col])
            updates_bulk.append({"range": a1, "values": [[val]]})

        custom_key = aq.replace(" ", "_")
        q_bulk("Vertical",             vertical)
        q_bulk("Language",             language)
        q_bulk("Custom Name Addition", custom_key)
        q_bulk("Primary Text",         toi)
        q_bulk("CTA",                  cta)
        q_bulk("Button Text",          add_txt)

        # — Sample 4 random stocks from all rows sharing this key —
        stocks = imagen_map.get(custom_key, [])
        if stocks:
            picks = random.sample(stocks, min(4, len(stocks)))
            for i in range(1,5):
                url = picks[i-1] if i-1 < len(picks) else ""
                # write into Bulkos “Stock#1”…“Stock#4”
                q_bulk(f"Stock#{i}", url)

        print(f"Row {row_i} queued.")

    # apply batch updates
    if updates_txt:
        sheet_txt.batch_update(updates_txt)
    if updates_bulk:
        sheet_bulk.batch_update(updates_bulk)

    print("All done!")

if __name__ == "__main__":
    main()
