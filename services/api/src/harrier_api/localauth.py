"""Who is allowed to change things through the local API (spec 035).

The API binds locally and has no accounts, which is a reasonable position for
a single-user tool. What made it a finding is what an unauthenticated caller
reached and how easily: a subprocess inheriting every credential, a billed
scraping run whose count it could also set, and stored configuration that the
next scheduled run executes.

Two properties turned that from local-process-only into browser-reachable,
and both are closed here.

**No trusted-host check.** DNS rebinding lets a page the operator visits
resolve its own hostname to 127.0.0.1 and then talk to this API as
same-origin, which defeats every browser protection at once. `TrustedHost`
rejects a request whose Host header is not a local name, so the rebound
request never reaches a route.

**A shared token on every state-changing request.** Generated once per
install, stored 0600 beside the database, and readable only by something that
can already read the operator's files. A cross-origin page cannot read it,
so it cannot forge a write even if it guesses the shape.

The token is not a secret from the operator and does not pretend to be
authentication in the multi-user sense. It is the thing that makes a request
prove it came from the operator's own browser rather than from a page that
happens to be open in it.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request

from harrier.db import data_dir

TOKEN_HEADER = "X-Harrier-Token"
TOKEN_FILENAME = "api-token"
TOKEN_ENV = "HARRIER_API_TOKEN"

# Hostnames a local browser legitimately uses. A request arriving with any
# other Host is either misrouted or rebound, and neither should reach a route.
TRUSTED_HOSTS = ("localhost", "127.0.0.1", "[::1]", "0.0.0.0", "testserver")


def token_path() -> Path:
    return data_dir() / TOKEN_FILENAME


def load_or_create_token() -> str:
    """The install's token, created on first use.

    Written 0600 because it is the whole authority for a state-changing
    request. An environment override exists for the tests and for anyone
    running the API somewhere the data directory is not writable.
    """
    override = os.environ.get(TOKEN_ENV, "").strip()
    if override:
        return override
    path = token_path()
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return token


def token_matches(candidate: str) -> bool:
    """Constant-time comparison. The token is short and guessable only by
    brute force, and a timing oracle is free to remove."""
    return hmac.compare_digest(candidate.strip(), load_or_create_token())


def require_token(request: Request) -> None:
    """FastAPI dependency for every route that changes something.

    Reads the header, or a form field for the capture confirmation page,
    which is a real form submission rather than a fetch and cannot set
    headers.
    """
    presented = request.headers.get(TOKEN_HEADER, "")
    if not presented:
        presented = getattr(request.state, "form_token", "") or ""
    if not presented or not token_matches(presented):
        raise HTTPException(
            status_code=403,
            detail=(
                "this request did not present the local API token. "
                "The UI sends it automatically; a page that is not the harrier UI cannot."
            ),
        )
