import os
import uuid
import cv2
import numpy as np
import httpx
import glob
import logging
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

# --- 3. TEMPLATE CACHING ---
LOGO_DIR = "logos"
# Store precomputed grayscale templates to save per-request CPU cycles
LOGO_TEMPLATES_GRAY = []

def load_templates():
    global LOGO_TEMPLATES_GRAY
    LOGO_TEMPLATES_GRAY.clear()
    
    if os.path.exists(LOGO_DIR):
        for ext in ("*.png", "*.jpg", "*.jpeg"):
            for filepath in glob.glob(os.path.join(LOGO_DIR, ext)):
                template_bgr = cv2.imread(filepath, cv2.IMREAD_COLOR)
                if template_bgr is not None:
                    # Convert to grayscale immediately for robust edge/intensity matching
                    template_gray = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)
                    LOGO_TEMPLATES_GRAY.append(template_gray)
        logger.info(f"Loaded {len(LOGO_TEMPLATES_GRAY)} grayscale logo templates.")
    else:
        logger.warning(f"'{LOGO_DIR}/' folder not found. Logo erasure is disabled.")

# Load templates on initial startup
load_templates()

@app.post("/reload-templates")
async def reload_templates_endpoint():
    """Hot-reload templates without restarting the server."""
    load_templates()
    return {"message": f"Reloaded {len(LOGO_TEMPLATES_GRAY)} templates."}

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
        
        logos_removed = 0
        
        # C. Weapon 1: Multi-Scale Grayscale Matching + Non-Maximum Suppression (NMS) + Inpainting
        if LOGO_TEMPLATES_GRAY:
            mask = np.zeros(gray.shape, dtype=np.uint8)
            boxes = []
            scores = []
            
            for template_gray in LOGO_TEMPLATES_GRAY:
                # Test multiple scales
                for scale in np.linspace(0.5, 2.0, 16):
                    try:
                        resized_t = cv2.resize(template_gray, (0, 0), fx=scale, fy=scale)
                        r_h, r_w = resized_t.shape[:2]
                        
                        if r_h > img_h or r_w > img_w:
                            continue
                        
                        # Match on Grayscale (ignores color noise)
                        result = cv2.matchTemplate(gray, resized_t, cv2.TM_CCOEFF_NORMED)
                        threshold = 0.60
                        locations = np.where(result >= threshold)
                        
                        for pt in zip(*locations[::-1]):
                            boxes.append([int(pt[0]), int(pt[1]), int(r_w), int(r_h)])
                            scores.append(float(result[pt[1], pt[0]]))
                    except cv2.error:
                        continue
            
            # Apply NMS to remove hundreds of overlapping duplicate boxes
            if boxes:
                indices = cv2.dnn.NMSBoxes(boxes, scores, score_threshold=0.60, nms_threshold=0.3)
                if len(indices) > 0:
                    for i in indices.flatten():
                        x, y, w, h = boxes[i]
                        # Draw filled rectangle on the MASK, not the image
                        cv2.rectangle(mask, (x, y), (x + w, y + h), 255, -1)
                        logos_removed += 1
            
            # Use Inpainting to seamlessly fill the masked area based on surrounding pixels
            if logos_removed > 0:
                bgr = cv2.inpaint(bgr, mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)
                # Re-calculate grayscale since we altered the BGR image
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        # D. Weapon 2: The High Jump (Faint Background Watermarks)
        # Using a fixed luminance threshold for proven consistency with faint gray watermarks
        bgr[gray > 235] = 255

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
        "templates_loaded": len(LOGO_TEMPLATES_GRAY)
    }