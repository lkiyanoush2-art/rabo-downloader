FROM python:3.10-slim

# Install system dependencies: ffmpeg is required for merging 1080p video & audio
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create downloads directory
RUN mkdir -p /app/downloads && chmod -R 777 /app/downloads

# Expose Render standard port
EXPOSE 10000

# Run the unified Bot & FastAPI server
CMD ["python", "main.py"]
