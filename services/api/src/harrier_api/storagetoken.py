"""A scheduled run's Storage token, minted by the hosted scheduler (spec 107,
Decision taken (2026-10-16)).

A scheduled run has no request, so no session token to reach its owner's
Storage prefix with. The scheduler holds one private signing key,
`HARRIER_STORAGE_SIGNING_KEY`, whose key is imported into the Supabase
project as a JWT signing key, and mints one short-lived token per scheduled
run for that run's owner. Nothing else holds the key: the API refuses to
start with it (`harrier_api.hostedauth`), and a run's environment is an
allowlist that does not name it (`harrier_api.runenv`).

The key's form in the variable is a private JSON Web Key: an EC key on the
P-256 curve (`kty` `EC`, `crv` `P-256`, `x`, `y`, `d`) with a non-empty
`kid`. That is the form Supabase's dashboard imports, and the `kid` is the
one Supabase's key set then lists, so Storage finds the key to verify a
token by its header. Tokens are `ES256`, an algorithm spec 104's verifier
accepts. The key's text is never printed, logged or kept in a repr.

`jwt` and `cryptography` are the `hosted` group's (spec 104), imported here
when a key is read, never when the module loads.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from harrier_api.hostedauth import STORAGE_SIGNING_KEY_VARIABLE

ALGORITHM = "ES256"
CURVE = "P-256"
ROLE = "authenticated"
AUDIENCE = "authenticated"
# The margin past the run's timeout a token lives, in seconds: the decision
# bounds `exp` by the timeout plus one minute.
EXPIRY_MARGIN_SECONDS = 60

SIGNING_KEY_REFUSAL = (
    f"{STORAGE_SIGNING_KEY_VARIABLE} is not a private P-256 JSON Web Key with a kid; "
    "the scheduler signs each scheduled run's Storage token with it"
)


class SigningKeyRefused(ValueError):
    """The signing key's text is not a key the scheduler can sign with. The
    message names the variable and the expected form, never the text."""

    def __init__(self) -> None:
        super().__init__(SIGNING_KEY_REFUSAL)


@dataclass(frozen=True)
class StorageSigner:
    """The scheduler's signing key and where its tokens say they come from.

    `issuer` is the project's Auth issuer, `<HARRIER_SUPABASE_URL>/auth/v1`,
    which spec 104's verifier checks; Storage reads the `role` and `sub`.
    """

    kid: str
    issuer: str
    key: Any = field(repr=False)

    def mint(self, owner: str, *, now: datetime, lifetime_seconds: int) -> str:
        """One token for `owner`'s scheduled run: `sub` the owner in its
        canonical lowercase form, `role` and `aud` `authenticated`, issued
        `now` and expiring `lifetime_seconds` later. Raises ValueError for
        an owner that is not a UUID or a lifetime below one second."""
        import jwt

        if lifetime_seconds < 1:
            raise ValueError("a scheduled run's token must live at least one second")
        subject = str(uuid.UUID(owner))
        issued = int(now.timestamp())
        claims: dict[str, object] = {
            "sub": subject,
            "role": ROLE,
            "aud": AUDIENCE,
            "iss": self.issuer,
            "iat": issued,
            "exp": issued + lifetime_seconds,
            "is_anonymous": False,
        }
        return jwt.encode(
            claims, self.key, algorithm=ALGORITHM, headers={"kid": self.kid, "typ": "JWT"}
        )


def token_lifetime(timeout_seconds: int) -> int:
    """How long a scheduled run's token lives: the run's timeout
    (`HARRIER_SCHEDULED_RUN_TIMEOUT_SECONDS`) plus one minute, the most the
    decision allows."""
    return timeout_seconds + EXPIRY_MARGIN_SECONDS


def parse_signing_key(text: str, *, issuer: str) -> StorageSigner:
    """The signer for the key in `text`, or `SigningKeyRefused`.

    `text` is a private P-256 JSON Web Key with a `kid`. Anything else is
    refused: another curve or key type, a public key alone, a missing or
    empty `kid`, or text that is not JSON. The refusal never quotes it.
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from jwt.algorithms import ECAlgorithm

    try:
        document: object = json.loads(text)
    except ValueError:
        raise SigningKeyRefused from None
    if not isinstance(document, dict):
        raise SigningKeyRefused
    jwk = cast("dict[str, object]", document)
    kid = jwk.get("kid")
    if (
        jwk.get("kty") != "EC"
        or jwk.get("crv") != CURVE
        or not isinstance(kid, str)
        or not kid.strip()
        or not isinstance(jwk.get("d"), str)
    ):
        raise SigningKeyRefused
    try:
        key = ECAlgorithm.from_jwk(json.dumps(jwk))
    except Exception:
        # The library's text can quote the key; only the refusal is raised.
        raise SigningKeyRefused from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise SigningKeyRefused
    return StorageSigner(kid=kid, issuer=issuer, key=key)
