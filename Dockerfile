# ── Stage 1: build deps ───────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy AS base

WORKDIR /app

# Install Python deps first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers are pre-installed in the base image.
# We only need Chromium; skip Firefox and WebKit to save ~600 MB.
RUN playwright install chromium

# ── Stage 2: copy app code ────────────────────────────────────────────────────
# Only the files needed at runtime:
#   gemini/          — FastAPI server, agents, core loop, config, static UI
#   utils/           — user_profile.py (imported by gemini/core/chat_session.py)
COPY gemini/ gemini/
COPY utils/  utils/

# Create writable dirs for sessions/screenshots (ephemeral; fine for hackathon)
RUN mkdir -p sessions screenshots

# ── Runtime config ────────────────────────────────────────────────────────────
# Cloud Run injects PORT; default to 8080 if not set.
# BROWSER_HEADLESS=true required — no display server on Cloud Run.
ENV PORT=8080 \
    BROWSER_HEADLESS=true \
    BROWSER_SLOW_MO=0 \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# Start the FastAPI server on $PORT (Cloud Run sets PORT env var)
CMD uvicorn gemini.server:app --host 0.0.0.0 --port $PORT --workers 1
