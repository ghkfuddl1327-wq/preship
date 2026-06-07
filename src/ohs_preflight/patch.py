"""AI patch translation (S3) — core submodule.

Approach (a): **without source code**, send only diagnostic info (request / observed response /
declared public OpenAPI contract) to the LLM and generate "explanation + cause + FastAPI patch"
per defect. cli only formats the result.

BYOK: the key is read only from the `ANTHROPIC_API_KEY` env var (the anthropic SDK loads it
automatically). The key is never printed to code, logs, or errors. If there's no key, cli skips the
patch step and scan still works as usual.

Excluded from the payload (internal info the user can't see): source code, schemathesis check names,
`[det]` example scaffolding.
"""
from __future__ import annotations

import os
import re
from collections import OrderedDict
from dataclasses import dataclass

from ohs_preflight.core import Finding, fetch_openapi

DEFAULT_MODEL = "claude-opus-4-8"  # overridable via the ANTHROPIC_MODEL env var

_SYSTEM = (
    "You are a senior FastAPI engineer. You receive only the diagnostic info an automated API "
    "probe collected (the request, the observed response, the declared OpenAPI contract) — never "
    "the source code. Your output is an 'application guide' to apply to the user's existing "
    "handler, not freshly written code files.\n"
    "Rules:\n"
    "- Do not rewrite whole files/apps. Do not invent the user's code — never imagine variable "
    "names, fields, handler bodies, imports, storage, or business logic that aren't in the "
    "diagnostic info. Refer to unknown parts with placeholders like <the user's handler> / "
    "<the user's model> and leave them as-is.\n"
    "- However, **name the specific FastAPI symbols** used in the fix precisely: e.g. Path(..., ge=1), "
    "Query(ge=1, le=100), Field(gt=0), HTTPException(status_code=404, ...), response_model=..., "
    "returning a model instance. Do not stop at abstract instructions like 'add validation'.\n"
    "- **Document status codes (required)**: if a handler raises/returns a new status code "
    "(404, 409, 400, etc.), always include a step to document it on the same route decorator via "
    "responses={code: {\"description\": ...}}. Omitting this fails re-check as non-conformance.\n"
    "- **Minimal footprint**: only the change strictly needed to fix the defect. Do not add things "
    "unrelated to the defect (new validation fields, EmailStr, logging, an unnecessary new "
    "response_model). Put hardening not strictly required for the fix under '## Optional' only, and mark it optional.\n"
    "- Prefer framework-standard idioms — delegating type validation, Pydantic models, "
    "HTTPException (with a documented status code), response_model conformance — over a stopgap "
    "that hides the 500 with try/except.\n"
    "- Write prose in English; keep code symbols/snippets in runnable form. Output exactly the four "
    "sections '## Explanation', '## Cause', '## Steps', '## Optional' (if '## Optional' has no content, write \"None\")."
)

_TASK = (
    "\n\n[Task]\nReason without source code — using only the request, observed response, and "
    "declared contract above. Answer in exactly these four sections:\n"
    "## Explanation\n(the defect in plain prose, 2-4 sentences)\n"
    "## Cause\n(the likely root cause)\n"
    "## Steps\n(numbered, line-level instructions to apply to the user's existing handler/model. "
    "Name the specific FastAPI symbols used in each step. If you introduce a new status code, you "
    "must include a responses={...} documentation step. Do not rewrite whole files — only the "
    "changing signature / one-or-two-line snippets.)\n"
    "## Optional\n(only hardening not strictly required for the fix, each item prefixed with "
    "\"Optional:\". If none, write \"None\".)"
)


@dataclass
class Diagnosis:
    """The LLM output for one defect (per endpoint). cli only formats it."""

    method: str
    path: str
    explanation: str = ""
    cause: str = ""
    steps: str = ""     # Steps (line-level instructions to apply to the user's handler)
    optional: str = ""  # Optional (non-essential hardening)
    error: str = ""     # reason this endpoint's patch generation failed, if any (no key leakage)


def has_api_key() -> bool:
    """Only checks whether ANTHROPIC_API_KEY is in the environment (never handles the value)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def model_name() -> str:
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)


def generate_patches(findings: list[Finding], url: str, headers: list[str] | None = None) -> list[Diagnosis]:
    """Group defects by endpoint and generate one patch per endpoint (6 endpoints → 6 patches).

    Before calling, cli guarantees a key exists via has_api_key().
    """
    import anthropic  # lazy: so the scan path doesn't depend on importing anthropic

    groups: "OrderedDict[tuple[str, str], list[Finding]]" = OrderedDict()
    for f in findings:
        groups.setdefault((f.method, f.path), []).append(f)

    # public contract (not source). If it can't be fetched, proceed without it.
    try:
        openapi = fetch_openapi(url, headers)
    except Exception:
        openapi = None

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY loaded automatically
    results: list[Diagnosis] = []
    for (method, path), group in groups.items():
        try:
            payload = _build_payload(method, path, group, openapi)
            text = _call_llm(client, payload)
            explanation, cause, steps, optional = _parse_sections(text)
            results.append(Diagnosis(method, path, explanation, cause, steps, optional))
        except anthropic.AuthenticationError:
            results.append(Diagnosis(method, path, error="API key authentication failed — check ANTHROPIC_API_KEY"))
        except anthropic.RateLimitError:
            results.append(Diagnosis(method, path, error="Rate limit — retry shortly"))
        except Exception as exc:  # noqa: BLE001 — one endpoint failing must not kill the whole run
            results.append(Diagnosis(method, path, error=f"Patch generation failed: {type(exc).__name__}"))
    return results


def _call_llm(client, payload: str) -> str:
    message = client.messages.create(
        model=model_name(),
        max_tokens=4096,
        thinking={"type": "adaptive"},  # diagnosis→patch is a reasoning task
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": payload}],
    )
    return "".join(b.text for b in message.content if getattr(b, "type", None) == "text")


def _build_payload(method: str, path: str, findings: list[Finding], openapi: dict | None) -> str:
    lines = [
        "An automated API probe scanned a FastAPI endpoint and found defects. There is no source code.",
        f"\nENDPOINT: {method} {path}",
        "\n[Request(s) the probe sent — the inputs that triggered the defect]",
    ]
    for trigger in _unique(f.trigger for f in findings):
        lines.append(f"  - {trigger}")
    lines.append("\n[Observed response(s)]")
    for summary in _unique(f.response_summary for f in findings):
        lines.append(f"  - {summary}")

    contract = _operation_contract(openapi, method, path)
    if contract is not None:
        import json
        lines.append("\n[The endpoint's declared OpenAPI contract (public; structure only; examples excluded)]")
        lines.append(json.dumps(contract, ensure_ascii=False, indent=2)[:4000])

    lines.append(_TASK)
    return "\n".join(lines)


def _operation_contract(openapi: dict | None, method: str, path: str) -> dict | None:
    """Extract the operation's declared contract (request/response schema). Removes example/examples."""
    if not openapi:
        return None
    operation = (openapi.get("paths", {}).get(path) or {}).get(method.lower())
    if not operation:
        return None
    schemas = openapi.get("components", {}).get("schemas", {})

    referenced: dict = {}
    pending = _collect_refs(operation)
    while pending:
        name = pending.pop()
        if name in referenced or name not in schemas:
            continue
        referenced[name] = schemas[name]
        pending |= _collect_refs(schemas[name])

    return _strip_examples({"operation": operation, "referenced_schemas": referenced})


def _collect_refs(node) -> set[str]:
    refs: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            refs.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            refs |= _collect_refs(value)
    elif isinstance(node, list):
        for value in node:
            refs |= _collect_refs(value)
    return refs


def _strip_examples(obj):
    if isinstance(obj, dict):
        return {k: _strip_examples(v) for k, v in obj.items() if k not in ("example", "examples")}
    if isinstance(obj, list):
        return [_strip_examples(v) for v in obj]
    return obj


_SECTION_RE = re.compile(r"^#{1,6}\s*(Explanation|Cause|Steps|Optional)\s*$", re.MULTILINE)


def _parse_sections(text: str) -> tuple[str, str, str, str]:
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return "", "", text.strip(), ""  # no header found → preserve the raw text (in the Steps slot)
    out = {"Explanation": "", "Cause": "", "Steps": "", "Optional": ""}
    for i, m in enumerate(matches):
        key = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[key] = text[start:end].strip()
    return out["Explanation"], out["Cause"], out["Steps"], out["Optional"]


def _unique(items):
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
