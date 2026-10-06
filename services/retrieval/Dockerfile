FROM python:3.12-slim

# System deps for torch / sentencepiece
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Use the correct requirements.txt (repo-root build context)
COPY services/retrieval/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download bge-m3 and bge-reranker during build so cold start is fast
RUN HF_HOME=/app/hf_cache python -c "\
from FlagEmbedding import BGEM3FlagModel; \
BGEM3FlagModel('BAAI/bge-m3', use_fp16=True); \
from FlagEmbedding import FlagReranker; \
FlagReranker('BAAI/bge-reranker-v2-m3', use_fp16=True); \
print('Models cached')" || echo "model pre-download skipped"

# Copy only the retrieval service files
COPY services/retrieval/ .

# Respect Cloud Run PORT env var (defaults to 8001 locally)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001}"]
