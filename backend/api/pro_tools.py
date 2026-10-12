"""
MAXIMUS Pro Tools API
========================
REST endpoints for the three pro features:
  GET  /api/v1/pro/cve-lookup   → CVE correlation via NVD API v2.0
  POST /api/v1/pro/js-scan      → JavaScript secret scanner
  POST /api/v1/pro/scope        → Save scope definition
  GET  /api/v1/pro/scope        → Get current scope (from active assessment workspace)
  POST /api/v1/pro/scope/check  → Validate a target against a given scope
"""

import re
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/pro", tags=["pro-tools"])

# ─── NVD / CVE ───────────────────────────────────────────────────────────────

NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 0}

_nvd_last_call = 0.0  # simple in-process rate limiter


def _nvd_query(keyword: str, severity: str = "", limit: int = 20) -> list:
    global _nvd_last_call
    elapsed = time.monotonic() - _nvd_last_call
    if elapsed < 0.7:
        time.sleep(0.7 - elapsed)

    params: dict = {"keywordSearch": keyword, "resultsPerPage": min(limit * 2, 50)}
    if severity and severity.upper() in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        params["cvssV3Severity"] = severity.upper()

    url = NVD_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "MAXIMUS/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        _nvd_last_call = time.monotonic()
        return data.get("vulnerabilities", [])
    except urllib.error.HTTPError as e:
        if e.code == 403:
            time.sleep(8)
            return _nvd_query(keyword, severity, limit)
        raise HTTPException(status_code=502, detail=f"NVD API error: HTTP {e.code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"NVD unreachable: {e}")


def _parse_cve(vuln: dict, version_hint: str = "") -> dict:
    cve = vuln.get("cve", {})
    descs = cve.get("descriptions", [])
    desc = next((d["value"] for d in descs if d.get("lang") == "en"), "No description")

    metrics = cve.get("metrics", {})
    cvss_score, cvss_severity, cvss_vector = None, "UNKNOWN", ""
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key, [])
        if entries:
            d = entries[0].get("cvssData", {})
            cvss_score = d.get("baseScore")
            cvss_severity = d.get("baseSeverity", entries[0].get("baseSeverity", "UNKNOWN"))
            cvss_vector = d.get("vectorString", "")
            break

    refs = [r["url"] for r in cve.get("references", [])[:3]]
    version_match = bool(version_hint and version_hint in desc)

    return {
        "id": cve.get("id", "UNKNOWN"),
        "published": cve.get("published", "")[:10],
        "description": desc[:400],
        "cvss_score": cvss_score,
        "cvss_severity": cvss_severity,
        "cvss_vector": cvss_vector,
        "references": refs,
        "version_match": version_match,
    }


class CveLookupResponse(BaseModel):
    technology: str
    version: str
    query: str
    total: int
    critical: int
    high: int
    cves: list


@router.get("/cve-lookup", response_model=CveLookupResponse)
async def cve_lookup(
    tech: str = Query(..., description="Technology name"),
    version: str = Query("", description="Version string"),
    severity: str = Query("", description="Min severity: CRITICAL/HIGH/MEDIUM/LOW"),
    limit: int = Query(15, ge=1, le=50),
):
    """Query NVD for CVEs matching a technology + version."""
    query = f"{tech} {version}".strip()
    raw = _nvd_query(query, severity, limit)
    cves = [_parse_cve(v, version) for v in raw]

    # Multi-severity filter (client may pass comma list, e.g. "CRITICAL,HIGH")
    if severity:
        allowed = {s.strip().upper() for s in severity.split(",") if s.strip()}
        cves = [c for c in cves if c["cvss_severity"].upper() in allowed]

    cves.sort(key=lambda x: (x["version_match"], x.get("cvss_score") or 0), reverse=True)
    cves = cves[:limit]

    return CveLookupResponse(
        technology=tech,
        version=version,
        query=query,
        total=len(cves),
        critical=sum(1 for c in cves if c["cvss_severity"] == "CRITICAL"),
        high=sum(1 for c in cves if c["cvss_severity"] == "HIGH"),
        cves=cves,
    )


# ─── JS Scanner ───────────────────────────────────────────────────────────────

JS_PATTERNS = [
    ("AWS Access Key ID",       r'AKIA[0-9A-Z]{16}',                                           "CRITICAL"),
    ("AWS Secret Access Key",   r'(?i)aws.{0,20}secret.{0,20}["\']([A-Za-z0-9/+]{40})["\']',  "CRITICAL"),
    ("Google API Key",          r'AIza[0-9A-Za-z\-_]{35}',                                     "CRITICAL"),
    ("GitHub PAT (classic)",    r'ghp_[0-9a-zA-Z]{36}',                                        "CRITICAL"),
    ("GitHub PAT (fine-grain)", r'github_pat_[0-9a-zA-Z_]{82}',                                "CRITICAL"),
    ("Stripe Live Secret Key",  r'sk_live_[0-9a-zA-Z]{24,}',                                   "CRITICAL"),
    ("RSA Private Key",         r'-----BEGIN (?:RSA )?PRIVATE KEY-----',                       "CRITICAL"),
    ("OpenSSH Private Key",     r'-----BEGIN OPENSSH PRIVATE KEY-----',                         "CRITICAL"),
    ("MongoDB URI",             r'mongodb(?:\+srv)?://[^"\'@\s]+:[^"\'@\s]+@[^"\'@\s]+',       "CRITICAL"),
    ("PostgreSQL URI",          r'postgres(?:ql)?://[^"\'@\s]+:[^"\'@\s]+@[^"\'@\s]+',        "CRITICAL"),
    ("Slack Bot Token",         r'xoxb-[0-9]{11}-[0-9]{11}-[0-9a-zA-Z]{24}',                  "CRITICAL"),
    ("GitHub OAuth Token",      r'gho_[0-9a-zA-Z]{36}',                                        "HIGH"),
    ("Slack Webhook",           r'https://hooks\.slack\.com/services/T[0-9A-Z]+/B[0-9A-Z]+/[0-9a-zA-Z]+', "HIGH"),
    ("SendGrid API Key",        r'SG\.[a-zA-Z0-9_-]{22}\.[a-zA-Z0-9_-]{43}',                  "HIGH"),
    ("Generic API Key",         r'(?i)(?:api[_-]?key|apikey)\s*[=:]\s*["\']([a-zA-Z0-9_\-]{20,})["\']', "HIGH"),
    ("Bearer Token",            r'(?i)Bearer\s+([a-zA-Z0-9\-._~+/]+=*)',                                  "HIGH"),
    ("JWT Token",               r'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}',       "HIGH"),
    ("Hardcoded Password",      r'(?i)(?:password|passwd|pwd)\s*[=:]\s*["\']([^"\']{4,})["\']',           "HIGH"),
    ("Internal IP",             r'https?://(?:10\.|172\.(?:1[6-9]|2[0-9]|3[01])\.|192\.168\.)[0-9.]+(?:[:/][^\s"\'<>]*)?', "MEDIUM"),
    ("Admin Panel Path",        r'(?i)["\'](?:/admin|/administrator|/wp-admin|/dashboard|/manage|/console|/portal)[/a-zA-Z0-9_\-?=&]*["\']', "MEDIUM"),
    ("Internal Hostname",       r'https?://(?:[a-z0-9-]+\.)*(?:internal|intranet|corp|local|staging|dev|test)\.[a-z]{2,}[/a-zA-Z0-9_\-?=&]*', "MEDIUM"),
    ("Source Map Reference",    r'//[#@]\s*sourceMappingURL=(.+\.map)',                         "LOW"),
    ("Hardcoded Username",      r'(?i)username\s*[=:]\s*["\']([^"\']+)["\']',                  "LOW"),
]

SRANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _mask(val: str) -> str:
    return val[:4] + "..." + val[-4:] if len(val) > 8 else "***"


def _scan_content(content: str, filename: str = "remote.js", min_severity: str = "LOW") -> list:
    findings = []
    lines = content.splitlines()
    seen: set = set()
    min_rank = SRANK.get(min_severity.upper(), 1)

    for label, pattern, severity in JS_PATTERNS:
        if SRANK.get(severity, 0) < min_rank:
            continue
        try:
            for m in re.finditer(pattern, content, re.MULTILINE):
                line_no = content[: m.start()].count("\n") + 1
                line_text = (lines[line_no - 1].strip() if line_no <= len(lines) else "")[:120]
                val = m.group(1) if m.lastindex else m.group(0)
                key = (label, _mask(val))
                if key in seen:
                    continue
                seen.add(key)
                findings.append({
                    "file": filename,
                    "line": line_no,
                    "severity": severity,
                    "type": label,
                    "match": _mask(val),
                    "context": line_text,
                })
        except re.error:
            continue

    findings.sort(key=lambda x: SRANK.get(x["severity"], 0), reverse=True)
    return findings


class JsScanRequest(BaseModel):
    url: Optional[str] = None
    content: Optional[str] = None
    min_severity: str = "HIGH"


class JsScanResponse(BaseModel):
    source: str
    total: int
    critical: int
    high: int
    medium: int
    findings: list


@router.post("/js-scan", response_model=JsScanResponse)
async def js_scan(body: JsScanRequest):
    """Scan JavaScript (by URL or raw content) for secrets."""
    if body.url:
        req = urllib.request.Request(body.url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to fetch URL: {e}")
        filename = body.url.split("/")[-1].split("?")[0] or "remote.js"
    elif body.content:
        content = body.content
        filename = "pasted.js"
    else:
        raise HTTPException(status_code=400, detail="Provide 'url' or 'content'")

    findings = _scan_content(content, filename, body.min_severity)
    return JsScanResponse(
        source=body.url or "pasted content",
        total=len(findings),
        critical=sum(1 for f in findings if f["severity"] == "CRITICAL"),
        high=sum(1 for f in findings if f["severity"] == "HIGH"),
        medium=sum(1 for f in findings if f["severity"] == "MEDIUM"),
        findings=findings,
    )


# ─── Scope Manager ────────────────────────────────────────────────────────────

_active_scope: dict = {"in_scope": [], "out_of_scope": [], "notes": ""}


def _ip_to_int(ip: str) -> int:
    try:
        parts = [int(x) for x in ip.split(".")]
        return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
    except Exception:
        return -1


def _ip_in_cidr(ip: str, cidr: str) -> bool:
    try:
        network, prefix = cidr.split("/")
        prefix = int(prefix)
        mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
        return (_ip_to_int(ip) & mask) == (_ip_to_int(network) & mask)
    except Exception:
        return False


def _is_ip(s: str) -> bool:
    return bool(re.match(r'^(\d{1,3}\.){3}\d{1,3}$', s))


def _is_cidr(s: str) -> bool:
    return bool(re.match(r'^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$', s))


def _extract_host(target: str) -> str:
    from urllib.parse import urlparse
    target = target.strip()
    if target.startswith(("http://", "https://", "ftp://")):
        return urlparse(target).hostname or ""
    if ":" in target and not target.startswith("["):
        return target.split(":")[0]
    return target


def _matches_entry(host: str, entry: str) -> bool:
    entry = entry.strip().lower()
    host = host.strip().lower()
    if _is_cidr(entry) and _is_ip(host):
        return _ip_in_cidr(host, entry)
    if _is_ip(entry) and _is_ip(host):
        return host == entry
    if entry.startswith("*."):
        suffix = entry[2:]
        return host == suffix or host.endswith("." + suffix)
    return host == entry or host.endswith("." + entry)


class ScopeDefinition(BaseModel):
    in_scope: list[str]
    out_of_scope: list[str] = []
    notes: str = ""


class ScopeCheckRequest(BaseModel):
    target: str
    scope: Optional[ScopeDefinition] = None


class ScopeCheckResponse(BaseModel):
    target: str
    host: str
    status: str   # IN_SCOPE | OUT_OF_SCOPE | NO_SCOPE_DEFINED
    reason: str
    notes: str


@router.post("/scope", status_code=200)
async def save_scope(body: ScopeDefinition):
    """Save the active scope definition (in-memory; also used by scope check)."""
    global _active_scope
    _active_scope = body.dict()
    return {"ok": True, "in_scope_count": len(body.in_scope)}


@router.get("/scope")
async def get_scope():
    """Get the current active scope."""
    return _active_scope


@router.post("/scope/check", response_model=ScopeCheckResponse)
async def check_scope(body: ScopeCheckRequest):
    """Validate a target against the given (or active) scope."""
    scope = body.scope.dict() if body.scope else _active_scope
    if not scope or not scope.get("in_scope"):
        return ScopeCheckResponse(
            target=body.target,
            host=_extract_host(body.target),
            status="NO_SCOPE_DEFINED",
            reason="No scope defined — testing blocked until scope is set",
            notes="",
        )

    host = _extract_host(body.target)
    for entry in scope.get("out_of_scope", []):
        if _matches_entry(host, entry):
            return ScopeCheckResponse(
                target=body.target, host=host,
                status="OUT_OF_SCOPE",
                reason=f"{host!r} matches out-of-scope entry: {entry!r}",
                notes=scope.get("notes", ""),
            )
    for entry in scope.get("in_scope", []):
        if _matches_entry(host, entry):
            return ScopeCheckResponse(
                target=body.target, host=host,
                status="IN_SCOPE",
                reason=f"{host!r} matches in-scope entry: {entry!r}",
                notes=scope.get("notes", ""),
            )
    return ScopeCheckResponse(
        target=body.target, host=host,
        status="OUT_OF_SCOPE",
        reason=f"{host!r} does not match any in-scope entry (default deny)",
        notes=scope.get("notes", ""),
    )
