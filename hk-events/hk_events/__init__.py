"""hk-events: daily Hong Kong tech/startup/AI + SME-buyer event digest for the operator.

Aggregates events from iCal/feed sources (Meetup per-group .ics, Luma calendars,
AI Tinkerers) plus brittle scrape sources (Cyberport, StartmeupHK), dedupes,
LLM-classifies each event for relevance (funded-startup/AI/founder room vs
SME-buyer room), surfaces matches to Telegram, and idempotently creates Google
Calendar events via the `gws` CLI.

Mirrors the job-sift architecture (sources → dedupe → classify → render → push)
and reuses the same /push Telegram endpoint as signal-brief.
"""
