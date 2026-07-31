"""Refresh the CEDARS session cookie from a locally logged-in browser.

CEDARS auth is HKU Portal SSO (2FA) — there is no scriptable credential login,
so the scraper depends on a `PHPSESSID` session cookie exported from a browser
where you're already logged in. This module pulls that cookie automatically via
`browser_cookie3` so you never have to copy-paste it: just stay logged into
CEDARS in your browser and run this before an on-demand sift.

Defaults to Firefox — that's where the HKU Portal / CEDARS login actually
lives. (It used to default to Chrome, which meant a missing cookie looked like
a session-expiry failure rather than a wrong-browser one.)

Interactive-only: decrypting the browser's cookie DB needs your unlocked OS
keyring, so this cannot run from the headless systemd timer — it's for the
manual "sift now" path. Usage:

    python -m job_sift.refresh_cookie            # pull from Firefox (default)
    python -m job_sift.refresh_cookie --browser chrome
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

from job_sift.config import CEDARS_COOKIES_PATH, CEDARS_PORTAL_URL

# Session cookies CEDARS sets once you're logged in. PHPSESSID is the one that
# actually authenticates; esd_from_sys rides along and we keep it if present.
_WANTED = ("PHPSESSID", "esd_from_sys")
_DOMAIN = "cedars.hku.hk"
# CEDARS runs SEPARATE PHP sessions per host (soconnect./web2./www.cedars…),
# so there are multiple PHPSESSID cookies with the same name. We must pick the
# one scoped to the host the scraper actually hits, or we send a valid-looking
# but wrong-session cookie and get bounced to login.
_TARGET_HOST = urlparse(CEDARS_PORTAL_URL).hostname or "web2.cedars.hku.hk"


def _load_from_browser(browser: str) -> dict[str, str]:
    try:
        import browser_cookie3 as bc3
    except ImportError:  # pragma: no cover - dependency guard
        print(
            "browser_cookie3 not installed — run `pip install browser_cookie3` "
            "in the job-sift venv.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    loader = getattr(bc3, browser, None)
    if loader is None:
        print(f"unknown browser '{browser}' — try chrome/chromium/brave/firefox", file=sys.stderr)
        raise SystemExit(2)

    jar = loader(domain_name=_DOMAIN)
    # Keep only cookies scoped to the exact host the scraper hits — otherwise a
    # www./soconnect. PHPSESSID with the same name silently shadows the web2 one.
    picked: dict[str, str] = {}
    for c in jar:
        if (c.domain or "").lstrip(".") == _TARGET_HOST:
            picked[c.name] = c.value
    return picked


def refresh(browser: str = "firefox") -> int:
    """Pull CEDARS session cookies from `browser` and write cedars.json.

    Returns a process exit code: 0 on success, 1 if not logged in.
    """
    cookies = _load_from_browser(browser)
    present = {k: cookies[k] for k in _WANTED if k in cookies}

    if "PHPSESSID" not in present:
        print(
            f"No PHPSESSID for {_DOMAIN} in {browser} — you're not logged into "
            "CEDARS in that browser. Open https://web2.cedars.hku.hk/jobs/ , log "
            "in via HKU Portal, then re-run.",
            file=sys.stderr,
        )
        return 1

    CEDARS_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    CEDARS_COOKIES_PATH.write_text(json.dumps(present, indent=2))
    print(f"wrote {len(present)} cookie(s) {list(present)} → {CEDARS_COOKIES_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="refresh CEDARS session cookie from a browser")
    parser.add_argument(
        "--browser",
        default="firefox",
        help="browser to pull from (firefox/chrome/chromium/brave); default firefox",
    )
    args = parser.parse_args()
    return refresh(args.browser)


if __name__ == "__main__":
    sys.exit(main())
