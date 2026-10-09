FROM python:3.12-slim

# System deps for torch / sentencepiece
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Models live in the shared hf_models volume (mounted at /models), never in the image
ENV HF_HOME=/models HF_HUB_OFFLINE=1

# CPU-only PyTorch (no multi-GB NVIDIA libraries)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY services/retrieval/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/retrieval/ .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001}"]
