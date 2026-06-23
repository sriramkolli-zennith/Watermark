import cv2
import numpy as np
import requests
from bs4 import BeautifulSoup
from supabase import Client, ClientOptions, create_client

SUPABASE_URL = "http://127.0.0.1:54321"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0.EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"

opts = ClientOptions(schema="core")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY, options=opts)


def wash_html_block(raw_html: str, q_id: str, tag_type: str) -> tuple[str, int]:
    """Takes raw HTML, washes any Google images inside it, returns (new_html, washed_count)"""
    if not raw_html or "storage.googleapis.com" not in raw_html:
        return raw_html, 0

    soup = BeautifulSoup(raw_html, "html.parser")
    img_tags = soup.find_all("img")
    washed_count = 0

    for idx, img in enumerate(img_tags):
        old_src = img.get("src", "")

        if "storage.googleapis.com" not in old_src:
            continue

        # Catch protocol-relative URLs (e.g. "//storage.googleapis.com/...")
        fetch_url = old_src if "http" in old_src else f"http:{old_src}"

        try:
            # 1. Download bytes to RAM
            resp = requests.get(fetch_url, timeout=10)
            resp.raise_for_status()

            bgr = cv2.imdecode(
                np.asarray(bytearray(resp.content), dtype="uint8"),
                cv2.IMREAD_COLOR,
            )

            # 2. High-jump math (> 235 snapped to pure white)
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            bgr[gray > 235] = 255

            success, encoded_png = cv2.imencode(".png", bgr)
            if not success:
                print(f"   ❌ Failed to re-encode PNG: {fetch_url}")
                continue

            # 3. Specific destination inside bucket: diagrams/d0040105_q_0.png
            file_path = f"diagrams/{q_id}_{tag_type}_{idx}.png"

            supabase.storage.from_("sanitized-diagrams").upload(
                path=file_path,
                file=encoded_png.tobytes(),
                file_options={"content-type": "image/png"},
            )

            # 4. Mutate HTML src attribute
            new_url = f"{SUPABASE_URL}/storage/v1/object/public/sanitized-diagrams/{file_path}"
            img["src"] = new_url
            washed_count += 1

            label = "Question Prompt" if tag_type == "q" else "Solution Answer"
            print(f"   [{label} Washed] Img #{idx+1} for ID: {q_id}")

        except Exception as e:
            print(f"   ❌ Failed on {fetch_url}: {e}")

    return str(soup), washed_count


def execute_total_omni_reset():
    print("🚀 INITIATING TOTAL OMNI-RESET SCRUBBER...")
    print("Scanning EVERY row for Google links in Questions OR Solutions...\n")

    # Query all rows where EITHER the question or the solution has a Google link
    response = (
        supabase.table("question")
        .select("id, question_text, solution_text")
        .or_(
            "question_text.ilike.%storage.googleapis.com%,solution_text.ilike.%storage.googleapis.com%"
        )
        .execute()
    )

    rows = response.data

    if not rows:
        print("⚠️ ZERO DIRTY ROWS FOUND!")
        print("Did you remember to run 'psql -f seed-dev.sql' in your terminal first?")
        return

    print(f"🎯 Found {len(rows)} infected rows. Commencing double-column scrub...\n")

    total_q_washed = 0
    total_s_washed = 0

    for row in rows:
        q_id = row["id"]

        # Wash Question column (tag_type = 'q')
        new_q_html, q_count = wash_html_block(row["question_text"], q_id, "q")

        # Wash Solution column (tag_type = 's')
        new_s_html, s_count = wash_html_block(row["solution_text"], q_id, "s")

        # If either column was mutated, execute an SQL update on the row
        if q_count > 0 or s_count > 0:
            supabase.table("question").update(
                {"question_text": new_q_html, "solution_text": new_s_html}
            ).eq("id", q_id).execute()

            total_q_washed += q_count
            total_s_washed += s_count

    print("\n" + "=" * 55)
    print("🎉 MASTER MIGRATION COMPLETE!")
    print(f"   • Question Prompts Sanitized: {total_q_washed} images")
    print(f"   • Solution Answers Sanitized: {total_s_washed} images")
    print(f"   • Total safe files in Bucket: {total_q_washed + total_s_washed} images")
    print("=" * 55)


if __name__ == "__main__":
    execute_total_omni_reset()