"""Boot-and-smoke gate: build the generated app, boot it against a throwaway
Postgres database, and exercise every entity endpoint like a real client.
Returns which entities are bad so the generator can drop them (never ships an
app that does not start)."""
import asyncio
import glob
import logging
import os
import re
import shutil
import socket
import uuid
from urllib.parse import urlsplit

logger = logging.getLogger("tasks.bootcheck")

_SERVER_MANAGED = {"id", "tenantId", "createdAt", "updatedAt"}
_FIELD = re.compile(r"(?:@Column\s*\(([^)]*)\)\s*)?(?:@[\w.]+(?:\([^)]*\))?\s+)*private\s+([A-Za-z][A-Za-z0-9_.]*)\s+([a-z][A-Za-z0-9]*)\s*(?:=[^;]*)?;")


def sample_payload(code):
    """Build a JSON body from the entity's own fields, the way a client would:
    server-managed fields are left out."""
    body = {}
    enums = {m.group(1): m.group(2).split(",")[0].strip().split("(")[0]
             for m in re.finditer(r"enum\s+(\w+)\s*\{([^}]*)\}", code)}
    for m in _FIELD.finditer(code):
        attrs, jtype, fname = m.group(1) or "", m.group(2).split(".")[-1], m.group(3)
        if fname in _SERVER_MANAGED:
            continue
        ln = re.search(r"length\s*=\s*(\d+)", attrs)
        if jtype == "String":
            body[fname] = "x" * max(1, min(int(ln.group(1)) if ln else 5, 5))
        elif jtype == "UUID":
            body[fname] = "11111111-1111-1111-1111-111111111111"
        elif jtype in ("Boolean", "boolean"):
            body[fname] = True
        elif jtype in ("Integer", "Long", "int", "long", "Short"):
            body[fname] = 1
        elif jtype in ("BigDecimal", "Double", "Float", "double", "float"):
            body[fname] = 1
        elif jtype == "LocalDate":
            body[fname] = "2026-01-01"
        elif jtype == "LocalDateTime":
            body[fname] = "2026-01-01T00:00:00"
        elif jtype == "Instant":
            body[fname] = "2026-01-01T00:00:00Z"
        elif jtype in enums:
            body[fname] = enums[jtype]
    return body


def scan_entities(files):
    """Map entity name -> {path, table, code} from the generated file set."""
    out = {}
    for key, code in files.items():
        m = re.search(r"/entity/([A-Za-z0-9_]+)\.java$", key)
        if not m:
            continue
        name = m.group(1)
        ctrl = next((v for k, v in files.items() if k.endswith(f"/controller/{name}Controller.java")), "")
        pm = re.search(r'@RequestMapping\("([^"]+)"\)', ctrl)
        tm = re.search(r'@Table\s*\(\s*name\s*=\s*"([^"]+)"', code)
        out[name] = {"path": pm.group(1) if pm else None, "table": tm.group(1) if tm else None, "code": code}
    return out


def parse_failure(log_text, entities):
    """Which entities does a failed startup point at?"""
    bad = set()
    by_table = {e["table"]: n for n, e in entities.items() if e["table"]}
    for t in re.findall(r"missing table \[([^\]]+)\]", log_text):
        if t in by_table:
            bad.add(by_table[t])
    for n in re.findall(r"for type '([A-Za-z0-9_]+)'", log_text):
        if n in entities:
            bad.add(n)
    for n in re.findall(r"\.([A-Za-z0-9_]+)Repository\.", log_text):
        if n in entities:
            bad.add(n)
    for n in re.findall(r"missing column \[([^\]]+)\] in table \[([^\]]+)\]", log_text):
        if n[1] in by_table:
            bad.add(by_table[n[1]])
    return bad


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _run(cmd, cwd, timeout):
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None, ""
    return proc.returncode, out.decode("utf-8", "replace")


async def run_boot_check(files, job_id, startup_timeout=120):
    import asyncpg
    import httpx

    entities = scan_entities(files)
    res = {"status": "ok", "bad": set(), "post_ok": 0, "total": len(entities), "warn": []}
    base = os.environ.get("GATE_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if not base:
        return {**res, "status": "unavailable"}
    u = urlsplit(base.replace("+asyncpg", ""))
    dbname = "gate_" + re.sub(r"[^a-z0-9]", "", job_id.lower())[:20]
    d = f"/tmp/boot-{job_id}"
    proc = None
    admin = None
    try:
        try:
            admin = await asyncpg.connect(f"postgresql://{u.username}:{u.password}@{u.hostname}:{u.port or 5432}{u.path}", timeout=15)
            await admin.execute(f'CREATE DATABASE "{dbname}"')
        except Exception as e:
            logger.error(f"boot check unavailable (cannot create database): {e}")
            return {**res, "status": "unavailable"}

        shutil.rmtree(d, ignore_errors=True)
        for path, content in files.items():
            fp = os.path.join(d, path)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w") as f:
                f.write(content)
        rc, text = await _run(["mvn", "-q", "-B", "-DskipTests", "-Dmaven.repo.local=/app/.m2", "package"], d, 900)
        jars = [j for j in glob.glob(f"{d}/target/*.jar") if not j.endswith(".original")]
        if rc != 0 or not jars:
            logger.error(f"boot check build failed: {text[-800:]}")
            return {**res, "status": "unavailable"}

        port = _free_port()
        env = {**os.environ,
               "SPRING_DATASOURCE_URL": f"jdbc:postgresql://{u.hostname}:{u.port or 5432}/{dbname}",
               "SPRING_DATASOURCE_USERNAME": u.username or "", "SPRING_DATASOURCE_PASSWORD": u.password or "",
               "SERVER_PORT": str(port)}
        proc = await asyncio.create_subprocess_exec("java", "-Xmx384m", "-jar", jars[0], cwd=d, env=env,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        lines = []

        async def _pump():
            async for raw in proc.stdout:
                lines.append(raw.decode("utf-8", "replace"))
        pump = asyncio.create_task(_pump())
        started = False
        for _ in range(startup_timeout):
            if any("Started " in l and "seconds" in l for l in lines):
                started = True
                break
            if proc.returncode is not None:
                break
            await asyncio.sleep(1)
        if not started:
            try:
                await asyncio.wait_for(asyncio.shield(pump), timeout=5)
            except Exception:
                pass
        log_text = "".join(lines)
        if not started:
            res["status"] = "fail"
            res["bad"] = parse_failure(log_text, entities)
            causes = " || ".join(l.strip()[:300] for l in lines if any(k in l for k in ("Caused by", "Description:", "Schema-validation", "Unable to", "APPLICATION FAILED")))[:2500]
            logger.error(f"boot check: app did not start; bad={res['bad']}; causes: {causes}; tail: {log_text[-900:]}")
            return res

        async with httpx.AsyncClient(timeout=20) as c:
            for name, e in entities.items():
                if not e["path"]:
                    continue
                try:
                    g = await c.get(f"http://127.0.0.1:{port}{e['path']}")
                    p = await c.post(f"http://127.0.0.1:{port}{e['path']}", json=sample_payload(e["code"]))
                except Exception as ex:
                    res["bad"].add(name)
                    res["warn"].append(f"{name}: request failed {ex}")
                    continue
                if g.status_code >= 500 or p.status_code >= 500:
                    res["bad"].add(name)
                    res["warn"].append(f"{name}: GET {g.status_code} POST {p.status_code}")
                elif p.status_code in (200, 201):
                    res["post_ok"] += 1
                else:
                    res["warn"].append(f"{name}: POST {p.status_code}")
        if res["bad"]:
            res["status"] = "fail"
        return res
    finally:
        if proc and proc.returncode is None:
            proc.kill()
        try:
            if admin:
                await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
                await admin.close()
        except Exception:
            pass
        shutil.rmtree(d, ignore_errors=True)
