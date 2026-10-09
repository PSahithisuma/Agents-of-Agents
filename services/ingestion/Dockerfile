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

# Models live in the shared hf_models volume (mounted at /models), never in the image
ENV HF_HOME=/models HF_HUB_OFFLINE=1

RUN pip install --quiet uv

# CPU-only PyTorch first (no multi-GB NVIDIA libraries)
RUN uv pip install --system --no-cache torch torchvision --index-url https://download.pytorch.org/whl/cpu

COPY services/ingestion/requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt
RUN uv pip install --system --no-cache minio

COPY services/ingestion/ .

CMD ["sh", "-c", "INGESTION_METRICS_PORT=${PORT:-9001} python ingestion.py"]
