"""URL ownership-verification gate (S4a) — core-level module.

Purpose: stop Preship from being abused to scan a URL you don't own. Once, right before the scan,
the target URL proves it belongs to the requester via a well-known token. **If unverified, no
external scan request (openapi fetch, schemathesis, LLM) is ever sent** — the only external request
is a single well-known probe GET.

The verification logic is abstracted behind an OwnershipVerifier strategy (room to add DNS etc.
later). The only current implementation is WellKnownVerifier — DNS etc. are not implemented.
"""
from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

WELL_KNOWN_PATH = "/.well-known/preflight-verify"

# Fixed namespace salt (chosen once at random and baked into the code). Binds the token to the URL
# and makes it stateless and reproducible (satisfies "random-based"). It need not be secret —
# security comes from "can you publish this token on the target server", not token secrecy.
_SALT = "b9c41e7a2f8d4063a1e5c0d7f2b3a6e4"


def issue_token(url: str) -> str:
    """A verification token deterministically bound to the URL, so the issuing run and the verifying run get the same value (stateless)."""
    base = url.rstrip("/")
    return hashlib.sha256(f"{_SALT}::{base}".encode("utf-8")).hexdigest()[:32]


@dataclass
class VerificationResult:
    verified: bool
    token: str
    reason: str = ""  # reason for non-verification (human-facing; no keys/sensitive info)


class OwnershipVerifier(Protocol):
    """Ownership-verification strategy. Implementations provide verify(url, headers) -> VerificationResult."""

    def verify(self, url: str, headers: list[str] | None = None) -> VerificationResult: ...


class WellKnownVerifier:
    """Proves ownership by checking whether the issued token is published at `<url>/.well-known/preflight-verify`.

    Sends exactly one external request: the well-known probe GET. It makes no other request.
    Unreachable / absent / mismatched all converge to verified=False (the scan does not proceed).
    """

    def verify(self, url: str, headers: list[str] | None = None) -> VerificationResult:
        token = issue_token(url)
        probe_url = url.rstrip("/") + WELL_KNOWN_PATH
        request = urllib.request.Request(probe_url)
        for name, value in _parse_headers(headers):
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 (user-supplied URL)
                body = response.read(4096).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return VerificationResult(False, token, reason=f"well-known not found (HTTP {exc.code})")
        except Exception as exc:  # noqa: BLE001 — any failure to reach it counts as 'unverified' (no request goes out)
            return VerificationResult(False, token, reason=f"well-known unreachable ({type(exc).__name__})")
        if token in body:
            return VerificationResult(True, token)
        return VerificationResult(False, token, reason="well-known token mismatch")


def _parse_headers(headers: list[str] | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for h in headers or []:
        name, sep, value = h.partition(":")
        if sep:
            out.append((name.strip(), value.strip()))
    return out
