"""
Sandbox-lite service.

Builds a generated repo's own Dockerfile, runs it next to a throwaway Postgres on a private network with no
internet access, checks it, and serves it for a limited time at  /preview/<token>/ .

POST /run                  start a sandbox for a finished job   (internal only)
GET  /report/{job_id}      gate report for that job
DELETE /preview/{job_id}   stop a sandbox early
ANY  /preview/{token}/...  reverse proxy to the running app
GET  /health               liveness

Isolation (lite): private internal network, memory/CPU/process limits, all capabilities dropped,
no-new-privileges, read-only filesystem with a small /tmp. Docker is reached only through a filtering proxy.
Not meant for running arbitrary customers' code.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import secrets
import tarfile
import time
from datetime import datetime, timezone
from typing import Literal, Optional

import docker
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

log = logging.getLogger("sandbox")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="VibeForge Sandbox (lite)")

DOCKER_HOST = os.environ.get("DOCKER_HOST", "tcp://dockerproxy:2375")
GITEA_URL = os.environ.get("GITEA_URL", "http://gitea:3000").rstrip("/")
GITEA_TOKEN = os.environ.get("GITEA_TOKEN", "")
GITEA_OWNER = os.environ.get("GITEA_OWNER", "vibeforge-admin")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
TTL_SECONDS = int(os.environ.get("SANDBOX_TTL_SECONDS", "1800"))
MAX_PREVIEWS = int(os.environ.get("SANDBOX_MAX_PREVIEWS", "2"))
BUILD_TIMEOUT = int(os.environ.get("SANDBOX_BUILD_TIMEOUT", "900"))
HEALTH_TIMEOUT = int(os.environ.get("SANDBOX_HEALTH_TIMEOUT", "240"))
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024
REPORT_DIR = os.environ.get("SANDBOX_REPORT_DIR", "/data/reports")
SELF_ID = os.environ.get("HOSTNAME", "")

LABEL = "vf.sandbox"
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}

_client: Optional[docker.DockerClient] = None
_tokens: dict[str, tuple[str, float]] = {}      # token -> (app container name, expires epoch)
_running: dict[str, str] = {}                    # job_id -> status while a run is in flight
_build_lock = asyncio.Lock()


def dkr() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.DockerClient(base_url=DOCKER_HOST, timeout=120)
    return _client


# ── models ────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    job_id: str
    tenant_id: str = "default"
    stack: Literal["java_springboot", "python_fastapi", "dotnet"] = "java_springboot"
    repo: Optional[str] = None          # Gitea repo name; derived from the job's result link when omitted


class StepResult(BaseModel):
    name: str
    passed: bool
    duration_s: float
    detail: dict = {}


class GateReport(BaseModel):
    job_id: str
    tenant_id: str
    stack: str
    overall: Literal["pass", "fail"]
    steps: list[StepResult]
    coverage_pct: Optional[float] = None
    sbom_url: Optional[str] = None
    preview_url: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: datetime


# ── helpers ───────────────────────────────────────────────────────────────────

def short(job_id: str) -> str:
    return "".join(c for c in job_id if c.isalnum())[:12].lower()


def repack_archive(gz_bytes: bytes) -> bytes:
    """Gitea archives have one top folder. Return a tar with that folder removed, keeping only plain files and
    folders with safe names (no absolute paths, no '..', no links, no devices)."""
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(gz_bytes), mode="r:gz") as src, tarfile.open(fileobj=out, mode="w") as dst:
        for m in src.getmembers():
            parts = [p for p in m.name.split("/") if p not in ("", ".")]
            if len(parts) < 2 and not m.isdir():
                continue
            rel = parts[1:]
            if not rel or any(p == ".." for p in rel) or m.name.startswith("/"):
                continue
            if not (m.isfile() or m.isdir()):
                continue
            m.name = "/".join(rel)
            m.uid = m.gid = 0
            m.uname = m.gname = ""
            m.mode = 0o755 if m.isdir() else 0o644
            dst.addfile(m, src.extractfile(m) if m.isfile() else None)
    return out.getvalue()


def repo_from_result_ref(ref: str) -> str:
    return (ref or "").rstrip("/").rsplit("/", 1)[-1]


def save_report(rep: GateReport) -> None:
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, f"{short(rep.job_id)}.json"), "w") as f:
        f.write(rep.model_dump_json())


def load_report(job_id: str) -> Optional[dict]:
    p = os.path.join(REPORT_DIR, f"{short(job_id)}.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def preview_url_for(token: str, stack: str = "java_springboot") -> str:
    path = {"java_springboot": "swagger-ui.html", "python_fastapi": "docs", "dotnet": "swagger"}.get(stack, "")
    return f"{PUBLIC_BASE_URL}/preview/{token}/{path}"


def _app_containers() -> list:
    return dkr().containers.list(all=True, filters={"label": [f"{LABEL}=1", "vf.role=app"]})


def _destroy_sync(job_id: str) -> None:
    c = dkr()
    jid = short(job_id)
    for ct in c.containers.list(all=True, filters={"label": [f"vf.job={jid}"]}):
        try:
            ct.remove(force=True, v=True)
        except Exception as e:                                  # noqa: BLE001
            log.warning("remove container %s: %s", ct.name, e)
    for net in c.networks.list(filters={"label": [f"vf.job={jid}"]}):
        try:
            if SELF_ID:
                try:
                    net.disconnect(SELF_ID, force=True)
                except Exception:                               # noqa: BLE001
                    pass
            net.remove()
        except Exception as e:                                  # noqa: BLE001
            log.warning("remove network %s: %s", net.name, e)
    try:
        c.images.remove(f"vf-sbx-{jid}", force=True)
    except Exception:                                           # noqa: BLE001
        pass
    for tok, (host, _) in list(_tokens.items()):
        if host.startswith(f"vf-app-{jid}"):
            _tokens.pop(tok, None)


async def destroy(job_id: str) -> None:
    await asyncio.to_thread(_destroy_sync, job_id)


def _reload_tokens_sync() -> None:
    for ct in _app_containers():
        lb = ct.labels or {}
        tok, exp = lb.get("vf.token"), lb.get("vf.expires")
        if tok and exp:
            _tokens[tok] = (ct.name, float(exp))


# ── the run ───────────────────────────────────────────────────────────────────

async def _fetch_repo(repo: str) -> bytes:
    url = f"{GITEA_URL}/api/v1/repos/{GITEA_OWNER}/{repo}/archive/main.tar.gz"
    async with httpx.AsyncClient(timeout=60) as cx:
        r = await cx.get(url, headers={"Authorization": f"token {GITEA_TOKEN}"})
    if r.status_code != 200:
        raise RuntimeError(f"Gitea returned {r.status_code} for repo {repo}")
    if len(r.content) > MAX_ARCHIVE_BYTES:
        raise RuntimeError("repo archive is too large")
    return r.content


def _build_sync(tar_bytes: bytes, tag: str) -> str:
    # dkr().images.build() (docker-py high-level helper) internally uses itertools.tee()
    # to parse the build-log stream and crashes with "'itertools._tee' object is not
    # subscriptable" against this daemon's (BuildKit-style) stream format. Bypass it and
    # use the low-level API client, parsing the JSON build-log stream ourselves.
    api = dkr().api
    image_id = None
    log_lines: list[str] = []
    try:
        for chunk in api.build(
                fileobj=io.BytesIO(tar_bytes), custom_context=True, tag=tag, rm=True, forcerm=True,
                timeout=BUILD_TIMEOUT, labels={LABEL: "1"}, decode=True,
                container_limits={"memory": 2 * 1024 ** 3, "memswap": 2 * 1024 ** 3, "cpushares": 512}):
            if "stream" in chunk and chunk["stream"].strip():
                log_lines.append(chunk["stream"].strip())
            if "error" in chunk:
                tail = " | ".join(log_lines[-8:])
                raise RuntimeError(f"docker build failed: {(tail + ' | ' + chunk['error'])[-600:]}")
            aux = chunk.get("aux")
            if isinstance(aux, dict) and aux.get("ID"):
                image_id = aux["ID"]
        if not image_id:
            try:
                image_id = dkr().images.get(tag).id
            except Exception:                                       # noqa: BLE001
                pass
        if not image_id:
            tail = " | ".join(log_lines[-8:])
            raise RuntimeError(f"docker build finished without an image id: {tail[-600:]}")
        return image_id
    except docker.errors.APIError as e:
        tail = " | ".join(log_lines[-8:])
        raise RuntimeError(f"docker build failed: {(tail + ' | ' + str(e))[-600:]}") from e


def _start_sync(jid: str, token: str, expires: float, db_password: str) -> tuple[str, str]:
    c = dkr()
    labels = {LABEL: "1", "vf.job": jid}
    net = c.networks.create(f"vf-net-{jid}", driver="bridge", internal=True, labels=labels)
    if SELF_ID:
        net.connect(SELF_ID)
    pg_name, app_name = f"vf-pg-{jid}", f"vf-app-{jid}"
    c.containers.run(
        "postgres:16-alpine", name=pg_name, detach=True, network=f"vf-net-{jid}",
        environment={"POSTGRES_DB": "appdb", "POSTGRES_PASSWORD": db_password},
        labels={**labels, "vf.role": "db"}, mem_limit="384m", pids_limit=128,
        cap_drop=["ALL"], cap_add=["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER"])
    return pg_name, app_name


def _start_app_sync(jid: str, token: str, expires: float, pg_name: str, app_name: str, db_password: str, stack: str = "java_springboot") -> None:
    if stack == "java_springboot":
        env = {
            "SPRING_DATASOURCE_URL": f"jdbc:postgresql://{pg_name}:5432/appdb",
            "SPRING_DATASOURCE_USERNAME": "postgres",
            "SPRING_DATASOURCE_PASSWORD": db_password,
            "SERVER_SERVLET_CONTEXT_PATH": f"/preview/{token}",
            "SERVER_FORWARD_HEADERS_STRATEGY": "framework",
            "JAVA_OPTS": "-Xmx384m -Xms128m",
        }
    elif stack == "python_fastapi":
        env = {"DATABASE_URL": "sqlite:////tmp/app.db", "PATH_PREFIX": f"/preview/{token}"}
    else:  # dotnet
        env = {"DATABASE_URL": "Data Source=/tmp/app.db", "PATH_BASE": f"/preview/{token}"}
    dkr().containers.run(
        f"vf-sbx-{jid}", name=app_name, detach=True, network=f"vf-net-{jid}",
        environment=env,
        labels={LABEL: "1", "vf.job": jid, "vf.role": "app", "vf.token": token, "vf.expires": str(expires)},
        mem_limit="768m", memswap_limit="768m", nano_cpus=1_000_000_000, pids_limit=256,
        cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
        read_only=True, tmpfs={"/tmp": "size=64m,mode=1777"})


def _logs_sync(name: str) -> str:
    try:
        return dkr().containers.get(name).logs(tail=25).decode(errors="replace")[-1500:]
    except Exception as e:                                      # noqa: BLE001
        return f"(no logs: {e})"


async def _wait_tcp(host: str, port: int, timeout: int) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
            w.close()
            return True
        except Exception:                                       # noqa: BLE001
            await asyncio.sleep(2)
    return False


async def _http_ok(url: str, timeout: int, want_up: bool = False) -> tuple[bool, str]:
    end = time.time() + timeout
    last = ""
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as cx:
        while time.time() < end:
            try:
                r = await cx.get(url)
                last = f"HTTP {r.status_code}"
                if r.status_code == 200 and (not want_up or '"UP"' in r.text.replace(" ", "")):
                    return True, last
            except Exception as e:                              # noqa: BLE001
                last = type(e).__name__
            await asyncio.sleep(4)
    return False, last


async def run_job(req: RunRequest, repo: str) -> None:
    jid = short(req.job_id)
    token = secrets.token_urlsafe(12)
    db_pw = secrets.token_urlsafe(16)
    started = datetime.now(timezone.utc)
    steps: list[StepResult] = []
    preview: Optional[str] = None
    expires_at: Optional[datetime] = None
    ok = False

    async def step(name: str, coro):
        t0 = time.time()
        try:
            detail = await coro()
            steps.append(StepResult(name=name, passed=True, duration_s=round(time.time() - t0, 1), detail=detail or {}))
            return True
        except Exception as e:                                  # noqa: BLE001
            steps.append(StepResult(name=name, passed=False, duration_s=round(time.time() - t0, 1),
                                    detail={"error": str(e)[:700]}))
            return False

    try:
        box: dict = {}

        async def fetch():
            box["tar"] = repack_archive(await _fetch_repo(repo))
            return {"repo": repo, "bytes": len(box["tar"])}

        async def build():
            async with _build_lock:
                await asyncio.to_thread(_build_sync, box["tar"], f"vf-sbx-{jid}")
            return {"image": f"vf-sbx-{jid}"}

        async def start():
            expires = time.time() + TTL_SECONDS
            box["expires"] = expires
            pg, ap = await asyncio.to_thread(_start_sync, jid, token, expires, db_pw)
            box["pg"], box["app"] = pg, ap
            if not await _wait_tcp(pg, 5432, 90):
                raise RuntimeError("database did not start")
            await asyncio.to_thread(_start_app_sync, jid, token, expires, pg, ap, db_pw, req.stack)
            _tokens[token] = (ap, expires)
            return {"app": ap}

        async def health():
            if req.stack == "java_springboot":
                hpath = f"http://{box['app']}:8080/preview/{token}/actuator/health"
                want_up = True
            else:
                hpath = f"http://{box['app']}:8080/preview/{token}/health"
                want_up = False
            ok_, last = await _http_ok(hpath, HEALTH_TIMEOUT, want_up)
            if not ok_:
                raise RuntimeError(f"app did not become healthy ({last}). Logs: {await asyncio.to_thread(_logs_sync, box['app'])}")
            return {"status": "UP"}

        async def docs():
            base = f"http://{box['app']}:8080/preview/{token}"
            if req.stack == "java_springboot":
                a, la = await _http_ok(f"{base}/api-docs", 30)
                b, lb = await _http_ok(f"{base}/swagger-ui.html", 30)
            elif req.stack == "python_fastapi":
                a, la = await _http_ok(f"{base}/openapi.json", 30)
                b, lb = await _http_ok(f"{base}/docs", 30)
            else:  # dotnet
                a, la = await _http_ok(f"{base}/swagger/v1/swagger.json", 30)
                b, lb = await _http_ok(f"{base}/swagger", 30)
            if not (a and b):
                raise RuntimeError(f"api-docs: {la}, swagger-ui: {lb}")
            return {"api-docs": "200", "swagger-ui": "200"}

        ok = (await step("fetch", fetch) and await step("build", build) and await step("start", start)
              and await step("health", health) and await step("docs", docs))
        if ok:
            preview = preview_url_for(token, req.stack)
            expires_at = datetime.fromtimestamp(box["expires"], timezone.utc)
    finally:
        rep = GateReport(job_id=req.job_id, tenant_id=req.tenant_id, stack=req.stack,
                         overall="pass" if ok else "fail", steps=steps, preview_url=preview,
                         expires_at=expires_at, created_at=started)
        try:
            save_report(rep)
        except Exception as e:                                  # noqa: BLE001
            log.error("could not save report: %s", e)
        if not ok:
            await destroy(req.job_id)
        _running.pop(req.job_id, None)


# ── routes ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    try:
        await asyncio.to_thread(_reload_tokens_sync)
    except Exception as e:                                      # noqa: BLE001
        log.warning("could not reload previews: %s", e)
    asyncio.create_task(janitor())


async def janitor():
    while True:
        await asyncio.sleep(30)
        try:
            now = time.time()
            for ct in await asyncio.to_thread(_app_containers):
                exp = float((ct.labels or {}).get("vf.expires", "0") or 0)
                if exp and exp < now:
                    job = (ct.labels or {}).get("vf.job", "")
                    log.info("preview %s expired", job)
                    await asyncio.to_thread(_destroy_sync, job)
        except Exception as e:                                  # noqa: BLE001
            log.warning("janitor: %s", e)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "sandbox", "mode": "local-docker"}


@app.post("/run", status_code=202)
async def run(req: RunRequest):
    if req.job_id in _running:
        raise HTTPException(409, "already running for this job")
    live = len(await asyncio.to_thread(_app_containers)) + len(_running)
    if live >= MAX_PREVIEWS:
        raise HTTPException(429, f"capacity reached ({MAX_PREVIEWS} previews at once); try again later")
    repo = req.repo
    if not repo:
        raise HTTPException(400, "repo is required (Gitea repository name)")
    _running[req.job_id] = "running"
    asyncio.create_task(run_job(req, repo))
    return {"job_id": req.job_id, "status": "running"}


@app.get("/report/{job_id}")
async def report(job_id: str):
    rep = load_report(job_id)
    if rep:
        return rep
    if job_id in _running:
        return Response(content=json.dumps({"job_id": job_id, "status": "running"}),
                        media_type="application/json", status_code=202)
    raise HTTPException(404, "no report for this job")


@app.delete("/preview/{job_id}")
async def stop(job_id: str):
    await destroy(job_id)
    return {"job_id": job_id, "status": "stopped"}


@app.api_route("/preview/{token}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(token: str, path: str, request: Request):
    hit = _tokens.get(token)
    if not hit or hit[1] < time.time():
        raise HTTPException(404, "preview not found or expired")
    host = hit[0]
    body = await request.body()
    if len(body) > 5 * 1024 * 1024:
        raise HTTPException(413, "request too large")
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    fwd["x-forwarded-proto"] = request.headers.get("x-forwarded-proto", "https")
    fwd["x-forwarded-host"] = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    url = f"http://{host}:8080/preview/{token}/{path}"
    cx = httpx.AsyncClient(timeout=60)
    try:
        up = await cx.send(cx.build_request(request.method, url, params=list(request.query_params.multi_items()), headers=fwd,
                                            content=body), stream=True)
    except Exception:                                           # noqa: BLE001
        await cx.aclose()
        raise HTTPException(502, "the preview app is not reachable")
    out_headers = {k: v for k, v in up.headers.items() if k.lower() not in HOP_BY_HOP}

    async def gen():
        try:
            async for chunk in up.aiter_raw():
                yield chunk
        finally:
            await up.aclose()
            await cx.aclose()

    return StreamingResponse(gen(), status_code=up.status_code, headers=out_headers)


@app.get("/preview/{token}")
async def preview_root(token: str):
    return RedirectResponse(f"/preview/{token}/swagger-ui.html")
