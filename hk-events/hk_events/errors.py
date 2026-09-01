"""Shared error types for hk-events sources.

Mirrors `job_sift/errors.py` — the two bots are deliberate copies of each other
and neither depends on the other. hk-events has no cookie/OAuth source, so it
only needs the fetch-failure half.
"""

from __future__ import annotations


class SourceFetchError(RuntimeError):
    """Every feed configured for a source failed — the source could not look.

    Distinct from an empty list, which means "I looked, and there was nothing".

    Feed adapters degrade per-feed: one dead .ics must not kill the other three.
    But when the degrade covered EVERY configured feed — a DNS outage, a downed
    VPN — returning `[]` reports a zero we did not observe. Worse,
    `source_health` then scores the run as a success: it zeroes an accumulated
    failure streak and stamps today as `last_success`. So a total per-feed
    failure must escalate to the orchestrator as a real exception.
    """

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        self.message = message
        super().__init__(f"[{source}] {message}")
