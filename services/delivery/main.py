"""
VibeForge Delivery Service
===========================
POST /deliver/{job_id}  - fast: sign existing GCS bundle; fallback: re-package from GCS files
GET  /bundle/{job_id}   - returns signed download URL (72h TTL)
GET  /health            - liveness probe
"""
from __future__ import annotations

import json
import os
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg
import google.auth
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import storage
from pydantic import BaseModel

app = FastAPI(title="VibeForge Delivery Service")

DATABASE_URL   = os.environ["DATABASE_URL"]
GCS_BUCKET     = os.environ.get("GCS_BUCKET", "vibeforge-artifacts-dev")
BUNDLE_PREFIX  = "bundles"
SIGNED_URL_TTL = int(os.environ.get("SIGNED_URL_TTL_HOURS", "72"))
DEV_TENANT_ID  = os.environ.get("DEV_TENANT_ID", "00000000-0000-0000-0000-000000000001")
SA_EMAIL       = os.environ.get(
    "SERVICE_ACCOUNT_EMAIL",
    "vibeforge-sa@supple-century-505405-v2.iam.gserviceaccount.com"
)


def _signed_url(blob) -> str:
    """Generate a v4 signed URL using Cloud Run workload identity."""
    try:
        credentials, _ = google.auth.default()
        credentials.refresh(GoogleAuthRequest())
        sa_email = getattr(credentials, "service_account_email", SA_EMAIL)
        return blob.generate_signed_url(
            expiration=timedelta(hours=SIGNED_URL_TTL),
            method="GET",
            version="v4",
            service_account_email=sa_email,
            access_token=credentials.token,
        )
    except Exception as e:
        # Fallback: return gs:// URI if signing fails
        import logging
        logging.getLogger(__name__).warning("Signed URL failed: %s", e)
        return f"gs://{blob.bucket.name}/{blob.name}"


class DeliverResponse(BaseModel):
    job_id: str
    download_url: str
    expires_in_hours: int = SIGNED_URL_TTL


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/deliver/{job_id}", response_model=DeliverResponse)
async def deliver(job_id: str):
    """Fast path: if bundle exists in GCS, sign it. Slow path: re-package from GCS files."""
    gcs = storage.Client()
    bucket = gcs.bucket(GCS_BUCKET)
    blob_name = f"{BUNDLE_PREFIX}/{job_id}/{job_id}.tar.gz"
    blob = bucket.blob(blob_name)

    # Fast path - bundle already created by worker
    if blob.exists():
        return DeliverResponse(job_id=job_id, download_url=_signed_url(blob))

    # Slow path - re-package from individual files in GCS
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, false)", DEV_TENANT_ID
        )
        row = await conn.fetchrow(
            "SELECT result_ref, status, tenant_id FROM jobs WHERE id = $1", job_id
        )
    finally:
        await conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["status"] != "completed":
        raise HTTPException(status_code=409, detail=f"Job not completed (status={row['status']})")

    result = json.loads(row["result_ref"]) if row["result_ref"] else {}
    gcs_prefix = result.get("prefix")

    files = {}
    if gcs_prefix:
        src_bucket = gcs.bucket(result.get("bucket", GCS_BUCKET))
        for b in src_bucket.list_blobs(prefix=gcs_prefix):
            rel = b.name[len(gcs_prefix):].lstrip("/")
            if rel:
                try:
                    files[rel] = b.download_as_text(encoding="utf-8")
                except Exception:
                    files[rel] = b.download_as_bytes().decode("latin-1")
    else:
        files = result.get("files", {})

    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = Path(tmpdir) / "bundle"
        bundle_dir.mkdir()
        for path, content in files.items():
            dest = bundle_dir / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        manifest = {"job_id": job_id, "tenant_id": str(row["tenant_id"]), "files": list(files.keys())}
        (bundle_dir / "vibeforge_manifest.json").write_text(json.dumps(manifest, indent=2))
        tar_path = Path(tmpdir) / f"{job_id}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(bundle_dir, arcname=job_id)
        blob.upload_from_filename(str(tar_path), content_type="application/gzip")

    return DeliverResponse(job_id=job_id, download_url=_signed_url(blob))


@app.get("/bundle/{job_id}", response_model=DeliverResponse)
async def get_bundle(job_id: str):
    """Return signed URL for an already-packaged bundle."""
    gcs = storage.Client()
    bucket = gcs.bucket(GCS_BUCKET)
    blob_name = f"{BUNDLE_PREFIX}/{job_id}/{job_id}.tar.gz"
    blob = bucket.blob(blob_name)
    if not blob.exists():
        raise HTTPException(status_code=404, detail="Bundle not found")
    return DeliverResponse(job_id=job_id, download_url=_signed_url(blob))


@app.get("/preview/{job_id}", response_class=HTMLResponse)
async def get_preview(job_id: str):
    """72h live browser preview (Contract C17). Returns an interactive HTML page."""
    gcs = storage.Client()
    bucket = gcs.bucket(GCS_BUCKET)
    bundle_blob_name = f"{BUNDLE_PREFIX}/{job_id}/{job_id}.tar.gz"
    blob = bucket.blob(bundle_blob_name)

    # Get file listing from GCS
    prefix = f"{BUNDLE_PREFIX}/{job_id}/"
    file_list = [b.name.replace(prefix, "") for b in bucket.list_blobs(prefix=prefix)
                 if not b.name.endswith(".tar.gz")]

    # Generate signed download URL
    bundle_url = _signed_url(blob) if blob.exists() else None
    expires_ts = (datetime.utcnow() + timedelta(hours=SIGNED_URL_TTL)).strftime("%Y-%m-%d %H:%M UTC")

    # Build file preview HTML
    file_items_html = ""
    for fname in sorted(file_list)[:30]:
        icon = "📄"
        if fname.endswith(".py"): icon = "🐍"
        elif fname.endswith(".java"): icon = "☕"
        elif fname.endswith(".yaml") or fname.endswith(".yml"): icon = "📋"
        elif fname.endswith(".json"): icon = "📦"
        elif "Dockerfile" in fname: icon = "🐳"
        elif "README" in fname: icon = "📖"
        elif "requirements" in fname: icon = "📎"
        file_items_html += f'<div class="file-item">{icon} <span class="fname">{fname}</span></div>\n'

    download_section = f'''
        <a class="btn-download" href="{bundle_url}" download="{job_id}.tar.gz">
            ⬇ Download Bundle (.tar.gz)
        </a>''' if bundle_url else '<div class="no-bundle">Bundle not yet available</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VibeForge Preview — {job_id[:8]}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:#0d0d1f; color:#e0e0f0; font-family:'Inter',sans-serif; min-height:100vh; }}
  .header {{ background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%); border-bottom:1px solid #2d2d4e; padding:1.5rem 2rem; }}
  .logo {{ font-size:1.5rem; font-weight:700; color:#c4b5fd; }}
  .logo span {{ color:#7c3aed; }}
  .badge {{ display:inline-block; background:#7c3aed20; color:#c4b5fd; border:1px solid #7c3aed40;
            border-radius:6px; padding:3px 10px; font-size:0.75rem; font-family:'JetBrains Mono',monospace; margin-left:12px; }}
  .container {{ max-width:1100px; margin:0 auto; padding:2rem; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:1.5rem; margin-top:1.5rem; }}
  .card {{ background:#1a1a2e; border:1px solid #2d2d4e; border-radius:12px; padding:1.5rem; }}
  .card-title {{ font-size:0.72rem; font-weight:600; letter-spacing:0.1em; text-transform:uppercase;
                 color:#7878aa; margin-bottom:1rem; }}
  .job-id {{ font-family:'JetBrains Mono',monospace; font-size:1.1rem; color:#c4b5fd; word-break:break-all; }}
  .meta-row {{ display:flex; justify-content:space-between; padding:6px 0;
               border-bottom:1px solid #2d2d4e20; font-size:0.85rem; }}
  .meta-key {{ color:#9090b0; }}
  .meta-val {{ color:#e0e0f0; font-weight:500; }}
  .file-list {{ max-height:320px; overflow-y:auto; }}
  .file-item {{ display:flex; align-items:center; gap:8px; padding:5px 0;
                border-bottom:1px solid #2d2d4e20; font-size:0.82rem; }}
  .fname {{ font-family:'JetBrains Mono',monospace; color:#c4b5fd; }}
  .btn-download {{ display:block; background:linear-gradient(135deg,#7c3aed,#4f46e5);
                   color:white; text-decoration:none; padding:0.9rem 1.5rem;
                   border-radius:10px; text-align:center; font-weight:600;
                   font-size:1rem; margin-top:1rem; transition:opacity 0.2s; }}
  .btn-download:hover {{ opacity:0.85; }}
  .expires {{ font-size:0.78rem; color:#7878aa; text-align:center; margin-top:0.5rem; }}
  .stack-badge {{ display:inline-block; background:#4c1d9520; color:#c4b5fd;
                  border:1px solid #7c3aed40; border-radius:6px; padding:4px 10px;
                  font-size:0.78rem; margin:3px; }}
  .status-live {{ color:#4ade80; font-weight:600; }}
  .quick-start {{ background:#0d0d1f; border:1px solid #2d2d4e; border-radius:8px;
                  padding:1rem; font-family:'JetBrains Mono',monospace;
                  font-size:0.8rem; color:#c4b5fd; line-height:1.7; margin-top:0.75rem; }}
</style>
</head>
<body>
<div class="header">
  <div class="logo">🔥 Vibe<span>Forge</span> <span class="badge">PREVIEW</span></div>
</div>
<div class="container">
  <div style="margin-top:1rem;">
    <div style="font-size:1.6rem;font-weight:700;color:#e0e0f0;">Generated App Preview</div>
    <div style="font-size:0.85rem;color:#7878aa;margin-top:0.25rem;">
      72h live preview window — expires {expires_ts}
    </div>
  </div>

  <div class="grid">
    <!-- Job Info -->
    <div class="card">
      <div class="card-title">📋 Job Details</div>
      <div class="job-id">{job_id}</div>
      <div style="margin-top:1rem;">
        <div class="meta-row">
          <span class="meta-key">Status</span>
          <span class="meta-val status-live">✅ Ready for Download</span>
        </div>
        <div class="meta-row">
          <span class="meta-key">Bundle</span>
          <span class="meta-val">{job_id}.tar.gz</span>
        </div>
        <div class="meta-row">
          <span class="meta-key">Files</span>
          <span class="meta-val">{len(file_list)} files</span>
        </div>
        <div class="meta-row">
          <span class="meta-key">Expires</span>
          <span class="meta-val">{expires_ts}</span>
        </div>
        <div class="meta-row">
          <span class="meta-key">SBOM</span>
          <span class="meta-val"><a href="/sbom/{job_id}" style="color:#c4b5fd;">CycloneDX JSON</a></span>
        </div>
      </div>
    </div>

    <!-- Download -->
    <div class="card">
      <div class="card-title">⬇ Download & Run</div>
      {download_section}
      <div class="expires">Link valid for {SIGNED_URL_TTL} hours</div>
      <div class="quick-start">
# Quick Start
tar -xzf {job_id}.tar.gz
cd {job_id}/
cat runbook.md           # step-by-step setup
docker compose up --build  # or follow runbook
      </div>
    </div>

    <!-- File List -->
    <div class="card" style="grid-column:1/-1;">
      <div class="card-title">📁 Generated Files ({len(file_list)})</div>
      <div class="file-list">
{file_items_html if file_items_html else '<div style="color:#7878aa;font-style:italic;">No files listed — bundle may be processing</div>'}
      </div>
    </div>
  </div>

  <div style="text-align:center;margin-top:2rem;color:#7878aa;font-size:0.78rem;">
    Generated by VibeForge · <a href="https://ui-5rpnepdwnq-uc.a.run.app" style="color:#c4b5fd;">Open Platform</a>
  </div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=200)


def _generate_sbom(files: dict, job_id: str, tenant_id: str) -> dict:
    """Generate a minimal CycloneDX-compatible SBOM from detected dependency files."""
    components = []
    for fname, content in files.items():
        if fname.endswith("requirements.txt"):
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.replace("==", "@").replace(">=", "@>=").split("@")
                    name = parts[0].strip()
                    version = parts[1].strip() if len(parts) > 1 else "unknown"
                    components.append({"type": "library", "name": name, "version": version, "ecosystem": "pypi"})
        elif fname.endswith("pom.xml"):
            import re
            for m in re.finditer(r"<artifactId>(.+?)</artifactId>", content):
                components.append({"type": "library", "name": m.group(1), "version": "unknown", "ecosystem": "maven"})
        elif fname == "package.json":
            try:
                pkg = json.loads(content)
                for dep, ver in {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}.items():
                    components.append({"type": "library", "name": dep, "version": ver, "ecosystem": "npm"})
            except Exception:
                pass

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "metadata": {"timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                     "component": {"type": "application", "name": job_id}},
        "components": components,
    }


def _generate_runbook(job_id: str, files: dict) -> str:
    """Generate a runbook.md for the delivered application."""
    has_docker = any("Dockerfile" in f for f in files)
    has_compose = any("docker-compose" in f for f in files)
    has_makefile = "Makefile" in files
    has_py = any(f.endswith(".py") for f in files)
    has_java = any(f.endswith(".java") or "pom.xml" in f for f in files)

    lines = [
        f"# VibeForge â€” Runbook for Job `{job_id}`",
        "",
        "## Quick Start",
        "",
    ]
    if has_compose:
        lines += ["### Docker Compose (recommended)", "```bash", "docker compose up --build", "```", ""]
    if has_docker:
        lines += ["### Docker", "```bash", "docker build -t app .", "docker run -p 8080:8080 app", "```", ""]
    if has_java:
        lines += ["### Java / Maven", "```bash", "mvn clean package -DskipTests", "java -jar target/*.jar", "```", ""]
    if has_py:
        lines += ["### Python", "```bash", "pip install -r requirements.txt", "uvicorn app.main:app --host 0.0.0.0 --port 8080", "```", ""]

    lines += [
        "## Environment Variables",
        "| Variable | Description | Default |",
        "|---|---|---|",
        "| DATABASE_URL | PostgreSQL connection string | postgresql://localhost:5432/appdb |",
        "| PORT | HTTP server port | 8080 |",
        "",
        "## Files in Bundle",
        *[f"- `{f}`" for f in sorted(files.keys())[:30]],
        "",
        "---",
        f"*Generated by VibeForge â€” Job ID: {job_id}*",
    ]
    return "\n".join(lines)