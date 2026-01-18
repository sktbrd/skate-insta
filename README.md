# Instagram/Social Media Downloader Service

Dockerized FastAPI service that downloads videos from Instagram, YouTube, TikTok, and 1000+ sites via yt-dlp, then uploads to IPFS via Pinata.

## 🎯 Features

- **Multi-Platform Support:** Instagram, YouTube, TikTok, and more via yt-dlp
- **Cookie Authentication:** Instagram authentication to bypass rate limits
- **IPFS Integration:** Automatic Pinata upload with CID generation
- **RESTful + Slug API:** JSON POST and base64 URL slug support
- **Health Monitoring:** Cookie validation and expiration tracking
- **File Management:** Configurable retention and size limits (max 1.5GB)

## Requirements

- Docker / Docker Compose
- Pinata account + **JWT** (Settings → API Keys → Create JWT)
- Instagram cookies (for Instagram downloads) - See Cookie Management section
- Optional: Tailscale on the host for private access

## Setup

1. `cp .env.example .env` and fill `PINATA_JWT`
2. Add Instagram cookies to `data/instagram_cookies.txt` (see Cookie Management)
3. `docker compose up -d --build`
4. Hit `http://<host-or-tailnet-ip>:6666/healthz` (external port)

**Note on Ports:**
- **Internal Port:** `8000` (FastAPI app inside container)
- **External Port:** `6666` (exposed on host machine)
- Access via `http://localhost:6666` on the host

## API Reference

### POST /download (recommended)

Download content from URL and upload to IPFS.

```bash
curl -X POST "http://<ip>:6666/download" \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.instagram.com/p/ABC123/"}'
```

### GET /d/<base64_slug>

Alternative slug-based endpoint.

```bash
# Make slug
python3 - <<'PY'
import base64
u = "https://www.instagram.com/p/ABC123/"
print(base64.urlsafe_b64encode(u.encode()).decode().rstrip('='))
PY

# Call
curl "http://<ip>:6666/d/<slug>"
```

### GET /healthz

Service health check with cookie status.

```bash
curl "http://<ip>:6666/healthz"
```

**Response:**
```json
{
  "status": "ok",
  "timestamp": "2025-12-05T10:35:00Z",
  "authentication": {
    "cookies_enabled": true,
    "cookies_exist": true,
    "cookies_valid": true,
    "last_validation": "2025-12-05T10:00:00Z",
    "cookies_path": "/data/instagram_cookies.txt"
  },
  "version": "2.0.0"
}
```

### POST /cookies/validate

Validate Instagram cookie authentication.

```bash
curl -X POST "http://<ip>:6666/cookies/validate"
```

### GET /cookies/status

Check cookie expiration status.

```bash
curl "http://<ip>:6666/cookies/status"
```

### Response example (successful download)

```json
{
  "status": "ok",
  "cid": "bafy...",
  "ipfs_uri": "ipfs://bafy...",
  "pinata_gateway": "https://ipfs.skatehive.app/ipfs/bafy...",
  "filename": "instagram_video_ABC123.mp4",
  "bytes": 14839234,
  "source_url": "https://www.instagram.com/p/ABC123/"
}
```

## Instagram Cookie Management

### Why Cookies Are Needed

Instagram requires authentication to download content. The service uses browser cookies to authenticate as a logged-in user.

### Cookie File Format

Cookies must be in **Netscape format** and stored in `data/instagram_cookies.txt`:

```
# Netscape HTTP Cookie File
.instagram.com	TRUE	/	TRUE	1234567890	csrftoken	ABC123...
.instagram.com	TRUE	/	TRUE	1234567890	sessionid	XYZ789...
```

### Obtaining Cookies

**Method 1: Browser Extension (Recommended)**

1. Install "Get cookies.txt LOCALLY" extension (Chrome/Firefox)
2. Log into Instagram in your browser
3. Navigate to instagram.com
4. Click extension icon → Export → Netscape format
5. Save as `data/instagram_cookies.txt`

**Method 2: Browser DevTools**

1. Log into Instagram
2. Open DevTools (F12) → Application/Storage → Cookies
3. Find `sessionid` and `csrftoken` cookies
4. Create Netscape format file manually

### Cookie Refresh Procedure

**When to Refresh:**
- Cookie expiration warning from `/healthz` endpoint
- "Rate limit" or "Login required" errors
- Service returns authentication errors
- Every 6-12 months (Instagram cookie lifetime)

**Refresh Steps:**

1. **Obtain fresh cookies** (see above methods)

2. **Update cookie file:**
   ```bash
   # On Mac Mini M4 or Raspberry Pi
   cd skatehive-monorepo/skatehive-instagram-downloader/ytipfs-worker
   nano data/instagram_cookies.txt  # paste new cookies
   ```

3. **Restart service:**
   ```bash
   docker compose restart
   ```

4. **Verify:**
   ```bash
   curl http://localhost:6666/healthz
   curl -X POST http://localhost:6666/cookies/validate
   ```

### Cookie Security

⚠️ **Important:** Instagram cookies are sensitive credentials
- Never commit `instagram_cookies.txt` to git (already in .gitignore)
- Use cookies from a dedicated Instagram account if possible
- Rotate cookies periodically
- Monitor for unusual activity on the Instagram account

## Production Deployment (Mac Mini M4)

**Current Live Configuration:**

- **External URL:** `https://minivlad.tail83ea3e.ts.net/instagram/download`
- **External Port:** `6666`
- **Internal Port:** `8000`
- **Container:** `ytipfs-worker`
- **Upload Limit:** `1500MB`
- **Cookie File:** `data/instagram_cookies.txt`
- **Network:** Tailscale Funnel (publicly accessible)

**Port Mapping:**
```yaml
# docker-compose.yml
ports:
  - "6666:8000"  # Host:Container
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

## Troubleshooting

### "Rate limit exceeded" or "Login required"

**Cause:** Instagram cookies are expired or invalid

**Solution:**
1. Check cookie status: `curl http://localhost:6666/healthz`
2. Refresh cookies (see Cookie Management section)
3. Restart service: `docker compose restart`
4. Verify: `curl -X POST http://localhost:6666/cookies/validate`

### "Server timeout" from skatehive3.0

**Cause:** Service not accessible or port misconfiguration

**Solution:**
1. Check service is running: `docker ps | grep ytipfs`
2. Test locally: `curl http://localhost:6666/healthz`
3. Check Tailscale Funnel: `tailscale funnel status`
4. Verify port mapping in docker-compose.yml (should be 6666:8000)

### "File too large" error

**Cause:** Video exceeds MAX_FILE_MB limit (default 1500MB)

**Solution:**
1. Increase limit in .env: `MAX_FILE_MB=2000`
2. Restart service: `docker compose restart`
3. Consider storage implications

### "Failed to download" from Instagram

**Cause:** Invalid URL, private post, or cookie issue

**Solution:**
1. Verify URL format: `https://www.instagram.com/p/<POST_ID>/`
2. Check if post is public
3. Validate cookies: `curl -X POST http://localhost:6666/cookies/validate`
4. Try different Instagram URL format

### Service won't start

**Cause:** Missing PINATA_JWT or cookie file

**Solution:**
1. Check .env file exists and has PINATA_JWT
2. Ensure data/instagram_cookies.txt exists
3. Check logs: `docker compose logs ytipfs-worker`
4. Verify file permissions on data/ directory

## Notes

- ffmpeg is installed in the container; yt-dlp merges to mp4 when possible.
- Large files are blocked by `MAX_FILE_MB`.
- Set `KEEP_FILES=1` to retain downloads for debugging.
- Cookie file location: `ytipfs-worker/data/instagram_cookies.txt`
- Backup cookie file location: `ytipfs-worker/data/instagram_cookies_real.txt`
