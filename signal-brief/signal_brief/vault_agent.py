"""Shared helper for evening + weekly orchestrators.

Both spawn a `claude -p` subagent with full vault tool access to do the
vault-side processing, then parse a structured JSON summary out of the
final response and push it to Telegram.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field

from signal_brief.config import (
    CLAUDE_BIN,
    SIGNAL_BRIEF_EFFORT,
    SIGNAL_BRIEF_MODEL,
    VAULT_ROOT,
)
from signal_brief.filter import _parse_claude_response  # reuse defensive parser
from signal_brief.schema import Digest, DigestSection

log = logging.getLogger(__name__)

VAULT_AGENT_TIMEOUT = 1800.0  # 30 min — vault sweeps + writes can be slow


@dataclass
class VaultAgentResult:
    headline: str = ""
    sections: list[DigestSection] = field(default_factory=list)
    rationale: str = ""
    raw: str = ""


def run_vault_agent(prompt: str) -> VaultAgentResult:
    """Spawn `claude -p` in the vault with bypassPermissions; expect JSON output.

    The prompt should describe the work to do AND explicitly require JSON output
    in the agreed shape:

        { "headline": str, "sections": [{"title", "body"}], "rationale": str }
    """
    log.info("invoking vault agent (%s/%s)", SIGNAL_BRIEF_MODEL, SIGNAL_BRIEF_EFFORT)
    proc = subprocess.run(
        [
            CLAUDE_BIN,
            "-p", prompt,
            "--output-format", "text",
            "--permission-mode", "bypassPermissions",
            "--model", SIGNAL_BRIEF_MODEL,
            "--effort", SIGNAL_BRIEF_EFFORT,
        ],
        capture_output=True,
        text=True,
        timeout=VAULT_AGENT_TIMEOUT,
        check=False,
        cwd=str(VAULT_ROOT),
    )

    if proc.returncode != 0:
        log.error("vault agent exited %d: %s", proc.returncode, proc.stderr[-1000:])
        return VaultAgentResult(
            headline="⚠️ Vault agent failed",
            sections=[DigestSection(
                title="Error",
                body=f"Vault agent exited {proc.returncode}.\n\n`{proc.stderr.strip()[-300:]}`",
            )],
            rationale=f"Subprocess error: {proc.stderr.strip()[-500:]}",
            raw=proc.stdout,
        )

    try:
        parsed = _parse_claude_response(proc.stdout)
    except ValueError as e:
        log.error("could not parse vault agent output: %s", e)
        # Last-resort fallback: surface the raw text as a single section.
        return VaultAgentResult(
            headline="⚠️ Vault agent output unparseable",
            sections=[DigestSection(
                title="Raw output",
                body=proc.stdout.strip()[:1500],
            )],
            rationale=str(e)[:500],
            raw=proc.stdout,
        )

    sections = [
        DigestSection(title=s.get("title", "").strip(), body=s.get("body", "").strip())
        for s in parsed.get("sections", [])
    ]
    return VaultAgentResult(
        headline=parsed.get("headline", "").strip(),
        sections=sections,
        rationale=parsed.get("rationale", "").strip(),
        raw=proc.stdout,
    )


def result_to_digest(result: VaultAgentResult, *, date: str) -> Digest:
    return Digest(
        date=date,
        headline=result.headline,
        sections=result.sections,
        rationale=result.rationale,
    )
