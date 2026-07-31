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
