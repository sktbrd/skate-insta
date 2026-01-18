import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import uuid

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, AnyUrl
from yt_dlp import YoutubeDL
import subprocess
import mimetypes

app = FastAPI(title="ytipfs-worker", version="2.0.0")

# FOR TESTING: Use this URL to test Instagram downloads
# TEST_URL = "https://www.instagram.com/p/DOCCkdVj0Iy/"
# Update this URL as needed for testing purposes

PINATA_JWT = os.getenv("PINATA_JWT", "").strip()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/data"))
YTDL_FORMAT = os.getenv(
    "YTDL_FORMAT",
    "bv*[vcodec^=avc]+ba/bv*[vcodec^=h264]+ba/bv*+ba/bestvideo+bestaudio/best",
)
OUTPUT_TEMPLATE = os.getenv("OUTPUT_TEMPLATE", "%(title).80s-%(id)s.%(ext)s")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "1500"))
KEEP_FILES = os.getenv("KEEP_FILES", "0") == "1"

# Cookie authentication settings
INSTAGRAM_COOKIES_ENABLED = os.getenv("INSTAGRAM_COOKIES_ENABLED", "true").lower() == "true"
INSTAGRAM_COOKIES_PATH = Path(os.getenv("INSTAGRAM_COOKIES_PATH", "/data/instagram_cookies.txt"))
COOKIE_VALIDATION_INTERVAL = int(os.getenv("COOKIE_VALIDATION_INTERVAL", "3600"))  # 1 hour
COOKIE_AUTO_REFRESH = os.getenv("COOKIE_AUTO_REFRESH", "true").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Download logging setup
def setup_download_logging():
    """Set up logging for Instagram downloads"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # Configure file handler for download logs
    download_log_path = log_dir / "instagram_download.log"
    
    # Create a separate logger for download tracking
    download_logger = logging.getLogger('instagram_downloads')
    download_logger.setLevel(logging.INFO)
    
    # Clear existing handlers
    download_logger.handlers.clear()
    
    # Create file handler with JSON formatting
    file_handler = logging.FileHandler(download_log_path)
    file_handler.setLevel(logging.INFO)
    
    # Don't add formatter - we'll write JSON directly
    download_logger.addHandler(file_handler)
    download_logger.propagate = False
    
    return download_logger

# Initialize download logger
download_logger = setup_download_logging()

def log_download_event(event_data: dict):
    """Log download events in JSON format"""
    event_data["timestamp"] = datetime.utcnow().isoformat() + "Z"
    download_logger.info(json.dumps(event_data))


class DownloadRequest(BaseModel):
    url: AnyUrl  # schema-validates URL

class MediaType:
    VIDEO = "video"
    IMAGE = "image"

class CookieManager:
    """Manages Instagram cookie authentication for yt-dlp"""
    
    def __init__(self):
        self.cookies_path = INSTAGRAM_COOKIES_PATH
        self.last_validation = None
        self.cookies_valid = False
        self.cookies_enabled = INSTAGRAM_COOKIES_ENABLED
        
    def _create_sample_cookies_file(self):
        """Create a sample cookies file with instructions"""
        sample_content = """# Instagram Cookie File for yt-dlp
# This is a Netscape HTTP Cookie File format
# To get your cookies:
# 1. Install "Get cookies.txt LOCALLY" browser extension
# 2. Go to instagram.com and login
# 3. Click the extension and download cookies.txt
# 4. Replace this file with your downloaded cookies.txt
#
# Example format:
# .instagram.com	TRUE	/	FALSE	1234567890	sessionid	your_session_id_here
# .instagram.com	TRUE	/	FALSE	1234567890	csrftoken	your_csrf_token_here
# .instagram.com	TRUE	/	FALSE	1234567890	ds_user_id	your_user_id_here

# Netscape HTTP Cookie File
"""
        self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
        self.cookies_path.write_text(sample_content)
        logging.info(f"Created sample cookies file at {self.cookies_path}")
    
    def cookies_exist(self) -> bool:
        """Check if cookies file exists and has content"""
        if not self.cookies_path.exists():
            return False
        
        content = self.cookies_path.read_text().strip()
        # Check if it's not just the sample file
        return bool(content) and "your_session_id_here" not in content
    
    def validate_cookies(self) -> bool:
        """Validate cookies by testing with Instagram"""
        if not self.cookies_enabled or not self.cookies_exist():
            self.cookies_valid = False
            return False
        
        try:
            # Test cookies with a simple Instagram request
            test_opts = {
                "cookiefile": str(self.cookies_path),
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
            }
            
            with YoutubeDL(test_opts) as ydl:
                # Try to extract info from Instagram main page (doesn't download)
                info = ydl.extract_info("https://www.instagram.com", download=False)
                if info:
                    self.cookies_valid = True
                    self.last_validation = datetime.now()
                    logging.info("✅ Instagram cookies validated successfully")
                    return True
                    
        except Exception as e:
            logging.warning(f"Cookie validation failed: {str(e)}")
            
        self.cookies_valid = False
        return False
    
    def should_validate(self) -> bool:
        """Check if cookies should be re-validated"""
        if not self.last_validation:
            return True
        
        time_since_validation = datetime.now() - self.last_validation
        return time_since_validation.total_seconds() > COOKIE_VALIDATION_INTERVAL
    
    def get_download_options(self) -> Dict[str, Any]:
        """Get yt-dlp options with or without cookies"""
        base_opts = {
            "format": YTDL_FORMAT,
            "outtmpl": str(DOWNLOAD_DIR / OUTPUT_TEMPLATE),
            "noplaylist": True,
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 4,
            "retries": 5,
            "nopart": True,
            "restrictfilenames": True,
            "ignoreerrors": False,
            "quiet": True,
            "no_warnings": True,
        }
        
        # Add cookies if available and enabled
        if self.cookies_enabled and self.cookies_exist():
            if self.should_validate():
                self.validate_cookies()
                
            if self.cookies_valid:
                base_opts["cookiefile"] = str(self.cookies_path)
                logging.info("🍪 Using Instagram cookies for authentication")
            else:
                logging.warning("⚠️ Cookies available but invalid, proceeding without authentication")
        elif self.cookies_enabled and not self.cookies_exist():
            self._create_sample_cookies_file()
            logging.warning("⚠️ No Instagram cookies found, created sample file. Please add your cookies for better reliability.")
        
        return base_opts
    
    def get_status(self) -> Dict[str, Any]:
        """Get cookie manager status for health endpoint"""
        return {
            "cookies_enabled": self.cookies_enabled,
            "cookies_exist": self.cookies_exist(),
            "cookies_valid": self.cookies_valid,
            "last_validation": self.last_validation.isoformat() if self.last_validation else None,
            "cookies_path": str(self.cookies_path),
        }


# Initialize global cookie manager
cookie_manager = CookieManager()


def wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept.lower()


def render_health_html(title: str, payload: Dict[str, Any]) -> HTMLResponse:
    lines = [
        "<!doctype html>",
        "<html>",
        f"  <head><meta charset="utf-8"><title>{title}</title></head>",
        "  <body>",
        f"    <h1>{title}</h1>",
    ]

    for key, value in payload.items():
        lines.append(f"    <p>{key}: {value}</p>")

    lines.extend(["  </body>", "</html>"])
    return HTMLResponse("
".join(lines))


def _b64url_decode(s: str) -> str:
    rem = len(s) % 4
    if rem:
        s += "=" * (4 - rem)
    return base64.urlsafe_b64decode(s.encode()).decode()


def _pin_to_pinata(file_path: Path, name: Optional[str] = None) -> dict:
    if not PINATA_JWT:
        raise HTTPException(status_code=500, detail="PINATA_JWT not configured")

    url = "https://api.pinata.cloud/pinning/pinFileToIPFS"
    headers = {"Authorization": f"Bearer {PINATA_JWT}"}

    metadata = {
        "name": name or file_path.name,
        "keyvalues": {"source": "ytipfs-worker"},
    }
    options = {"cidVersion": 1}

    with file_path.open("rb") as f:
        files = {"file": (file_path.name, f, "application/octet-stream")}
        data = {
            "pinataMetadata": json.dumps(metadata),
            "pinataOptions": json.dumps(options),
        }
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=600)

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Pinata error: {resp.text}")

    return resp.json()

def _needs_h264_conversion(file_path: Path) -> bool:
    """Check if video needs conversion to H.264 for mobile compatibility"""
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json", 
            "-show_streams", "-select_streams", "v:0", str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        
        for stream in info.get("streams", []):
            codec_name = stream.get("codec_name", "").lower()
            # Check for mobile-incompatible codecs
            if codec_name in ["vp9", "vp8", "av1", "hevc"]:
                logging.info(f"Found {codec_name} codec, conversion needed for mobile compatibility")
                return True
            elif codec_name in ["h264", "avc"]:
                logging.info(f"Found {codec_name} codec, mobile compatible")
                return False
        
        # If we can't determine, assume conversion needed
        return True
    except Exception as e:
        logging.warning(f"Could not probe video codec: {e}, assuming conversion needed")
        return True


def _convert_media(file_path: Path, media_type: str) -> Path:
    """
    Convert video to H.264/AAC MP4 for mobile compatibility, or image to jpg if needed.
    """
    suffix = file_path.suffix.lower()
    
    if media_type == MediaType.VIDEO:
        # Always check codec, even if it's already MP4
        needs_conversion = (suffix != ".mp4") or _needs_h264_conversion(file_path)
        
        if needs_conversion:
            out_path = file_path.with_suffix(".mp4") if suffix != ".mp4" else file_path.parent / f"{file_path.stem}_h264.mp4"
            cmd = [
                "ffmpeg", "-y", "-i", str(file_path),
                "-c:v", "libx264", "-crf", "23", "-preset", "medium",  # Better quality settings
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",  # Optimize for web streaming
                "-pix_fmt", "yuv420p",  # Ensure mobile compatibility
                str(out_path)
            ]
            logging.info(f"Converting video to mobile-compatible H.264: {' '.join(cmd)}")
            try:
                result = subprocess.run(cmd, check=True, capture_output=True, text=True)
                # Remove original if conversion successful and it's a different file
                if out_path != file_path:
                    file_path.unlink(missing_ok=True)
                file_path = out_path
            except subprocess.CalledProcessError as e:
                logging.error(f"ffmpeg conversion failed: {e.stderr}")
                raise HTTPException(status_code=500, detail=f"Video conversion failed: {e.stderr}")
        else:
            logging.info(f"Video already in mobile-compatible format: {file_path}")
            
    elif media_type == MediaType.IMAGE and suffix not in [".jpg", ".jpeg"]:
        out_path = file_path.with_suffix(".jpg")
        cmd = [
            "ffmpeg", "-y", "-i", str(file_path), "-q:v", "2", str(out_path)
        ]
        logging.info(f"Converting image to jpg: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            file_path.unlink(missing_ok=True)  # Remove original
            file_path = out_path
        except subprocess.CalledProcessError as e:
            logging.error(f"ffmpeg image conversion failed: {e.stderr}")
            raise HTTPException(status_code=500, detail=f"Image conversion failed: {e.stderr}")
    
    logging.info(f"Final file path: {file_path}")
    return file_path


def _download_video(url: str) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Get download options with cookie support
    ydl_opts = cookie_manager.get_download_options()
    
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise HTTPException(status_code=400, detail="yt-dlp failed to extract info")

            out = Path(ydl.prepare_filename(info))
            if out.suffix.lower() != ".mp4" and (out.with_suffix(".mp4").exists()):
                out = out.with_suffix(".mp4")

            if not out.exists():
                # try to locate by id if prepare_filename changed
                vid = info.get("id", "")
                candidates = sorted(
                    DOWNLOAD_DIR.glob(f"*{vid}*"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if candidates:
                    out = candidates[0]
                else:
                    raise HTTPException(status_code=500, detail="Downloaded file not found")

            size_mb = out.stat().st_size / (1024 * 1024)
            if size_mb > MAX_FILE_MB:
                try:
                    out.unlink(missing_ok=True)
                except Exception:
                    pass
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large ({size_mb:.1f} MB > {MAX_FILE_MB} MB)",
                )

            # Detect media type (Instagram: video or image)
            mime, _ = mimetypes.guess_type(str(out))
            if mime and mime.startswith("image"):
                media_type = MediaType.IMAGE
            else:
                media_type = MediaType.VIDEO
            out = _convert_media(out, media_type)
            return out
            
    except Exception as e:
        error_msg = str(e).lower()
        
        # Check for Instagram-specific errors
        if any(keyword in error_msg for keyword in ["login", "rate limit", "429", "authentication", "private"]):
            if cookie_manager.cookies_enabled and not cookie_manager.cookies_valid:
                raise HTTPException(
                    status_code=503, 
                    detail="Instagram rate limit reached or authentication required. Please update your Instagram cookies for better reliability."
                )
            else:
                raise HTTPException(
                    status_code=503,
                    detail="Instagram rate limit reached or login required. Please try again later."
                )
        elif "unavailable" in error_msg or "not found" in error_msg:
            raise HTTPException(
                status_code=404,
                detail="The requested Instagram content was not found or is unavailable."
            )
        elif "private" in error_msg:
            raise HTTPException(
                status_code=403,
                detail="This Instagram content is private and cannot be downloaded."
            )
        else:
            # Generic error
            logging.error(f"Download failed for {url}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Download failed: {str(e)}"
            )


@app.get("/logs")
def get_download_logs(limit: int = 50):
    """Get recent download logs"""
    try:
        log_path = Path("logs/instagram_download.log")
        if not log_path.exists():
            return {"logs": [], "total": 0, "success_count": 0, "failure_count": 0}
        
        with open(log_path, 'r') as f:
            lines = f.readlines()
        
        # Get last 'limit' lines and parse JSON
        recent_lines = lines[-limit:] if len(lines) > limit else lines
        logs = []
        
        for line in recent_lines:
            try:
                log_entry = json.loads(line.strip())
                logs.append(log_entry)
            except json.JSONDecodeError:
                continue
        
        # Sort by timestamp, newest first
        logs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        success_count = len([l for l in logs if l.get('success', False)])
        failure_count = len([l for l in logs if l.get('success') == False])
        
        return {
            "logs": logs,
            "total": len(logs),
            "success_count": success_count,
            "failure_count": failure_count
        }
        
    except Exception as e:
        return {"error": str(e), "logs": [], "total": 0, "success_count": 0, "failure_count": 0}


@app.get("/health")
def health(request: Request):
    """Enhanced health endpoint with cookie status"""
    try:
        cookie_status = cookie_manager.get_status()
    except Exception as exc:
        cookie_status = {"error": str(exc)}

    payload = {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "authentication": cookie_status,
        "version": "2.0.0"
    }

    if wants_html(request):
        return render_health_html("Instagram Downloader Health", payload)

    return payload

@app.get("/healthz")
def healthz(request: Request):
    """Simple health check for backward compatibility"""
    payload = {"status": "ok", "timestamp": datetime.now().isoformat()}

    if wants_html(request):
        return render_health_html("Instagram Downloader Healthz", payload)

    return payload

@app.get("/instagram/health")
def instagram_health(request: Request):
    return health(request)

@app.get("/instagram/healthz")
def instagram_healthz(request: Request):
    return healthz(request)

@app.get("/cookies/status")
def cookies_status():
    """Detailed cookie status endpoint"""
    return cookie_manager.get_status()

@app.post("/cookies/validate")
def validate_cookies():
    """Force cookie validation"""
    if not cookie_manager.cookies_enabled:
        raise HTTPException(status_code=400, detail="Cookie authentication is disabled")
    
    if not cookie_manager.cookies_exist():
        raise HTTPException(status_code=400, detail="No cookies file found")
    
    is_valid = cookie_manager.validate_cookies()
    return {
        "valid": is_valid,
        "timestamp": datetime.now().isoformat(),
        "message": "Cookies are valid" if is_valid else "Cookies are invalid or expired"
    }


@app.post("/download")
def download_post(req: DownloadRequest, request: Request):
    download_id = f"insta_{int(datetime.utcnow().timestamp() * 1000)}"
    start_time = datetime.utcnow().timestamp() * 1000
    client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
    user_agent = request.headers.get("user-agent", "unknown")
    
    # Log download start
    log_download_event({
        "id": download_id,
        "status": "started", 
        "url": str(req.url),
        "clientIP": client_ip,
        "userAgent": user_agent,
        "startTime": start_time
    })
    
    logging.info(f"[{download_id}] Downloading: {req.url}")
    
    try:
        file_path = _download_video(str(req.url))
        logging.info(f"[{download_id}] Final file for IPFS upload: {file_path}")
        
        pin = _pin_to_pinata(file_path, name=file_path.name)
        cid = pin.get("IpfsHash")
        file_size = file_path.stat().st_size
        duration = int(datetime.utcnow().timestamp() * 1000 - start_time)
        
        # Log successful completion
        log_download_event({
            "id": download_id,
            "status": "completed",
            "url": str(req.url),
            "filename": file_path.name,
            "cid": cid,
            "gatewayUrl": f"https://ipfs.skatehive.app/ipfs/{cid}",
            "bytes": file_size,
            "duration": duration,
            "clientIP": client_ip,
            "success": True
        })
        
        res = {
            "status": "ok",
            "cid": cid,
            "ipfs_uri": f"ipfs://{cid}",
            "pinata_gateway": f"https://ipfs.skatehive.app/ipfs/{cid}",
            "filename": file_path.name,
            "bytes": file_size,
            "source_url": str(req.url),
        }
        return res
        
    except Exception as e:
        duration = int(datetime.utcnow().timestamp() * 1000 - start_time)
        
        # Log failure
        log_download_event({
            "id": download_id,
            "status": "failed",
            "url": str(req.url),
            "error": str(e),
            "duration": duration,
            "clientIP": client_ip,
            "success": False
        })
        
        logging.error(f"[{download_id}] Download failed: {e}")
        raise e
        
    finally:
        if not KEEP_FILES:
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                logging.warning(f"[{download_id}] Failed to remove temp file", exc_info=True)


@app.get("/d/{b64url}")
def download_get(b64url: str, request: Request):
    try:
        url = _b64url_decode(b64url)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed base64url slug")
    return download_post(DownloadRequest(url=url), request)
