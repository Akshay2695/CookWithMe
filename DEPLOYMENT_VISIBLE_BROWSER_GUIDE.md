# Visible Browser Deployment Flow (Local First)

This guide lets you test a visible Chromium window **inside a container** locally,
then deploy the same pattern on Google Compute Engine.

## Why this path

Cloud Run is headless-only. If you need to see and interact with the browser window,
run the browser worker on a VM (or locally) and expose it via noVNC.

## Local Test: Visible Chromium in Container

Build image:

```bash
docker build -f Dockerfile.novnc -t cook-with-me-novnc:local .
```

Run image:

```bash
docker run --rm -it \
  -p 8000:8080 \
  -p 6080:6080 \
  -e GOOGLE_API_KEY=YOUR_REAL_KEY \
  -e BROWSER_HEADLESS=false \
  --name cook-with-me-novnc \
  cook-with-me-novnc:local
```

Open in browser:

- App UI: `http://localhost:8000`
- Visible container desktop (noVNC): `http://localhost:6080/vnc.html`

Login flow:

1. Open app UI and click **Accounts**.
2. Open noVNC tab to watch/interact with Chromium window.
3. Complete platform login.
4. Continue shopping from app UI while monitoring noVNC.

## If Docker is unavailable locally

Use `docker` with the same commands:

```bash
docker build -f Dockerfile.novnc -t cook-with-me-novnc:local .
docker run --rm -it -p 8000:8080 -p 6080:6080 \
  -e GOOGLE_API_KEY=YOUR_REAL_KEY -e BROWSER_HEADLESS=false \
  cook-with-me-novnc:local
```

## Deploy Same Pattern on Google Compute Engine

1. Create Ubuntu VM and allow ports `8000` and `6080` in firewall.
2. Install Docker (or docker/containerd).
3. Build/pull `Dockerfile.novnc` image.
4. Run with `-p 8000:8080 -p 6080:6080`.
5. Access:
   - `http://VM_EXTERNAL_IP:8000` (app)
   - `http://VM_EXTERNAL_IP:6080/vnc.html` (visible browser)

## Security notes

- Do not expose noVNC (`6080`) publicly without auth.
- For demos, prefer temporary firewall allowlists.
- Keep `GOOGLE_API_KEY` in env or a secret manager.
