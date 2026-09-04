# Scrub + dry-run guard — report

Branch `scrub-and-guard`. Two changes: remove the operator's job-search record
from a public repo, and stop `--dry-run` writing state.

## Task 1 — scrub

The sensitive thing was never the employer names on their own. It was that the
repo let a stranger reconstruct **which roles the operator applied to and which
ones closed on him** — application records with real listing ids attached.

Replacements keep the SHAPE of every value so nothing was weakened to make a
scrub easier: LinkedIn ids stay 10 digits, CEDARS ids stay `G` + 7 digits,
employers stay plausible multi-word names.

| What it was | Placeholder now in the tree |
|---|---|
| four real LinkedIn listing ids | `4400000001` – `4400000004` |
| one real CEDARS listing id | `G2600001` |
| a real HFT employer | Northwind / Northwind Trading |
| a real bank | Contoso Bank |
| the employer on the `applied` row | Northwind Capital |

The real values are deliberately NOT reproduced here. This report ships in the
same public repo as the code it describes; a before/after table would undo the
scrub in the act of documenting it. The mapping is recoverable from the diff by
anyone who already has the history, which is the correct audience for it.

Files changed:

- `job-sift/tests/test_open_roles.py` — the `status="applied"` row. This was the
  single most direct statement in the repo that the operator applied somewhere.
- `job-sift/tests/test_liveness.py` — the marker-set docstring named three real
  closed rows by employer + id. Rewritten to state the same finding ("every
  closed posting seen while tuning this matched the one marker") without the
  roster. Real ids elsewhere in the file scrubbed too — one flagged line is not
  a scrub if the same ids sit twenty lines down.
- `job-sift/tests/test_dedupe_collapse.py`, `job-sift/tests/test_silent_zero.py` —
  employer/listing pairs lifted from the register.
- `job-sift/job_sift/schema.py`, `job-sift/job_sift/open_roles.py` — the same
  pairs quoted in design docstrings.
- `docs/superpowers/plans/close-issues-plan.md` — the worst one: real ids
  annotated "already No longer accepting applications", i.e. an outcome log.
  Now illustrative ids, explicitly labelled as such.
- `docs/superpowers/plans/keepalive-plan.md` — absolute home paths.
- `signal-brief/signal_brief/orchestrators/agent_watch.py` — `EMAIL_TO` no
  longer defaults to a personal address.
- `signal-brief/tests/test_threads.py` and `signal-brief/signal_brief/threads.py`
  — a real client first name, a real hackathon the operator entered, and a real
  newsletter issue, used as worked examples in a test and a module docstring.
  The `threads.py` docstring was NOT on the brief's list; the final unfiltered
  sweep caught it. Same class of data, so it went with the rest.
- `signal-brief/tests/test_home_refresh.py`, `src/index.ts` — home paths.

### EMAIL_TO: removing a default creates a second hazard

Dropping the hardcoded address would have left an unset variable silently
mailing nobody — a backup delivery leg that has been quietly off for weeks is
exactly the failure the second leg exists to prevent. So the absence is loud:
`_send_email` returns early and logs an error naming `AGENT_WATCH_EMAIL_TO` and
the number of hits that went out over Telegram only. Three tests pin it: no
`@gmail.com` anywhere in the module source, loud-and-off when unset, still
sends when configured.

### Parameterised, not scrubbed

- `evening.py` — the vault root in the prompt came from `config.VAULT_ROOT`.
  Substituted via a `__VAULT_ROOT__` token rather than `.format()`, because the
  prompt embeds a JSON block and doubling every brace to survive formatting is a
  worse trade than one explicit `.replace`.
- `weekly.py` — the link-health skill path is now `config.LINK_HEALTH_SKILL`
  (`SIGNAL_BRIEF_LINK_HEALTH_SKILL`, defaulting to `~/.claude/skills/...` off
  `Path.home()`).
- `test_home_refresh.py` — the live-`Home.md` test resolved a hardcoded vault
  path. Now reads `config.HOME_NOTE` and skips cleanly when no vault is
  configured. This is why signal-brief reports 1 skip in an unconfigured
  environment; with `SIGNAL_BRIEF_VAULT_ROOT` set it is 98 passed, 0 skipped.

### Kept — product data, not personal

- **`job-sift/config/companies.yaml`** — the curated list of companies whose
  public ATS APIs the tool polls. This is configuration of what the tool
  *watches*, and says nothing about what the operator applied to. Removing it
  breaks the tool and hides nothing.
- **`job-sift/job_sift/classifier.py`** — the prestige/boost employer vocabulary
  and the hard-skip list. Same reasoning: it is the classifier's dictionary. A
  reader learns the tool's definition of "prestige", not the operator's history.
- **`job-sift/README.md:141`** — the lane-definition table, whose prestige row
  is illustrated with a few well-known employer names. Checked
  against the brief's test (a real listing, or an application outcome): it is
  neither. It documents what the lane means. The README carries no real listing
  ids at all.
- **`LICENSE`, `systemd/*.service`** — the copyright line is deliberate public
  authorship. The systemd units carry an absolute home path in `WorkingDirectory`,
  which systemd requires to be absolute; these were outside the brief's scope
  and are install-time config rather than a record of anything. Flagged, not
  changed.

## Task 2 — `--dry-run` wrote state

`log_classification` sat inside the classify loop with no dry-run guard in both
services, so a dry run appended one line per listing to
`.data/state/classifier_log.jsonl` (job-sift) and `relevance_log.jsonl`
(hk-events). Every other writer in both `run()` functions was already guarded —
`_write_board` even says so in its own comment — and `job-sift/sift:6` documents
`--dry-run` as "no state save".

Not a cosmetic leak: job-sift's README plans to derive the prestige whitelist
from ~30 days of that log, so a dry run was quietly voting in the dataset that
will later configure the classifier.

Fix is one guard per repo. Three tests each, asserted on the FILESYSTEM rather
than on a call count so the guard cannot be removed without failing:

1. the specific log file is absent after a dry run,
2. the state dir is empty after a dry run — the general property, so the *next*
   unguarded writer fails here too,
3. a live run still logs every classification, so the guard cannot pass by
   disabling the log outright.

### Found while testing, not fixed

`hk_events/dedupe.py` does `from hk_events.config import STATE_DIR`, binding the
path at **import** time. Patching `config.STATE_DIR` in a test therefore leaves
`_log_path()` pointing at the real `.data/state/`. job-sift already fixed this
(it resolves `config.STATE_DIR` at call time, with a test pinning that
property); hk-events has not. The new test patches `dedupe.STATE_DIR` directly
and says why in a comment. Left unfixed here on purpose — this change is a
scrub plus a one-line correctness fix, and re-plumbing a module's path
resolution is neither. Worth its own issue.

## Test counts

| Suite | Before | After |
|---|---|---|
| job-sift | 655 | 658 |
| hk-events | 253 | 256 |
| signal-brief | 95 | 97 passed + 1 skipped (98 passed with a vault configured) |

No network, no new dependencies, nothing written to any real `.data/state/`.
