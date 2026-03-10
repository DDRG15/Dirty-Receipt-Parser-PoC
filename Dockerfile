# Dockerfile -- Titanium-Vault V13.2
# Build : docker build -t titanium-vault:v13.2 .
# Run   : docker compose up
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/data/raw_samples /app/data/output
# /app/data is the named-volume mount point; inputs and outputs survive
# container restarts and are isolated from host OS file-locking behaviour.
VOLUME ["/app/data"]
CMD ["python", "main.py"]
