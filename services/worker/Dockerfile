FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Build context = project root; requirements are in services/worker/
COPY services/worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Worker application code
COPY services/worker/ .

# agents/ package lives at project root (mounted as volume in Docker Compose;
# baked into the image for Cloud Run where no volume mounts exist)
COPY agents/ /project/agents/
ENV AGENTS_ROOT=/project

CMD ["python", "run.py"]

