#!/bin/bash
set -e

echo "[Ollama] Starting server..."
ollama serve &
OLLAMA_PID=$!

# Wait for Ollama to be ready
echo "[Ollama] Waiting for server to be ready..."
until curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do
  sleep 2
done
echo "[Ollama] Server ready!"

# Pull qwen3:8b if not already cached
if ! ollama list | grep -q "qwen3:8b"; then
  echo "[Ollama] Pulling qwen3:8b (~4.9GB)..."
  ollama pull qwen3:8b
  echo "[Ollama] qwen3:8b loaded!"
else
  echo "[Ollama] qwen3:8b already present, skipping pull."
fi

echo "[Ollama] Ready to serve requests on :11434"
wait $OLLAMA_PID