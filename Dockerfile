FROM python:3.11-slim

# Playwright deps for Chromium on Debian
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install --with-deps chromium

COPY . .

ENV HOST=0.0.0.0
EXPOSE 8000
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
