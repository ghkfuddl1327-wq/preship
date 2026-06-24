"""Scan engine: URL -> fetch /openapi.json -> run schemathesis -> return list[Finding].

This module knows nothing about the CLI / output formatting. It returns structured Finding
objects only (cli.py does the formatting).

Integration: it runs the scope command locked and validated in S1
    schemathesis run <URL>/openapi.json --phases examples,fuzzing --mode positive --seed 0
**verbatim** as a subprocess via the schemathesis console script in the same venv (behavior parity).
Results come back as an NDJSON report (--report ndjson) and we parse the ScenarioFinished events.

Limitation (fresh state): the scanner calls the target over HTTP, so it cannot stop the target
from mutating its own state in response to requests (e.g. a POST handler writing to an in-memory
store). For reproducible results, assume a **clean / disposable staging instance per scan**.
Repeatedly scanning a target that accumulates state may make results flaky.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ohs_preflight.verify import WELL_KNOWN_PATH

# Matches the locked command in S1 §Appendix B exactly (--checks unset → 4.x default = all).
SCOPE_FLAGS = ["--phases", "examples,fuzzing", "--mode", "positive", "--seed", "0"]

# A (signal refinement): noisy checks are excluded from the report after collection (detection
# unchanged — filter layer only). positive_data_acceptance = a false positive where the app
# correctly rejected bad input but it's flagged as "rejected valid input".
EXCLUDED_CHECKS = frozenset({"positive_data_acceptance"})

# A (severity): 500s/crashes are high; undocumented-contract / schema violations are low.
_HIGH_CHECKS = frozenset({"not_a_server_error"})


def _level(check: str) -> str:
    return "high" if check in _HIGH_CHECKS else "low"


@dataclass
class Finding:
    """One defect = (endpoint, failing check). The S1_spec §5 matrix is the classification basis."""

    method: str
    path: str
    check: str
    trigger: str          # the input (request) that fired the defect
    response_summary: str  # response status + content-type + body summary
    severity: str = ""     # schemathesis failure type (extra info)
    level: str = "low"     # A: high (500/crash) | low (undocumented-contract / schema violation)
    status: int = 0        # raw response status_code, used to reclassify documented 5xx


class ScanError(RuntimeError):
    """The scan itself could not run (target unreachable, schemathesis missing, etc.)."""


def _reclassify_documented_5xx(findings, spec):
    """Documented 5xx in not_a_server_error -> downgrade to a separate low check.
    spec is the OpenAPI dict. If spec is falsy, return findings unchanged (safe default)."""
    import dataclasses
    if not spec:
        return findings
    paths = spec.get("paths", {}) if isinstance(spec, dict) else {}
    out = []
    for f in findings:
        if f.check == "not_a_server_error" and 500 <= f.status <= 599:
            op = (paths.get(f.path, {}) or {}).get(f.method.lower(), {}) or {}
            responses = op.get("responses", {}) or {}
            keys = {str(k).upper() for k in responses.keys()}
            documented = (str(f.status) in keys) or ("5XX" in keys) or ("DEFAULT" in keys)
            if documented:
                out.append(dataclasses.replace(
                    f, check="documented_5xx_response", level="low"))
                continue
        out.append(f)
    return out


def scan(url: str, headers: list[str] | None = None) -> list[Finding]:
    """Scan `url` (a FastAPI base URL) and return the defects as list[Finding].

    `headers`: a list of "Name: Value" strings. Sent on every request (passed via --header).
    """
    exe = _find_schemathesis()
    if exe is None:
        raise ScanError("Could not find the schemathesis executable (it must be installed in the same venv).")

    with tempfile.TemporaryDirectory(prefix="ohs-preflight-") as tmp:
        ndjson_path = Path(tmp) / "events.ndjson"
        cmd = [
            exe, "run", _schema_url(url), *SCOPE_FLAGS,
            "--report", "ndjson", "--report-dir", tmp,
            "--report-ndjson-path", str(ndjson_path),
        ]
        for h in headers or []:
            cmd += ["--header", h]

        # cwd=tmp: confine the .schemathesis/ cache schemathesis creates in cwd to the temp dir
        # (M11, auto-cleaned). URL and report paths are all absolute, so behavior is unchanged.
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp)
        # schemathesis exits non-zero when it finds defects (that's normal). Only a missing report is an error.
        if not ndjson_path.exists():
            raise ScanError(
                "schemathesis did not produce a report.\n"
                f"exit={proc.returncode}\nstderr:\n{(proc.stderr or '')[-2000:]}"
            )
        findings = _parse(ndjson_path)
        # M10: exclude the verification well-known path (avoid self-reference). A(a): exclude
        # false-positive checks like positive_data_acceptance. Both are 'filter layers' — detection itself is unchanged.
        filtered = [
            f for f in findings
            if f.path != WELL_KNOWN_PATH and f.check not in EXCLUDED_CHECKS
        ]
        try:
            spec = fetch_openapi(url, headers)
        except Exception:
            spec = None
        return _reclassify_documented_5xx(filtered, spec)


def _find_schemathesis() -> str | None:
    """Resolve the schemathesis console script in the same venv (current interpreter), independent of PATH.

    Calling `preship` by full path may mean venv/bin is not on PATH, so we look at the sibling
    of sys.executable first and fall back to PATH (shutil.which).
    """
    sibling = Path(sys.executable).parent / "schemathesis"
    if sibling.exists():
        return str(sibling)
    return shutil.which("schemathesis")


def _schema_url(url: str) -> str:
    u = url.rstrip("/")
    return u if u.endswith("/openapi.json") else u + "/openapi.json"


def _parse_headers(headers: list[str] | None) -> list[tuple[str, str]]:
    """Parse a list of "Name: Value" strings into (name, value) tuples."""
    out: list[tuple[str, str]] = []
    for h in headers or []:
        name, sep, value = h.partition(":")
        if sep:
            out.append((name.strip(), value.strip()))
    return out


def fetch_openapi(url: str, headers: list[str] | None = None) -> dict:
    """Fetch the target's /openapi.json and return it as a dict (public contract — for S3 patch diagnosis).

    Headers are applied to every request, same as scan (--header).
    """
    request = urllib.request.Request(_schema_url(url))
    for name, value in _parse_headers(headers):
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (user-supplied URL)
        return json.loads(response.read().decode("utf-8"))


def _parse(ndjson_path: Path) -> list[Finding]:
    """Extract failing checks from the NDJSON ScenarioFinished events, dedup by (method, path, check)."""
    seen: set[tuple[str, str, str]] = set()
    findings: list[Finding] = []

    for line in ndjson_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        scenario = event.get("ScenarioFinished")
        if not scenario:
            continue

        recorder = scenario.get("recorder", {})
        cases = recorder.get("cases", {})
        checks = recorder.get("checks", {})
        interactions = recorder.get("interactions", {})

        for case_id, check_list in checks.items():
            failed = [c for c in check_list if c.get("status") == "failure"]
            if not failed:
                continue

            case = cases.get(case_id, {}).get("value", {})
            method = case.get("method", "?")
            path = case.get("path", "?")
            interaction = interactions.get(case_id, {})
            trigger = _trigger(method, path, case, interaction.get("request", {}))
            response_summary = _response_summary(interaction.get("response", {}))
            try:
                status_code = int(interaction.get("response", {}).get("status_code", 0) or 0)
            except (TypeError, ValueError):
                status_code = 0

            for c in failed:
                name = c.get("name", "?")
                key = (method, path, name)
                if key in seen:
                    continue
                seen.add(key)
                failure = (c.get("failure_info") or {}).get("failure", {})
                findings.append(Finding(
                    method=method,
                    path=path,
                    check=name,
                    trigger=trigger,
                    response_summary=response_summary,
                    severity=failure.get("type", ""),
                    level=_level(name),
                    status=status_code,
                ))

    findings.sort(key=lambda f: (f.path, f.method, f.check))
    return findings


def _decode(blob) -> str:
    """Render the NDJSON body representation ({"$base64": ...} or a plain value) as a string."""
    if isinstance(blob, dict) and "$base64" in blob:
        try:
            return base64.b64decode(blob["$base64"]).decode("utf-8", "replace")
        except Exception:
            return str(blob["$base64"])
    if blob is None:
        return ""
    return blob if isinstance(blob, str) else json.dumps(blob, ensure_ascii=False)


def _trigger(method: str, path: str, case: dict, request: dict) -> str:
    uri = request.get("uri") or path  # uri includes the query string (useful for identifying GET triggers)
    body = case.get("body")
    body_str = json.dumps(body, ensure_ascii=False) if body is not None else _decode(request.get("body"))
    out = f"{method} {uri}"
    if body_str:
        out += f"  body={body_str}"
    return out


def _response_summary(response: dict) -> str:
    if not response:
        return "(no response captured)"
    status = response.get("status_code", "?")
    content_type = ""
    for key, value in (response.get("headers") or {}).items():
        if key.lower() == "content-type":
            content_type = value[0] if isinstance(value, list) else value
            break
    body = _decode(response.get("content")).replace("\n", " ")[:120]
    return f"{status} {content_type}; {body}".strip()
