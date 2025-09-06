import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, AnyUrl
from yt_dlp import YoutubeDL
import subprocess
import mimetypes

app = FastAPI(title="ytipfs-worker", version="2.0.0")

PINATA_JWT = os.getenv("PINATA_JWT", "").strip()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/data"))
YTDL_FORMAT = os.getenv(
    "YTDL_FORMAT",
    "bv*+ba/bestvideo+bestaudio/best",
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

def _convert_media(file_path: Path, media_type: str) -> Path:
    """
    Convert video to mp4 or image to jpg if needed. Returns new file path if converted, else original.
    """
    suffix = file_path.suffix.lower()
    if media_type == MediaType.VIDEO and suffix != ".mp4":
        out_path = file_path.with_suffix(".mp4")
        cmd = [
            "ffmpeg", "-y", "-i", str(file_path), "-c:v", "libx264", "-c:a", "aac", str(out_path)
        ]
        logging.info(f"Converting video to mp4: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            file_path = out_path
        except Exception as e:
            logging.error(f"ffmpeg conversion failed: {e}")
            raise HTTPException(status_code=500, detail="ffmpeg conversion failed")
    elif media_type == MediaType.IMAGE and suffix not in [".jpg", ".jpeg"]:
        out_path = file_path.with_suffix(".jpg")
        cmd = [
            "ffmpeg", "-y", "-i", str(file_path), str(out_path)
        ]
        logging.info(f"Converting image to jpg: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            file_path = out_path
        except Exception as e:
            logging.error(f"ffmpeg image conversion failed: {e}")
            raise HTTPException(status_code=500, detail="ffmpeg image conversion failed")
    logging.info(f"Converted file path: {file_path}")
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


@app.get("/health")
def health():
    """Enhanced health endpoint with cookie status"""
    cookie_status = cookie_manager.get_status()
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "authentication": cookie_status,
        "version": "2.0.0"
    }

@app.get("/healthz")
def healthz():
    """Simple health check for backward compatibility"""
    return {"status": "ok"}

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
def download_post(req: DownloadRequest):
    logging.info(f"Downloading: {req.url}")
    file_path = _download_video(str(req.url))
    logging.info(f"Final file for IPFS upload: {file_path}")
    try:
        pin = _pin_to_pinata(file_path, name=file_path.name)
        cid = pin.get("IpfsHash")
        res = {
            "status": "ok",
            "cid": cid,
            "ipfs_uri": f"ipfs://{cid}",
            "pinata_gateway": f"https://ipfs.skatehive.app/ipfs/{cid}",
            "filename": file_path.name,
            "bytes": file_path.stat().st_size,
            "source_url": str(req.url),
        }
        return res
    finally:
        if not KEEP_FILES:
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                logging.warning("Failed to remove temp file", exc_info=True)


@app.get("/d/{b64url}")
def download_get(b64url: str):
    try:
        url = _b64url_decode(b64url)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed base64url slug")
    return download_post(DownloadRequest(url=url))
