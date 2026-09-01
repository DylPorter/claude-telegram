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


class SourceNotConfiguredError(RuntimeError):
    """The source has no configuration to work from — it was never asked anything.

    The third outcome, and the one the first cut of the staleness alarm missed.
    `SourceFetchError` means "I tried to look and could not"; an empty list means
    "I looked and there was nothing". This means neither: with no slugs in
    `companies.yaml` there is no endpoint to poll, so the run produced no
    evidence about the source at all.

    That distinction is load-bearing because of how `source_health` scores a
    run. An unconfigured adapter returns `[]` without raising, lands in the
    orchestrator's `succeeded` list, and is scored a SUCCESS — resetting an
    accumulated failure streak and stamping today as `last_success`. Verified:
    with `companies.yaml` removed, a seeded 12-run streak went to 0.

    So the orchestrator catches this separately and puts the source in NEITHER
    the `succeeded` list nor the error map, which `update_health` already treats
    as "not attempted this run" and PRUNES — the same handling a source that is
    commented out of the fetch list gets. Not a success, not a failure, and
    above all not a fabricated `last_success`.
    """

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        self.message = message
        super().__init__(f"[{source}] {message}")
