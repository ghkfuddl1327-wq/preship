"""Human-facing CLI: `preship scan <URL>`.

Formats the list[Finding] returned by core.scan() into human-readable output (not raw string dumps).
Machine (JSON) output and patch generation are S3 — not here.
"""
from __future__ import annotations

import argparse
import sys
from collections import OrderedDict

from ohs_preflight.core import Finding, ScanError, scan
from ohs_preflight.patch import Diagnosis, generate_patches, has_api_key
from ohs_preflight.verify import WELL_KNOWN_PATH, VerificationResult, WellKnownVerifier


# A(c): when the same check repeats across endpoints, group it as "1 pattern × N locations".
# Canned bulk-fix direction — not AI-generated (unrelated to the S3.5 prompts).
_FIX_HINT = {
    "not_a_server_error": "Eliminate the 500 with input validation / exception handling (tighten types, add guards, use standard exceptions).",
    "status_code_conformance": "Document the returned status code on the route via responses={code: {...}}.",
    "response_schema_conformance": "Make the response satisfy the declared response_model schema (return a model instance).",
    "content_type_conformance": "Match the response to the declared content-type (application/json, etc.).",
}

# S3.5(a): plain-language problem statement per pattern — sentence ① of the prompt to paste into an external AI.
# Describes the diagnostic classification (observed behavior) only — we don't know the user's source, so we don't guess the cause.
_PROBLEM = {
    "not_a_server_error": "The endpoints below return a server 500 from an unhandled exception on certain inputs.",
    "status_code_conformance": "The endpoints below return status codes that aren't documented in the OpenAPI spec.",
    "response_schema_conformance": "The response bodies of the endpoints below don't satisfy the declared response_model schema.",
    "content_type_conformance": "The endpoints below respond with a different format than the declared content-type.",
    "documented_5xx_response": "These endpoints return a 5xx status that IS documented in the OpenAPI spec - likely intended (e.g. maintenance/gateway). Shown for review, not an unhandled crash.",
}


def _group_by_pattern(findings: list[Finding]) -> "list[tuple[str, list[Finding]]]":
    """Group defects by check (pattern) and return them sorted with high patterns first.

    Shared by _format (human report) and _format_prompts (paste-ready prompts) —
    the "1 pattern = 1 group" unit is defined in one place only.
    """
    patterns: "OrderedDict[str, list[Finding]]" = OrderedDict()
    for f in findings:
        patterns.setdefault(f.check, []).append(f)
    # high patterns first, low after (by the level of the group's first Finding; otherwise keep appearance order)
    return sorted(patterns.items(), key=lambda kv: 0 if kv[1][0].level == "high" else 1)


def _format(findings: list[Finding]) -> str:
    if not findings:
        return "✓ No issues found.\n"

    ordered = _group_by_pattern(findings)
    patterns = dict(ordered)

    n_ep = len({(f.method, f.path) for f in findings})
    lines = [
        f"{len(findings)} defect(s) / {len(patterns)} pattern(s) / {n_ep} endpoint(s) "
        "(severity: high first):\n"
    ]
    for check, group in ordered:
        level = group[0].level
        endpoints = sorted({f"{f.method} {f.path}" for f in group})
        lines.append(f"[{level.upper()}] {check} — {len(endpoints)} endpoint(s)")
        hint = _FIX_HINT.get(check)
        if hint:
            lines.append(f"  Bulk fix: {hint}")
        lines.append("  Affected endpoints:")
        for ep in endpoints:
            lines.append(f"    - {ep}")
        rep = group[0]
        trig = rep.trigger if len(rep.trigger) <= 160 else rep.trigger[:160] + "…"
        lines.append(f"  Example: {trig}  →  {rep.response_summary}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# S3.5: a one-line notice in the scan output. Grounds for "it was beta and announced" if direction changes later (e.g. paid auto-patch).
_BETA_NOTICE = (
    "Beta: the AI fix prompts are free during beta. Features and policy may change at general availability."
)


def _format_prompts(findings: list[Finding]) -> str:
    """Assemble, per pattern (check), a prompt to paste verbatim into an external AI.

    **Does not call an LLM** — it only assembles already-refined Findings into text (zero cost,
    deterministic). 1 pattern = 1 prompt (same unit as _group_by_pattern). A wild app's "same
    pattern × N endpoints" naturally collapses into a single prompt.

    Constraint (★): we don't know the user's source, so we never imagine code (<the user's handler>
    placeholder) — only the observed diagnostic info (request trigger / response summary) goes in.
    It names no specific AI (model-neutral).
    """
    if not findings:
        return ""

    bar = "=" * 64
    sep = "─" * 64
    ordered = _group_by_pattern(findings)
    out = [
        "",
        bar,
        "AI fix prompts — free, no key · paste into an external AI (S3.5 · beta)",
        _BETA_NOTICE,
        bar,
        "For each pattern, copy the whole prompt block below and paste it into the AI you use (any of them).",
        "",
    ]
    for i, (check, group) in enumerate(ordered, start=1):
        level = group[0].level
        endpoints = sorted({f"{f.method} {f.path}" for f in group})
        problem = _PROBLEM.get(check, f"A '{check}' violation was observed on the endpoints below.")

        out.append(sep)
        out.append(f"[Prompt {i}/{len(ordered)}] [{level.upper()}] {check} · {len(endpoints)} endpoint(s)")
        out.append(sep)
        # ④ instruction (model-neutral) + no-imagining-source notice
        out.append(
            "You are a FastAPI expert. Using only the diagnostic info below, produce a bulk fix patch. "
            "The handler source is not provided (each handler is <the user's handler>). "
            "Do not imagine code — infer the cause from the observed behavior alone and fix it."
        )
        out.append("")
        # ① problem (plain language)
        out.append(f"[Problem] {problem}")
        out.append("")
        # ② affected endpoints
        out.append("[Affected endpoints]")
        for ep in endpoints:
            out.append(f"  - {ep}")
        out.append("")
        # ③ observed behavior (per-endpoint request trigger → response summary) — diagnostic info only
        out.append("[Observed behavior] (the request the scanner actually sent → the response it got)")
        for f in group:
            trig = f.trigger if len(f.trigger) <= 200 else f.trigger[:200] + "…"
            out.append(f"  - Request: {trig}")
            out.append(f"    Response: {f.response_summary}")
        out.append("")
        # ④ deliverable instructions
        out.append("[What to do]")
        out.append("  1. Infer why each endpoint produced this behavior,")
        out.append("  2. Propose a bulk fix patch that follows FastAPI idioms (fixed code or a diff), and")
        out.append("  3. Explain why each fix resolves the problem.")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preship",
        description="Scan a FastAPI staging URL with schemathesis and print the defects.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    scan_p = sub.add_parser("scan", help="Scan a FastAPI URL.")
    scan_p.add_argument("url", help="FastAPI base URL (/openapi.json is fetched from here).")
    scan_p.add_argument(
        "--header",
        action="append",
        default=[],
        metavar='"Name: Value"',
        help='Header to send on every request (repeatable). e.g. --header "Authorization: Bearer ..."',
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "scan":
        # Ownership gate: if unverified, no external request (scan/openapi/LLM) is sent.
        verification = WellKnownVerifier().verify(args.url, headers=args.header)
        if not verification.verified:
            sys.stdout.write(_format_rejection(args.url, verification))
            return 3
        try:
            findings = scan(args.url, headers=args.header)
        except ScanError as exc:
            print(f"scan failed: {exc}", file=sys.stderr)
            return 2
        sys.stdout.write(_format(findings))
        # S3.5: after the findings report, the per-pattern AI fix prompts (text assembly only, zero LLM calls).
        sys.stdout.write(_format_prompts(findings))
        _maybe_patch(findings, args)
        return 1 if findings else 0
    return 0


def _maybe_patch(findings: list[Finding], args) -> None:
    """If there are defects and a key is set, generate and print patches. No key → just a notice (scan already printed)."""
    if not findings:
        return
    if not has_api_key():
        sys.stdout.write(
            "\nℹ️  Set ANTHROPIC_API_KEY to also generate a per-defect explanation, cause, and "
            "FastAPI patch (BYOK). The scan works without a key.\n"
        )
        return
    try:
        diagnoses = generate_patches(findings, args.url, headers=args.header)
    except Exception as exc:  # noqa: BLE001 — scan results are already printed; only the patch step fails gracefully
        print(f"\nPatch generation step failed (scan results are above): {type(exc).__name__}", file=sys.stderr)
        return
    sys.stdout.write(_format_patches(diagnoses))


def _format_patches(diagnoses: list[Diagnosis]) -> str:
    bar = "=" * 64
    lines = ["", bar, "AI patch suggestions — BYOK · generated from diagnostics, no source (S3)", bar, ""]
    for d in diagnoses:
        lines.append(f"■ {d.method} {d.path}")
        if d.error:
            lines.append(f"    ⚠ {d.error}")
        else:
            lines.append("  [Explanation]")
            lines.append(_indent(d.explanation))
            lines.append("  [Cause]")
            lines.append(_indent(d.cause))
            lines.append("  [Steps]")
            lines.append(_indent(d.steps))
            if (d.optional or "").strip() and d.optional.strip() != "None":
                lines.append("  [Optional]")
                lines.append(_indent(d.optional))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _indent(text: str, prefix: str = "    ") -> str:
    body = (text or "").strip() or "(none)"
    return "\n".join(prefix + line for line in body.splitlines())


def _format_rejection(url: str, result: VerificationResult) -> str:
    """Rejection notice when ownership is unverified — make the reason, method, and token clear (avoid bug confusion)."""
    lines = [
        f"✗ Ownership unverified — refusing to scan (reason: {result.reason}).",
        "",
        "You must prove this URL is yours before we scan it (prevents scanning third-party servers).",
        f"Make {WELL_KNOWN_PATH} on the target server return the token below, then run again.",
        "",
        f"  Token: {result.token}",
        "",
        "  # FastAPI example (one route):",
        "  from fastapi.responses import PlainTextResponse",
        f'  @app.get("{WELL_KNOWN_PATH}")',
        "  def _preflight_verify():",
        f'      return PlainTextResponse("{result.token}")',
        "",
        f"  Once proven: preship scan {url}",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
