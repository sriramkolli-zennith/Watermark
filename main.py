import os
import uuid
import requests
import numpy as np
import cv2
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# Ensure required environment variables are set
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in environment.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
bucket_name = "sanitized-diagrams"

app = FastAPI(title="Watermark Sanitizer API")

# Add CORS middleware for custom admin frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict this to specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SanitizeRequest(BaseModel):
    source_url: str

def remove_watermark_in_memory(image_bytes: bytes) -> bytes:
    """
    Decodes image bytes, removes faint watermarks, and returns png bytes.
    Everything is done in-memory without disk I/O.
    """
    np_arr = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    
    if image is None:
        raise ValueError("Could not decode image bytes.")
        
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 235, 255, cv2.THRESH_BINARY)
    image[mask == 255] = [255, 255, 255]
    
    success, encoded_image = cv2.imencode('.png', image)
    if not success:
        raise ValueError("Could not encode image to PNG.")
        
    return encoded_image.tobytes()

@app.post("/sanitize")
async def sanitize_image(req: SanitizeRequest):
    try:
        # 1. Fetch image bytes from source URL
        resp = requests.get(req.source_url, stream=True, timeout=10)
        resp.raise_for_status()
        dirty_bytes = resp.content
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch source image: {e}")

    try:
        # 2. Run in-memory watermark removal
        clean_bytes = remove_watermark_in_memory(dirty_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process image: {e}")

    try:
        # 3. Generate unique filename (using UUID)
        unique_filename = f"diagrams/{uuid.uuid4().hex}.png"
        
        # 4. Upload clean bytes to Supabase bucket
        supabase.storage.from_(bucket_name).upload(
            file=clean_bytes,
            path=unique_filename,
            file_options={"content-type": "image/png"}
        )
        
        # 5. Get public URL
        public_url = supabase.storage.from_(bucket_name).get_public_url(unique_filename)
        
        # 6. Return JSON response
        return {
            "status": "success",
            "clean_url": public_url
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload to Supabase: {e}")

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Watermark Sanitizer API is running"}
