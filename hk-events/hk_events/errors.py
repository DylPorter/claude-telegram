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


class SourceNotConfiguredError(RuntimeError):
    """The source has no configuration to work from — it was never asked anything.

    Mirrors `job_sift/errors.py`. `SourceFetchError` means "I tried to look and
    could not"; an empty list means "I looked and there was nothing". This means
    neither: with no feed URLs configured for the group (or every entry still
    marked TODO) there is no feed to fetch, so the run produced no evidence
    about the source at all.

    That distinction is load-bearing because of how `source_health` scores a
    run. An unconfigured adapter returns `[]` without raising, lands in the
    orchestrator's `succeeded` list, and is scored a SUCCESS — resetting an
    accumulated failure streak and stamping today as `last_success`.

    So the orchestrator catches this separately and puts the source in NEITHER
    the `succeeded` list nor the error map, which `update_health` already treats
    as "not attempted this run" and PRUNES — the same handling the three
    commented-out adapters get. Not a success, not a failure, and above all not
    a fabricated `last_success`.
    """

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        self.message = message
        super().__init__(f"[{source}] {message}")
