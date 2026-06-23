# Watermark Sanitizer Project

This repository contains two parts to clean faint watermarks from images and upload them to Supabase Storage:
1. **Backlog Sweeper** (`sweeper.py`): Batch script to process existing rows.
2. **Live Interceptor** (`main.py`): FastAPI microservice to sanitize new image uploads on the fly.

## Deployment Instructions

Since you are looking for student-tier hosting, here is how you can deploy the FastAPI application seamlessly using either **Render** or **DigitalOcean App Platform**.

### Prerequisites (For both platforms)
Ensure your project is pushed to a GitHub repository containing `main.py` and `requirements.txt`.
Make sure `main.py` exports your FastAPI app instance as `app`.

### Option A: Render (Easiest & Free Tier Available)
1. Log in to **Render** (`render.com`) using your GitHub account.
2. Click **New +** and select **Web Service**.
3. Connect the GitHub repository containing your FastAPI code.
4. **Configuration**:
    - **Name**: `watermark-sanitizer`
    - **Environment**: `Python 3`
    - **Build Command**: `pip install -r requirements.txt`
    - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. **Environment Variables**:
    - Add `SUPABASE_URL`: (Your Supabase Project URL)
    - Add `SUPABASE_SERVICE_ROLE_KEY`: (Your Supabase Service Role Key)
6. Click **Create Web Service**. Render will automatically build and deploy your app. You'll get a URL like `https://watermark-sanitizer.onrender.com`.

### Option B: DigitalOcean App Platform (Great for GitHub Student Pack)
1. Log in to **DigitalOcean** (`digitalocean.com`). The Student Developer Pack gives you credits here.
2. Go to **Apps** -> **Create App**.
3. Choose **GitHub** as your source and select your repository.
4. **Configuration**:
    - DigitalOcean will automatically detect Python.
    - Set the **Run Command** to: `uvicorn main:app --host 0.0.0.0 --port $PORT`
    - Add a **HTTP Port**: `8080` (DigitalOcean maps this to 80/443).
5. **Environment Variables**:
    - Add `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Make sure to check the box to "Encrypt" the service role key so it stays hidden.
6. **Plan**: Select the Basic tier (often covered by student credits).
7. Click **Create Resources**. Once the build finishes, DigitalOcean will provide a live URL.
