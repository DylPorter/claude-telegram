"""Shared error types for job-sift sources.

The important one is SourceAuthError: sources raise it when they detect an
*authentication* failure (expired session cookie, revoked OAuth token) as
opposed to a genuinely-empty result. The orchestrator catches it and surfaces
a ⚠️ health line in the digest + archive, so a dead source can never again
masquerade as a quiet "None today".
"""

from __future__ import annotations


class SourceAuthError(RuntimeError):
    """A source could not authenticate (expired cookie / revoked token).

    Carries the source name and a human-actionable message describing how to
    re-authenticate.
    """

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        self.message = message
        super().__init__(f"[{source}] {message}")


class SourceFetchError(RuntimeError):
    """Every endpoint configured for a source failed — the source could not look.

    Distinct from `SourceAuthError` (we know *why*: credentials) and, critically,
    distinct from an empty list (we looked, and there was nothing).

    Adapters here degrade per-endpoint: one dead company slug or feed must not
    kill the other nine. But when the degrade covered EVERYTHING — a DNS outage,
    a downed VPN, a revoked API — returning `[]` reports "I looked and found
    nothing", which is a fabrication. Worse, `source_health` then scores the run
    as a success: it zeroes an accumulated failure streak and stamps today as
    `last_success`. So a total per-endpoint failure must escalate to the
    orchestrator as a real exception.
    """

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        self.message = message
        super().__init__(f"[{source}] {message}")
