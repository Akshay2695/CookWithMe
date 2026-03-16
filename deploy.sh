#!/usr/bin/env bash
# deploy.sh — Build and deploy CookWithMe to Google Cloud Run
#
# Prerequisites:
#   brew install google-cloud-sdk          # or apt-get install google-cloud-cli
#   gcloud auth login
#   gcloud auth configure-docker
#
# Usage:
#   export GOOGLE_API_KEY="AIza..."
#   bash deploy.sh
#
# Optional overrides:
#   PROJECT_ID=my-project bash deploy.sh
#   REGION=asia-south1    bash deploy.sh   # Mumbai — lowest latency from India

set -euo pipefail

# ── Config (edit these or set as env vars before running) ────────────────────
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-asia-south1}"           # default: Mumbai (low latency from India)
SERVICE_NAME="${SERVICE_NAME:-cook-with-me}"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: PROJECT_ID is not set. Run: gcloud config set project YOUR_PROJECT_ID"
  exit 1
fi

if [[ -z "${GOOGLE_API_KEY:-}" ]]; then
  echo "ERROR: GOOGLE_API_KEY is not set. Export it before running this script."
  exit 1
fi

echo "========================================="
echo "  CookWithMe → Cloud Run Deployment"
echo "  Project : $PROJECT_ID"
echo "  Region  : $REGION"
echo "  Image   : $IMAGE"
echo "========================================="

# ── Step 1: Enable required APIs ─────────────────────────────────────────────
echo "[1/5] Enabling Cloud APIs..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  --project "$PROJECT_ID" --quiet

# ── Step 2: Build image via Cloud Build (no local Docker needed) ──────────────
echo "[2/5] Building container image with Cloud Build..."
gcloud builds submit . \
  --tag "$IMAGE" \
  --project "$PROJECT_ID" \
  --timeout 20m \
  --machine-type e2-highcpu-8

# ── Step 3: Deploy to Cloud Run ───────────────────────────────────────────────
echo "[3/5] Deploying to Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --platform managed \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 2Gi \
  --cpu 2 \
  --timeout 3600 \
  --concurrency 1 \
  --min-instances 1 \
  --max-instances 3 \
  --set-env-vars "GOOGLE_API_KEY=${GOOGLE_API_KEY}" \
  --set-env-vars "BROWSER_HEADLESS=true" \
  --set-env-vars "BROWSER_SLOW_MO=0" \
  --set-env-vars "GEMINI_MODEL=${GEMINI_MODEL:-gemini-2.0-flash-001}" \
  --quiet

# ── Step 4: Print the service URL ────────────────────────────────────────────
echo "[4/5] Fetching service URL..."
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --platform managed \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --format 'value(status.url)')

echo ""
echo "========================================="
echo "  SUCCESS"
echo "  URL: $SERVICE_URL"
echo "========================================="
echo ""
echo "Next steps:"
echo "  1. Open $SERVICE_URL in your browser"
echo "  2. Click the platform logo (Blinkit / Zepto)"
echo "     to connect your account via the login flow"
echo "  3. Type what you want to buy — the agent handles the rest"
echo ""
echo "Notes:"
echo "  - Sessions are stored in the container's ephemeral filesystem."
echo "    They survive container restarts (--min-instances=1 keeps it warm)"
echo "    but will be lost if the container is replaced (e.g. after redeployment)."
echo "  - To persist sessions across deployments, mount a Cloud Filestore NFS"
echo "    volume or use --set-env-vars SESSION_DIR=/persistent-path with a"
echo "    Cloud Storage FUSE mount."
