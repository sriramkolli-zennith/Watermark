import os
import uuid
import cv2
import numpy as np
import httpx
import logging
import easyocr
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl, Field
from supabase import Client, ClientOptions, create_client

# --- 1. CONFIGURATION & LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("WatermarkScrubber")

app = FastAPI(
    title="Unified Watermark Scrubber API",
    description="Enterprise-grade image sanitization microservice."
)

# In production, pull these from an environment variable (e.g., "https://admin.yourdomain.com")
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS, 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ImagePayload(BaseModel):
    source_url: HttpUrl = Field(..., description="The original Google Cloud Storage URL")

# --- 2. SUPABASE INITIALIZATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://127.0.0.1:54321")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "your_local_key_here")

try:
    opts = ClientOptions(schema="core")
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY, options=opts)
    logger.info("Supabase client initialized successfully.")
except Exception as e:
    logger.error(f"Supabase client failed to initialize: {e}")

# --- 3. INITIALIZE EASYOCR ---
logger.info("⏳ Initializing EasyOCR Models...")
OCR_READER = easyocr.Reader(['en'], gpu=False)
logger.info("✅ EasyOCR Ready!")

# --- 4. CORE SANITIZATION PIPELINE ---
@app.post("/sanitize")
async def sanitize_image(payload: ImagePayload):
    fetch_url = str(payload.source_url)
    
    # Security: Limit image size to prevent memory exhaustion (e.g., 5MB)
    MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024 

    try:
        # A. Async Download (Non-blocking)
        async with httpx.AsyncClient() as client:
            resp = await client.get(fetch_url, timeout=10.0)
            resp.raise_for_status()
            
            if len(resp.content) > MAX_IMAGE_SIZE_BYTES:
                raise HTTPException(status_code=413, detail="Image size exceeds 5MB limit.")

        # B. Decode to BGR Matrix
        raw_array = np.asarray(bytearray(resp.content), dtype="uint8")
        bgr = cv2.imdecode(raw_array, cv2.IMREAD_COLOR)
        
        if bgr is None:
            raise HTTPException(status_code=400, detail="Could not decode image.")

        img_h, img_w = bgr.shape[:2]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        
        # Weapon 1: Global Faint Watermark Wipe
        bgr[gray > 235] = 255
        
        clean_gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        mask = np.zeros(clean_gray.shape, dtype=np.uint8)
        
        logos_removed = 0
        logos_to_inpaint = 0
        
        # Weapon 2: Full-Image EasyOCR Text Detection
        ocr_results = OCR_READER.readtext(clean_gray)
        
        for (bbox, text, prob) in ocr_results:
            clean_text = text.lower().replace(" ", "")
            target_words = ["testbook", "tesibook", "testb", "estbook", "tbook"]
            
            if any(target in clean_text for target in target_words):
                x_coords = [p[0] for p in bbox]
                y_coords = [p[1] for p in bbox]
                x_min, x_max = int(min(x_coords)), int(max(x_coords))
                y_min, y_max = int(min(y_coords)), int(max(y_coords))

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
                    # Look at the pixels forming the border around the logo
                    top_edge = patch[0, :]
                    bottom_edge = patch[-1, :]
                    left_edge = patch[:, 0]
                    right_edge = patch[:, -1]
                    border_pixels = np.concatenate([top_edge, bottom_edge, left_edge, right_edge])
                    
                    # If the median color of the border is white/light, it's a diagram.
                    if np.median(border_pixels) > 235:
                        cv2.rectangle(bgr, (x1, y1), (x2, y2), (255, 255, 255), -1)
                        logger.info(f"   [OCR Tracker] Found '{text}'. White background detected -> Solid White Box applied.")
                    else:
                        # Otherwise, it's a photograph (like the Cheetah). We need to blend it.
                        cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
                        logos_to_inpaint += 1
                        logger.info(f"   [OCR Tracker] Found '{text}'. Photo background detected -> Inpainting applied.")

                logos_removed += 1
                break
        
        # Use Inpainting ONLY for photographic backgrounds
        if logos_to_inpaint > 0:
            radius = max(3, int(img_w * 0.01))
            bgr = cv2.inpaint(bgr, mask, inpaintRadius=radius, flags=cv2.INPAINT_TELEA)

        # E. Encode to PNG
        success, encoded_png = cv2.imencode(".png", bgr)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to encode clean image.")

        # F. Upload to Supabase
        unique_filename = f"live_upload_{uuid.uuid4().hex[:8]}.png"
        file_path = f"diagrams/{unique_filename}"

        upload_res = supabase.storage.from_("sanitized-diagrams").upload(
            path=file_path,
            file=encoded_png.tobytes(),
            file_options={"content-type": "image/png"},
        )
        
        # Validate Upload Success
        if not upload_res or upload_res.status_code != 200:
            logger.error(f"Supabase Upload Failed: {upload_res}")
            raise HTTPException(status_code=502, detail="Failed to upload sanitized image to storage.")

        clean_url = f"{SUPABASE_URL}/storage/v1/object/public/sanitized-diagrams/{file_path}"
        logger.info(f"Successfully sanitized and uploaded: {clean_url}")
        
        return {
            "status": "success",
            "clean_url": clean_url,
            "metadata": {
                "logos_removed": logos_removed,
                "original_size": f"{img_w}x{img_h}"
            }
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP Error downloading image: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to fetch original image: {e.response.status_code}")
    except httpx.RequestError as e:
        logger.error(f"Network Error downloading image: {e}")
        raise HTTPException(status_code=400, detail="Network error while fetching the image.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error during sanitization")
        raise HTTPException(status_code=500, detail="Internal server error during image processing.")

@app.get("/")
def health_check():
    return {
        "status": "Online",
        "service": "Unified Watermark Scrubber API",
        "ocr_engine": "Active"
    }