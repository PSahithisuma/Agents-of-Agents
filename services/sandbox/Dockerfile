FROM python:3.12-slim
WORKDIR /app
COPY services/sandbox/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/sandbox/sandbox.py .
CMD ["uvicorn", "sandbox:app", "--host", "0.0.0.0", "--port", "8080"]
