curl -X POST "http://localhost:8000/download" \
 -H 'Content-Type: application/json' \
 -d '{"url":"https://www.instagram.com/p/DMNeLBSukhg/"}'# ytipfs-worker

Dockerized FastAPI service that downloads a video via yt-dlp and uploads it to Pinata using a JWT, returning the CID.

## Requirements

- Docker / Docker Compose
- Pinata account + **JWT** (Settings → API Keys → Create JWT)
- Optional: Tailscale on the host for private access

## Setup

1. `cp .env.example .env` and fill `PINATA_JWT`
2. `docker compose up -d --build`
3. Hit `http://<host-or-tailnet-ip>:8000/health`

## Usage

### POST (recommended)

```bash
curl -X POST "http://<ip>:8000/download" \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

### GET with base64url slug

```bash
# Make slug
python3 - <<'PY'
import base64
u = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
print(base64.urlsafe_b64encode(u.encode()).decode().rstrip('='))
PY

# Call
curl "http://<ip>:8000/d/<slug>"
```

### Response example

```json
{
  "status": "ok",
  "cid": "bafy...",
  "ipfs_uri": "ipfs://bafy...",
  "pinata_gateway": "https://ipfs.skatehive.app/ipfs/bafy...",
  "filename": "Rick_Astley-...mp4",
  "bytes": 14839234,
  "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
}
```

## Environment

- `PINATA_JWT` _(required)_
- `DOWNLOAD_DIR` default `/data`
- `YTDL_FORMAT` default `bv*+ba/bestvideo+bestaudio/best`
- `OUTPUT_TEMPLATE` default `%(title).80s-%(id)s.%(ext)s`
- `MAX_FILE_MB` default `1500`
- `KEEP_FILES` default `0` (delete file after pin)

## Tailscale (host)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
tailscale ip -4    # use 100.x.x.x here
```

## Notes

- ffmpeg is installed in the container; yt-dlp merges to mp4 when possible.
- Large files are blocked by `MAX_FILE_MB`.
- If a site needs cookies/login, extend yt-dlp options in code.
- Set `KEEP_FILES=1` to retain downloads for debugging.
