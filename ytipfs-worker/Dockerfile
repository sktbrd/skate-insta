# Multi-arch friendly (arm64/armv7/amd64)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps: ffmpeg for muxing/merging, ca-certs for TLS
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# Writable data dir for downloads
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Run the FastAPI app
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
