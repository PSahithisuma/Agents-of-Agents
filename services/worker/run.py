"""
Cloud Run entrypoint for VibeForge ARQ worker.
Runs a minimal HTTP health server alongside the ARQ worker.
"""
from __future__ import annotations

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s - %(message)s")

PORT = int(os.getenv("PORT", "8080"))

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","service":"vibeforge-worker"}')
    def log_message(self, format, *args):
        pass

def _start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info("[worker] Health server on port %d", PORT)
    server.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=_start_health_server, daemon=True).start()
    logger.info("[worker] Starting ARQ worker...")
    from arq import run_worker
    from worker import WorkerSettings
    # run_worker manages its own event loop - do NOT wrap in asyncio.run()
    run_worker(WorkerSettings)
