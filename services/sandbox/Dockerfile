FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY services/sandbox/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/sandbox/sandbox.py .
# Bake cloudbuild YAMLs so sandbox can upload them to GCS at runtime
COPY services/sandbox/cloudbuild/ /app/cloudbuild/
CMD ["uvicorn", "sandbox:app", "--host", "0.0.0.0", "--port", "8080"]
