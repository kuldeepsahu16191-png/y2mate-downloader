# Use a lightweight Python base image
FROM python:3.11-slim

# Install system dependencies (ffmpeg and ffprobe are required by yt-dlp)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy dependency file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the Python server script (ignoring Windows binaries like ffmpeg.exe)
COPY server.py cookies.txt* ./

# Expose port (Render/Railway will map this automatically)
EXPOSE 10000

# Launch the server with Gunicorn, dynamically binding to the assigned PORT env variable
CMD gunicorn --bind 0.0.0.0:$PORT --workers 2 server:app
