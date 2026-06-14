FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

ENV PYTHONUNBUFFERED=1
ENV PORT=5000

# Install xvfb for headless execution
RUN apt-get update && apt-get install -y xvfb && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create volume mount point
RUN mkdir -p /data
ENV DATA_DIR=/data

COPY . .

# Run via start.sh
RUN chmod +x start.sh
CMD ["./start.sh"]
