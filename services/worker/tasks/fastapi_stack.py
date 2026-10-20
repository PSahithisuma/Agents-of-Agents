"""python_fastapi stack profile: deterministic generation from the Spec IR (no model call), gated by compileall + pytest."""
import os, re, shutil, asyncio, keyword, logging

logger = logging.getLogger(__name__)
GATE_PY = "/opt/fastapi-gate/bin/python"
_RESERVED = {"id", "tenant_id", "created_at", "updated_at", "metadata", "registry", "query"}
_TYPES = {
    "string": ("String(255)", "str", "'sample'"),
    "int": ("Integer", "int", "1"),
    "uuid": ("String(36)", "str", "'00000000-0000-4000-8000-000000000001'"),
    "decimal": ("Float", "float", "10.5"),
    "bool": ("Boolean", "bool", "True"),
    "datetime": ("DateTime", "datetime", "'2026-01-01T00:00:00'"),
    "enum": ("String(60)", "str", "'ACTIVE'"),
}


def _pascal(s):
    p = re.sub(r"[^A-Za-z0-9]", "", str(s or ""))
    if not p or not p[0].isalpha():
        p = "E" + p
    return p[0].upper() + p[1:]


def _snake(s):
    x = re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(s or "")).lower()).strip("_")
    if not x or not x[0].isalpha():
        x = "f_" + x
    if keyword.iskeyword(x) or x in _RESERVED:
        x += "_value"
    return x


def _col(t):
    t = str(t or "string")
    return _TYPES.get(t, _TYPES["string"])


def _entities(spec):
    out, names, paths = [], set(), set()
    for e in (spec.get("domain_model") or {}).get("entities") or []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        name = _pascal(e["name"])
        path = _snake(e.get("table") or e["name"])
        if name in names or path in paths:
            continue
        fields, fn = [], set()
        for f in e.get("fields") or []:
            if not isinstance(f, dict) or not f.get("name"):
                continue
            n = _snake(f["name"])
            if n in fn:
                continue
            fn.add(n)
            fields.append((n, f.get("type")))
        names.add(name)
        paths.add(path)
        out.append({"name": name, "path": path, "fields": fields})
    return out


def generate_files(spec):
    ents = _entities(spec)
    app = spec.get("app_name") or "generated-app"
    f = {}
    f["requirements.txt"] = ("fastapi==0.115.6\nuvicorn==0.34.0\nsqlalchemy==2.0.36\npydantic==2.10.4\npsycopg2-binary==2.9.10\n")
    f["requirements-dev.txt"] = "-r requirements.txt\nhttpx==0.28.1\npytest==8.3.4\n"
    f["Dockerfile"] = ("FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\n"
                       "COPY . .\nEXPOSE 8080\nCMD [\"uvicorn\", \"app.main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8080\"]\n")
    f[".gitignore"] = "__pycache__/\n*.pyc\n*.db\n.pytest_cache/\n"
    f["README.md"] = (f"# {app}\n\nFastAPI service generated from the Spec IR by Agents of Agents (python_fastapi profile).\n\n"
                      f"Entities: {', '.join(e['name'] for e in ents)}\n\n```\npip install -r requirements-dev.txt\npytest\n"
                      "uvicorn app.main:app --port 8080\n```\n\nSet DATABASE_URL (default: local SQLite file). "
                      "This is a generated template, not a compliance certification.\n")
    f["app/__init__.py"] = "# package\n"
    f["app/routers/__init__.py"] = "# package\n"
    f["tests/__init__.py"] = "# package\n"
    f["app/database.py"] = (
        "import os\nfrom sqlalchemy import create_engine\nfrom sqlalchemy.orm import sessionmaker, declarative_base\n\n"
        "URL = os.environ.get(\"DATABASE_URL\", \"sqlite:///./app.db\")\n"
        "engine = create_engine(URL, connect_args={\"check_same_thread\": False} if URL.startswith(\"sqlite\") else {})\n"
        "SessionLocal = sessionmaker(bind=engine, autoflush=False)\nBase = declarative_base()\n\n\n"
        "def get_db():\n    db = SessionLocal()\n    try:\n        yield db\n    finally:\n        db.close()\n")
    m = ["import uuid", "from datetime import datetime, timezone",
         "from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime", "from .database import Base", "",
         "", "def _uuid():", "    return str(uuid.uuid4())", "", "", "def _now():", "    return datetime.now(timezone.utc)", ""]
    s = ["from datetime import datetime", "from typing import Optional", "from pydantic import BaseModel, ConfigDict", ""]
    for e in ents:
        m += ["", f"class {e['name']}(Base):", f"    __tablename__ = \"{e['path']}\"",
              "    id = Column(String(36), primary_key=True, default=_uuid)", "    tenant_id = Column(String(36), nullable=True, index=True)"]
        s += ["", f"class {e['name']}Create(BaseModel):"]
        for n, t in e["fields"]:
            c, py, _ = _col(t)
            m.append(f"    {n} = Column({c}, nullable=True)")
            s.append(f"    {n}: Optional[{py}] = None")
        if not e["fields"]:
            s.append("    pass")
        m += ["    created_at = Column(DateTime, default=_now)", "    updated_at = Column(DateTime, default=_now, onupdate=_now)"]
        s += ["", f"class {e['name']}Out({e['name']}Create):", "    model_config = ConfigDict(from_attributes=True)",
              "    id: str", "    created_at: Optional[datetime] = None", "    updated_at: Optional[datetime] = None"]
    f["app/models.py"] = "\n".join(m) + "\n"
    f["app/schemas.py"] = "\n".join(s) + "\n"
    for e in ents:
        N, P = e["name"], e["path"]
        f[f"app/routers/{P}.py"] = (
            "from fastapi import APIRouter, Depends, HTTPException\nfrom sqlalchemy.orm import Session\n"
            "from ..database import get_db\nfrom .. import models, schemas\n\n"
            f"router = APIRouter(prefix=\"/api/v1/{P}\", tags=[\"{N}\"])\n\n\n"
            f"@router.get(\"\", response_model=list[schemas.{N}Out])\n"
            "def list_items(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):\n"
            f"    return db.query(models.{N}).offset(skip).limit(min(limit, 500)).all()\n\n\n"
            f"@router.post(\"\", response_model=schemas.{N}Out, status_code=201)\n"
            f"def create_item(body: schemas.{N}Create, db: Session = Depends(get_db)):\n"
            f"    obj = models.{N}(**body.model_dump())\n    db.add(obj)\n    db.commit()\n    db.refresh(obj)\n    return obj\n\n\n"
            f"def _get(db: Session, item_id: str):\n    obj = db.get(models.{N}, item_id)\n"
            "    if obj is None:\n        raise HTTPException(status_code=404, detail=\"not found\")\n    return obj\n\n\n"
            f"@router.get(\"/{{item_id}}\", response_model=schemas.{N}Out)\n"
            "def get_item(item_id: str, db: Session = Depends(get_db)):\n    return _get(db, item_id)\n\n\n"
            f"@router.put(\"/{{item_id}}\", response_model=schemas.{N}Out)\n"
            f"def update_item(item_id: str, body: schemas.{N}Create, db: Session = Depends(get_db)):\n"
            "    obj = _get(db, item_id)\n    for k, v in body.model_dump(exclude_unset=True).items():\n        setattr(obj, k, v)\n"
            "    db.commit()\n    db.refresh(obj)\n    return obj\n\n\n"
            "@router.delete(\"/{item_id}\", status_code=204)\n"
            "def delete_item(item_id: str, db: Session = Depends(get_db)):\n    db.delete(_get(db, item_id))\n    db.commit()\n")
    imports = "".join(f"from .routers import {e['path']} as r_{e['path']}\n" for e in ents)
    inc = "".join(f"app.include_router(r_{e['path']}.router)\n" for e in ents)
    f["app/main.py"] = ("from fastapi import FastAPI\nfrom .database import Base, engine\nfrom . import models  # noqa: F401\n" + imports +
                        "\nimport os\nfrom starlette.types import ASGIApp, Receive, Scope, Send\n\n"
                        "class _StripPrefixMiddleware:\n"
                        "    def __init__(self, app: ASGIApp, prefix: str):\n"
                        "        self.app = app\n        self.prefix = prefix.rstrip(\"/\")\n\n"
                        "    async def __call__(self, scope: Scope, receive: Receive, send: Send):\n"
                        "        if scope[\"type\"] == \"http\" and self.prefix and scope[\"path\"].startswith(self.prefix):\n"
                        "            scope = dict(scope)\n            scope[\"path\"] = scope[\"path\"][len(self.prefix):] or \"/\"\n"
                        "        await self.app(scope, receive, send)\n\n\n"
                        "Base.metadata.create_all(bind=engine)\n"
                        f"app = FastAPI(title={app!r}, version=\"1.0.0\")\n\n\n"
                        "@app.get(\"/health\")\ndef health():\n    return {\"status\": \"ok\"}\n\n\n" + inc +
                        "\napp = _StripPrefixMiddleware(app, os.environ.get(\"PATH_PREFIX\", \"\"))\n")
    t = ["import os", "import tempfile", "", "os.environ[\"DATABASE_URL\"] = \"sqlite:///\" + os.path.join(tempfile.mkdtemp(), \"test.db\")", "",
         "from fastapi.testclient import TestClient", "from app.main import app", "", "client = TestClient(app)", "", "",
         "def test_health():", "    assert client.get(\"/health\").json()[\"status\"] == \"ok\""]
    for e in ents:
        payload = "{" + ", ".join(f"{n!r}: {_col(tp)[2]}" for n, tp in e["fields"]) + "}"
        t += ["", "", f"def test_crud_{e['path']}():", f"    p = \"/api/v1/{e['path']}\"", f"    payload = {payload}",
              "    r = client.post(p, json=payload)", "    assert r.status_code == 201, r.text", "    item_id = r.json()[\"id\"]",
              "    assert client.get(p).status_code == 200", "    assert client.get(f\"{p}/{item_id}\").status_code == 200",
              "    assert client.put(f\"{p}/{item_id}\", json=payload).status_code == 200",
              "    assert client.delete(f\"{p}/{item_id}\").status_code == 204",
              "    assert client.get(f\"{p}/{item_id}\").status_code == 404"]
    f["tests/test_api.py"] = "\n".join(t) + "\n"
    return f, ents


async def _sh(cmd, cwd, timeout):
    p = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "timeout"
    return p.returncode, out.decode("utf-8", "replace")


async def run_gate(files, job_id, py=GATE_PY):
    d = f"/tmp/fa-{job_id}"
    if not os.path.exists(py):
        return {"compile": {"status": "unavailable"}, "tests": {"status": "unavailable"}}
    shutil.rmtree(d, ignore_errors=True)
    try:
        for path, content in files.items():
            fp = os.path.join(d, path)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w") as fh:
                fh.write(content)
        rc, out = await _sh([py, "-m", "compileall", "-q", "app", "tests"], d, 120)
        comp = {"status": "ok" if rc == 0 else "fail", "output": out[-800:]}
        if rc != 0:
            return {"compile": comp, "tests": {"status": "skipped"}}
        rc2, out2 = await _sh([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], d, 300)
        ok = re.search(r"(\d+) passed", out2)
        bad = re.search(r"(\d+) failed", out2)
        tests = {"status": "ok" if rc2 == 0 else "fail", "tests": int(ok.group(1)) if ok else 0,
                 "failures": int(bad.group(1)) if bad else 0, "errors": 0, "output": out2[-1500:]}
        return {"compile": comp, "tests": tests}
    finally:
        shutil.rmtree(d, ignore_errors=True)

DESC = "FastAPI + SQLAlchemy + pydantic"
GATE_DESC = "compileall + pytest (isolated venv)"
