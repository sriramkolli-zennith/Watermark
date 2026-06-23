import os
import io
import requests
import numpy as np
import cv2
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# Ensure required environment variables are set
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in environment.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def remove_watermark_in_memory(image_bytes: bytes) -> bytes:
    """
    Decodes image bytes, removes faint watermarks, and returns png bytes.
    Everything is done in-memory without disk I/O.
    """
    # 1. Decode bytes to BGR matrix
    np_arr = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    
    if image is None:
        raise ValueError("Could not decode image bytes.")
        
    # 2. Create grayscale copy
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    
    # 3. Create boolean mask where pixel_value > 235
    _, mask = cv2.threshold(gray, 235, 255, cv2.THRESH_BINARY)
    
    # 4. Apply mask to original BGR matrix, forcing pixels to white [255, 255, 255]
    image[mask == 255] = [255, 255, 255]
    
    # 5. Re-encode BGR matrix to .png liquid bytes
    success, encoded_image = cv2.imencode('.png', image)
    if not success:
        raise ValueError("Could not encode image to PNG.")
        
    return encoded_image.tobytes()

def run_sweeper():
    print("Starting Phase 1 Backlog Sweeper...")
    
    # 1. Query table for rows where image_url contains storage.googleapis.com
    try:
        response = supabase.table("questions").select("*").ilike("image_url", "%storage.googleapis.com%").execute()
        rows = response.data
    except Exception as e:
        print(f"Failed to fetch rows: {e}")
        return

    print(f"Found {len(rows)} rows to process.")

    for row in rows:
        row_id = row.get("id")
        dirty_url = row.get("image_url")
        print(f"Processing ID: {row_id} | URL: {dirty_url}")
        
        try:
            # 2. Stream bytes via requests
            resp = requests.get(dirty_url, stream=True, timeout=10)
            resp.raise_for_status()
            dirty_bytes = resp.content
            
            # 3. Run in-memory watermark removal
            clean_bytes = remove_watermark_in_memory(dirty_bytes)
            
            # 4. Generate clean destination path
            destination_path = f"diagrams/{row_id}.png"
            
            # 5. Upload clean bytes to Supabase bucket
            bucket_name = "sanitized-diagrams"
            supabase.storage.from_(bucket_name).upload(
                file=clean_bytes,
                path=destination_path,
                file_options={"content-type": "image/png", "x-upsert": "true"}
            )
            
            # 6. Get public URL
            public_url = supabase.storage.from_(bucket_name).get_public_url(destination_path)
            
            # 7. Execute Supabase .update() on specific row
            supabase.table("questions").update({"image_url": public_url}).eq("id", row_id).execute()
            
            print(f"Successfully processed ID: {row_id}")
            
        except requests.exceptions.RequestException as e:
            print(f"Network error downloading image for ID {row_id}: {e}")
            # Continue to next row safely
        except Exception as e:
            print(f"Error processing ID {row_id}: {e}")
            # Continue to next row safely

if __name__ == "__main__":
    run_sweeper()
