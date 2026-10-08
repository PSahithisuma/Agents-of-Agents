"""Security scan v1 (non-blocking): gitleaks (secrets), Semgrep (code), CycloneDX SBOM (dependencies).
Never raises into the job: every tool failure becomes a line in the report and a short note."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile

logger = logging.getLogger("tasks.generation")
_T = int(os.getenv("SECURITY_SCAN_TIMEOUT", "240"))


async def _run(cmd, cwd, timeout=_T):
    p = None
    try:
        p = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        try:
            p.kill()
        except Exception:
            pass
        return 124, "", "timeout"
    except Exception as e:
        return 127, "", str(e)


def parse_gitleaks(text):
    try:
        data = json.loads(text) if text.strip() else []
    except Exception:
        return None
    return data if isinstance(data, list) else None


def parse_semgrep(text):
    try:
        res = json.loads(text).get("results", [])
    except Exception:
        return None
    out = []
    for r in res:
        ex = r.get("extra") or {}
        out.append({"rule": r.get("check_id", "?"), "sev": ex.get("severity", "INFO"), "path": r.get("path", ""),
                    "line": (r.get("start") or {}).get("line", 0), "msg": (ex.get("message") or "")[:200]})
    return out


def parse_trivy(text):
    try:
        res = json.loads(text).get("Results") or []
    except Exception:
        return None
    out = []
    for r in res:
        for v in r.get("Vulnerabilities") or []:
            out.append({"id": v.get("VulnerabilityID", "?"), "sev": v.get("Severity", "UNKNOWN"),
                        "pkg": v.get("PkgName", "?"), "ver": v.get("InstalledVersion", "?"),
                        "fix": v.get("FixedVersion") or "none yet"})
    return out


def _sev_counts(tv):
    c = sum(1 for v in tv if v["sev"] == "CRITICAL")
    return c, len(tv) - c


def build_report(job_id, gl, sg, comps, notes, tv=None):
    L = ["# Security report", "", f"Job {job_id[:8]} - automatic scan, informational only (not a certification).", "",
         "| Check | Tool | Result |", "| --- | --- | --- |"]
    L.append("| Secrets | gitleaks | " + ("unavailable" if gl is None else f"{len(gl)} finding(s)") + " |")
    L.append("| Code issues | Semgrep (p/java) | " + ("unavailable" if sg is None else f"{len(sg)} finding(s)") + " |")
    L.append("| Dependencies (SBOM) | CycloneDX | " + ("unavailable" if comps is None else f"{comps} component(s), see sbom.json") + " |")
    if tv is None:
        L.append("| Dependency vulnerabilities | Trivy | unavailable |")
    else:
        _c, _h = _sev_counts(tv)
        L.append(f"| Dependency vulnerabilities | Trivy (high/critical) | {_c} critical, {_h} high |")
    if gl:
        L += ["", "## Secrets", ""] + [f"- {g.get('File', '?')}:{g.get('StartLine', '?')} {g.get('RuleID', '?')}" for g in gl[:50]]
    if sg:
        L += ["", "## Code issues", ""] + [f"- [{s['sev']}] {s['path']}:{s['line']} {s['rule']} - {s['msg']}" for s in sg[:50]]
    if tv:
        L += ["", "## Dependency vulnerabilities (high/critical)", ""]
        L += [f"- [{v['sev']}] {v['id']} {v['pkg']} {v['ver']} (fixed in: {v['fix']})" for v in tv[:50]]
    if notes:
        L += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    return "\n".join(L) + "\n"


async def run_security_scan(files, job_id):
    """Returns (extra_files, note). Scans a temp copy of the generated files."""
    work = tempfile.mkdtemp(prefix=f"sec-{job_id[:8]}-")
    gl = sg = comps = tv = None
    sbom = None
    notes = []
    try:
        for path, content in files.items():
            full = os.path.join(work, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(content)
        if shutil.which("gitleaks"):
            rep = work + ".gitleaks.json"
            rc, out, err = await _run(["gitleaks", "detect", "--no-git", "--source", ".", "--report-format", "json",
                                       "--report-path", rep, "--exit-code", "0", "--no-banner"], work)
            if rc == 0:
                gl = parse_gitleaks(open(rep).read() if os.path.exists(rep) else "")
            if gl is None:
                notes.append(f"gitleaks did not finish (exit {rc})")
        else:
            notes.append("gitleaks is not installed")
        if shutil.which("semgrep"):
            rc, out, err = await _run(["semgrep", "--config", "p/java", "--json", "--quiet", "--metrics=off",
                                       "--timeout", "30", "."], work)
            sg = parse_semgrep(out) if rc in (0, 1) else None
            if sg is None:
                notes.append(f"semgrep did not finish (exit {rc}: {err[-120:].strip()})")
        else:
            notes.append("semgrep is not installed")
        if shutil.which("mvn"):
            rc, out, err = await _run(["mvn", "-q", "-B", "-DskipTests", "org.cyclonedx:cyclonedx-maven-plugin:2.9.1:makeAggregateBom",
                                       "-DoutputFormat=json", "-DoutputName=bom"], work, timeout=300)
            bom = os.path.join(work, "target", "bom.json")
            if os.path.exists(bom):
                sbom = open(bom).read()
                try:
                    comps = len(json.loads(sbom).get("components", []))
                except Exception:
                    comps = None
            if comps is None:
                notes.append(f"SBOM was not produced (exit {rc})")
        else:
            notes.append("maven is not installed")
        if sbom and shutil.which("trivy"):
            rc, out, err = await _run(["trivy", "sbom", "--quiet", "--format", "json", "--severity", "HIGH,CRITICAL",
                                       "--cache-dir", os.getenv("TRIVY_CACHE_DIR", "/opt/trivy-cache"),
                                       "--timeout", "200s", os.path.join(work, "target", "bom.json")], work, timeout=260)
            tv = parse_trivy(out) if rc == 0 else None
            if tv is None:
                notes.append(f"trivy did not finish (exit {rc}: {err[-120:].strip()})")
        elif sbom:
            notes.append("trivy is not installed")
    except Exception as e:
        notes.append(f"scan error: {e}")
        logger.error(f"security scan error: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            os.remove(work + ".gitleaks.json")
        except Exception:
            pass
    extra = {"SECURITY_REPORT.md": build_report(job_id, gl, sg, comps, notes, tv)}
    if sbom:
        extra["sbom.json"] = sbom
    fmt = lambda v, w: "n/a" if v is None else str(w)
    note = (f"; security scan: secrets {fmt(gl, len(gl or []))}, code findings {fmt(sg, len(sg or []))}, "
            f"SBOM {'ok' if sbom else 'n/a'}, vulnerabilities "
            + ("n/a" if tv is None else "critical %d, high %d" % _sev_counts(tv)))
    return extra, note
