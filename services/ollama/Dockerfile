FROM ollama/ollama:latest

# Keep model loaded indefinitely (no timeout)
ENV OLLAMA_KEEP_ALIVE=-1
ENV OLLAMA_HOST=0.0.0.0

EXPOSE 11434

COPY start.sh /start.sh
RUN chmod +x /start.sh

CMD ["/start.sh"]