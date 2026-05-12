FROM python:3.9-slim

WORKDIR /app

# Install ffmpeg (required by yt-dlp)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY server.py ./
COPY index.html ./

EXPOSE 5050

CMD ["python", "server.py"]
