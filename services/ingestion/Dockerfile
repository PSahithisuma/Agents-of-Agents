FROM python:3.12-slim

WORKDIR /app

# System deps needed by Docling (poppler for PDF, tesseract for OCR)
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    tesseract-ocr \
    libgl1 \
    libglib2.0-0 \
    build-essential \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install uv — uses PubGrub resolver (no backtracking depth limit).
RUN pip install --quiet uv

COPY services/ingestion/requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# Pre-download bge-m3 so cold start is fast
RUN python -c "\
import os; os.environ['HF_HOME']='/app/hf_cache'; \
from FlagEmbedding import BGEM3FlagModel; \
BGEM3FlagModel('BAAI/bge-m3', use_fp16=True); \
print('bge-m3 cached')" || echo "bge-m3 pre-download skipped (non-fatal)"

RUN uv pip install --system --no-cache minio

COPY services/ingestion/ .

# PORT env var is set by Cloud Run — use it for the metrics/health port
CMD ["sh", "-c", "INGESTION_METRICS_PORT=${PORT:-9001} python ingestion.py"]
