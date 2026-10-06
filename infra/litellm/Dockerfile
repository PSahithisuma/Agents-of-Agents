FROM ghcr.io/berriai/litellm:main-latest

# Bake the config into the image so Cloud Run can read it
COPY config.yaml /app/config.yaml

# LiteLLM reads VERTEX_PROJECT_ID from environment at runtime
# Set via Cloud Run env vars -- no secrets needed for Vertex AI
# (Cloud Run service account handles auth automatically)
EXPOSE 4000

CMD ["--config", "/app/config.yaml", "--port", "4000", "--host", "0.0.0.0"]
