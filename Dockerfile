FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Create necessary directories
RUN mkdir -p uploads output overlays temp vod_jobs vod_downloads vod_analysis_cache

# Run bot
CMD ["python", "main.py"]