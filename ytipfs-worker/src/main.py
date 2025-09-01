import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, AnyUrl
from yt_dlp import YoutubeDL
import subprocess
import mimetypes

app = FastAPI(title="ytipfs-worker", version="1.0.0")

PINATA_JWT = os.getenv("PINATA_JWT", "").strip()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/data"))
YTDL_FORMAT = os.getenv(
    "YTDL_FORMAT",
    "bv*+ba/bestvideo+bestaudio/best",
)
OUTPUT_TEMPLATE = os.getenv("OUTPUT_TEMPLATE", "%(title).80s-%(id)s.%(ext)s")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "1500"))
KEEP_FILES = os.getenv("KEEP_FILES", "0") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class DownloadRequest(BaseModel):
    url: AnyUrl  # schema-validates URL

class MediaType:
    VIDEO = "video"
    IMAGE = "image"


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

    ydl_opts = {
        "format": "best",  # Changed to handle both images and videos
        "outtmpl": str(DOWNLOAD_DIR / OUTPUT_TEMPLATE),
        "noplaylist": True,
        "concurrent_fragment_downloads": 4,
        "retries": 5,
        "nopart": True,
        "restrictfilenames": True,
        "ignoreerrors": False,
        "quiet": True,
        "no_warnings": True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise HTTPException(status_code=400, detail="yt-dlp failed to extract info")

        out = Path(ydl.prepare_filename(info))
        
        # Don't assume MP4 format - check what was actually downloaded
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


@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


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
