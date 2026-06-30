import os
import cv2
import numpy as np
import requests
import logging
import easyocr
from bs4 import BeautifulSoup
from supabase import Client, ClientOptions, create_client

# --- 1. SETUP & LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("OmniScrubber")

SUPABASE_URL = "http://127.0.0.1:54321"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0.EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"

try:
    opts = ClientOptions(schema="core")
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY, options=opts)
    logger.info("Supabase client initialized successfully.")
except Exception as e:
    logger.error(f"Supabase client failed to initialize: {e}")

# --- 2. INITIALIZE EASYOCR ---
logger.info("⏳ Initializing EasyOCR Models (This may take a moment...)")
OCR_READER = easyocr.Reader(['en'], gpu=False)
logger.info("✅ EasyOCR Ready!")


def wash_html_block(raw_html: str, q_id: str, tag_type: str) -> tuple[str, int]:
    """Takes raw HTML, wipes faint watermarks, uses OCR to find logos, and intelligently erases them."""
    if not raw_html or "storage.googleapis.com" not in raw_html:
        return raw_html, 0

    soup = BeautifulSoup(raw_html, "html.parser")
    img_tags = soup.find_all("img")
    washed_count = 0

    for idx, img in enumerate(img_tags):
        old_src = img.get("src", "")
        if "storage.googleapis.com" not in old_src:
            continue

        fetch_url = old_src if "http" in old_src else f"http:{old_src}"

        try:
            # A. Download bytes to RAM
            resp = requests.get(fetch_url, timeout=10)
            resp.raise_for_status()

            bgr = cv2.imdecode(np.asarray(bytearray(resp.content), dtype="uint8"), cv2.IMREAD_COLOR)
            if bgr is None:
                continue

            img_h, img_w = bgr.shape[:2]
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            
            # =================================================================
            # B. WEAPON 1: Global Faint Watermark Wipe
            # =================================================================
            bgr[gray > 235] = 255
            
            clean_gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            mask = np.zeros(clean_gray.shape, dtype=np.uint8)
            
            logos_removed = 0
            logos_to_inpaint = 0

            # =================================================================
            # C. WEAPON 2: Full-Image EasyOCR Text Detection
            # =================================================================
            ocr_results = OCR_READER.readtext(clean_gray)
            
            for (bbox, text, prob) in ocr_results:
                clean_text = text.lower().replace(" ", "")
                target_words = ["testbook", "tesibook", "testb", "estbook", "tbook"]
                
                if any(target in clean_text for target in target_words):
                    x_coords = [p[0] for p in bbox]
                    y_coords = [p[1] for p in bbox]
                    x_min, x_max = int(min(x_coords)), int(max(x_coords))
                    y_min, y_max = int(min(y_coords)), int(max(y_coords))

                    # Expand the bounding box to the LEFT to swallow the blue book icon
                    text_width = x_max - x_min
                    pad_left = int(text_width * 0.55) 
                    pad_right = 10
                    pad_y = 15
                    
                    x1 = max(0, x_min - pad_left)
                    y1 = max(0, y_min - pad_y)
                    x2 = min(img_w, x_max + pad_right)
                    y2 = min(img_h, y_max + pad_y)

                    # --- SMART CONTEXT-AWARE ERASURE ---
                    patch = clean_gray[y1:y2, x1:x2]
                    
                    if patch.shape[0] > 0 and patch.shape[1] > 0:
                        top_edge = patch[0, :]
                        bottom_edge = patch[-1, :]
                        left_edge = patch[:, 0]
                        right_edge = patch[:, -1]
                        border_pixels = np.concatenate([top_edge, bottom_edge, left_edge, right_edge])
                        
                        # Check median background color around the logo
                        if np.median(border_pixels) > 235:
                            # Diagram detected (White background): Solid White Box
                            cv2.rectangle(bgr, (x1, y1), (x2, y2), (255, 255, 255), -1)
                            logger.info(f"   [OCR Tracker] Found '{text}'. Diagram detected -> Solid White Box applied.")
                        else:
                            # Photograph detected: Add to mask for inpainting
                            cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
                            logos_to_inpaint += 1
                            logger.info(f"   [OCR Tracker] Found '{text}'. Photo detected -> Inpainting applied.")

                    logos_removed += 1
                    break # Stop reading this image once the logo is handled

            # =================================================================
            # D. Inpainting (ONLY for photographic backgrounds)
            # =================================================================
            if logos_to_inpaint > 0:
                radius = max(3, int(img_w * 0.01))
                bgr = cv2.inpaint(bgr, mask, inpaintRadius=radius, flags=cv2.INPAINT_TELEA)

            # E. Re-encode and Upload
            success, encoded_png = cv2.imencode(".png", bgr)
            if not success:
                continue

            file_path = f"diagrams/{q_id}_{tag_type}_{idx}.png"

            supabase.storage.from_("sanitized-diagrams").upload(
                path=file_path,
                file=encoded_png.tobytes(),
                file_options={"content-type": "image/png"},
            )

            new_url = f"{SUPABASE_URL}/storage/v1/object/public/sanitized-diagrams/{file_path}"
            img["src"] = new_url
            washed_count += 1
            
            label = "Question Prompt" if tag_type == "q" else "Solution Answer"
            if logos_removed == 0:
                logger.info(f"[{label} Washed] Img #{idx+1} for ID: {q_id} (Clean diagram, safely preserved)")

        except Exception as e:
            logger.error(f"Failed on {fetch_url}: {e}")

    return str(soup), washed_count

def execute_total_omni_reset():
    logger.info("🚀 INITIATING SMART OCR OMNI-SCRUBBER...")
    logger.info("Scanning EVERY row for Google links in Questions OR Solutions...\n")

    response = (
        supabase.table("question")
        .select("id, question_text, solution_text")
        .or_("question_text.ilike.%storage.googleapis.com%,solution_text.ilike.%storage.googleapis.com%")
        .execute()
    )

    rows = response.data

    if not rows:
        logger.warning("⚠️ ZERO DIRTY ROWS FOUND! (Run 'npx supabase db reset' if testing)")
        return

    logger.info(f"🎯 Found {len(rows)} infected rows. Commencing double-column scrub...\n")

    total_q_washed, total_s_washed = 0, 0

    for row in rows:
        q_id = row["id"]
        new_q_html, q_count = wash_html_block(row["question_text"], q_id, "q")
        new_s_html, s_count = wash_html_block(row["solution_text"], q_id, "s")

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
    print("=" * 55)

if __name__ == "__main__":
    execute_total_omni_reset()