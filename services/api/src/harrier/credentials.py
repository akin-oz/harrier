"""Tenant credentials, stored encrypted (spec 116).

Hosted, each owner brings their own keys (spec 102, open decision 3), and
ADR-013 decision 8 stores them per owner. This module holds the kinds and
their fields, the rule every field follows, encryption and decryption with
a key the caller passes, and the reads and writes through spec 112's
`Connection`. It reads no process variable: the hosted API reads
`HARRIER_CREDENTIALS_KEY` and passes it in (`parse_key`).

The table is Postgres only (`harrier.tracker.schema.HOSTED_ONLY_TABLES`):
locally credentials stay in `.env`. Its row policy keeps each owner to
their own rows, so no statement here names an owner.

Each credential is AES-256-GCM over its fields as one JSON object, with a
fresh 96-bit nonce per write and `harrier-credential:v1:<owner>:<kind>` as
associated data, so a ciphertext copied to another owner's row or another
kind does not decrypt. The key lives on Fly, the ciphertext in Supabase.

`cryptography` is imported where a value is encrypted or decrypted, never
at module level: the local API imports this module through its routes, and
the image installs no `hosted` group.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast, get_args

from harrier.connection import Connection, store_now, update_or_insert

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CredentialKind = Literal["ai", "apify", "hunter", "telegram"]
KINDS: tuple[CredentialKind, ...] = get_args(CredentialKind)

# The two API providers a hosted run may use (ADR-013 decision 8).
AiProvider = Literal["openai-api", "anthropic-api"]
AI_PROVIDERS: tuple[AiProvider, ...] = get_args(AiProvider)


@dataclass(frozen=True)
class KindFields:
    """A kind's fields, which of them may be left out, and which are
    secret: spec 106 scrubs every secret value from a run's events."""

    fields: tuple[str, ...]
    optional: tuple[str, ...]
    secret: tuple[str, ...]


# Spec 116, "Credential kinds". The Telegram chat id is secret everywhere
# (decision D7, 2026-10-11).
KIND_FIELDS: dict[CredentialKind, KindFields] = {
    "ai": KindFields(("provider", "key", "model"), optional=("model",), secret=("key",)),
    "apify": KindFields(("token",), optional=(), secret=("token",)),
    "hunter": KindFields(("key",), optional=(), secret=("key",)),
    "telegram": KindFields(("bot_token", "chat_id"), optional=(), secret=("bot_token", "chat_id")),
}

# --- the field rule ---

# A chosen bound, not a measured one (spec 116).
FIELD_MAX_BYTES = 4096

EMPTY = "must not be empty"
TOO_LONG = f"must be at most {FIELD_MAX_BYTES} bytes in UTF-8"
CONTROL_CHARACTER = "must not hold a control character"
# A lone surrogate is text Python holds and UTF-8 cannot encode. Asked to,
# the codec raises an error that quotes the character and holds the whole
# value, so it is refused by its own rule first (local review of PR #310).
NOT_UTF8 = "must be text that UTF-8 can encode"
NOT_A_PROVIDER = "must be " + " or ".join(AI_PROVIDERS)
# The problem a field this kind does not take is reported under. The name
# a caller sent is never repeated: it is their text, and may be a value.
FIELDS = "fields"


def unknown_field_problem(kind: CredentialKind) -> str:
    return "may only be " + ", ".join(KIND_FIELDS[kind].fields)


class CredentialRefused(ValueError):
    """A submitted credential breaks the field rule. Each problem names the
    field and the rule, never the value, nor a field name the kind does not
    have."""

    def __init__(self, problems: list[tuple[str, str]]) -> None:
        super().__init__("; ".join(f"{field} {problem}" for field, problem in problems))
        self.problems = problems


def field_problem(value: str, *, optional: bool = False) -> str | None:
    """The rule `value` breaks once stripped, or None. A field becomes a
    process variable of a run (spec 106), so a control character, NUL
    and newline included, is refused. Empty is allowed only when optional.
    Nothing here raises, so no error can carry the value."""
    stripped = value.strip()
    if not stripped:
        return None if optional else EMPTY
    if any(unicodedata.category(character) == "Cs" for character in stripped):
        return NOT_UTF8
    if len(stripped.encode("utf-8")) > FIELD_MAX_BYTES:
        return TOO_LONG
    if any(unicodedata.category(character) == "Cc" for character in stripped):
        return CONTROL_CHARACTER
    return None


def checked_fields(kind: CredentialKind, fields: Mapping[str, str | None]) -> dict[str, str]:
    """`fields` stripped, with an optional field left out when empty.

    Raises CredentialRefused naming each field that breaks the rule or is
    missing, and, under `fields`, any field the kind does not take, without
    its name. Spec 109's Telegram route writes through this, with no request
    model in front of it.
    """
    declared = KIND_FIELDS[kind]
    problems: list[tuple[str, str]] = []
    if any(name not in declared.fields for name in fields):
        problems.append((FIELDS, unknown_field_problem(kind)))
    kept: dict[str, str] = {}
    for name in declared.fields:
        optional = name in declared.optional
        value = fields.get(name) or ""
        problem = field_problem(value, optional=optional)
        if problem is None and name == "provider" and value.strip() not in AI_PROVIDERS:
            problem = NOT_A_PROVIDER
        if problem is not None:
            problems.append((name, problem))
        elif value.strip():
            kept[name] = value.strip()
    if problems:
        raise CredentialRefused(problems)
    return kept


# --- the key ---

KEY_SHAPE = "<key id>:<base64 of 32 bytes>"
KEY_BYTES = 32
# A key id is recorded on each row and may be logged (spec 106), so it is
# plain text: letters, digits, dot, underscore and hyphen.
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
NONCE_BYTES = 12


class CredentialsKey:
    """The key that encrypts every owner's credentials, and its id.

    Its text form names the id only, so a key that reaches a log record or
    an error message by `repr` or `str` leaks nothing."""

    __slots__ = ("_secret", "key_id")

    def __init__(self, key_id: str, secret: bytes) -> None:
        if not _KEY_ID.fullmatch(key_id) or len(secret) != KEY_BYTES:
            raise ValueError(f"a credentials key is {KEY_SHAPE}")
        self.key_id = key_id
        self._secret = secret

    def __repr__(self) -> str:
        return f"CredentialsKey(key_id={self.key_id!r})"

    __str__ = __repr__

    def aesgcm(self) -> AESGCM:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(self._secret)


def parse_key(text: str) -> CredentialsKey | None:
    """`text` as `<key id>:<base64 of 32 bytes>`, or None when it is
    anything else. Nothing of `text` is raised or returned on refusal."""
    key_id, separator, encoded = text.partition(":")
    if not separator or not _KEY_ID.fullmatch(key_id):
        return None
    secret: bytes | None = None
    try:
        secret = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        secret = None
    if secret is None or len(secret) != KEY_BYTES:
        return None
    return CredentialsKey(key_id, secret)


# --- encryption ---


class CredentialUnreadable(Exception):
    """A stored credential that does not decrypt: the key was replaced, the
    row's key id is unknown, or the ciphertext was moved to another owner's
    row or another kind. The text is spec 106's 409; the kind and key id are
    for its log, never a value or an owner's identity."""

    def __init__(self, kind: str, key_id: str) -> None:
        super().__init__(f"your {kind} credential cannot be read; set it again")
        self.kind = kind
        self.key_id = key_id


@dataclass(frozen=True)
class StoredCredential:
    """One row as the store holds it: ciphertext, never a value."""

    kind: CredentialKind
    key_id: str
    nonce: bytes
    ciphertext: bytes
    updated_at: str


def _owner_form(owner: str) -> str:
    # One spelling of the owner in the associated data, whatever case the
    # claims carried. Raises ValueError for anything that is not a UUID.
    return str(uuid.UUID(owner))


def associated_data(owner: str, kind: str) -> bytes:
    return f"harrier-credential:v1:{_owner_form(owner)}:{kind}".encode()


def encrypt(
    key: CredentialsKey, owner: str, kind: CredentialKind, fields: Mapping[str, str]
) -> tuple[bytes, bytes]:
    """The nonce and ciphertext of `fields` for `owner`'s `kind`."""
    nonce = os.urandom(NONCE_BYTES)
    plain = json.dumps(dict(fields), sort_keys=True, separators=(",", ":")).encode()
    return nonce, key.aesgcm().encrypt(nonce, plain, associated_data(owner, kind))


def decrypt(key: CredentialsKey, owner: str, stored: StoredCredential) -> dict[str, str]:
    """`stored`'s fields for `owner`. Raises CredentialUnreadable when its
    key id is not `key`'s, or it does not decrypt for this owner and kind."""
    from cryptography.exceptions import InvalidTag

    if stored.key_id != key.key_id:
        raise CredentialUnreadable(stored.kind, stored.key_id)
    plain: bytes | None = None
    try:
        plain = key.aesgcm().decrypt(
            stored.nonce, stored.ciphertext, associated_data(owner, stored.kind)
        )
    except (InvalidTag, ValueError):
        plain = None
    if plain is None:
        # Outside the except block, so nothing of the failure is chained.
        raise CredentialUnreadable(stored.kind, stored.key_id)
    return cast(dict[str, str], json.loads(plain))


# --- the store ---


def credential_updates(conn: Connection) -> dict[CredentialKind, str]:
    """When each of the session owner's set kinds was last written. Reads
    `kind` and `updated_at` only, never the ciphertext (spec 116)."""
    rows = conn.execute("SELECT kind, updated_at FROM credentials").fetchall()
    return {cast(CredentialKind, str(row[0])): str(row[1]) for row in rows}


def stored_credentials(conn: Connection) -> list[StoredCredential]:
    """The session owner's ciphertexts, in kind order, for spec 106's run
    start to decrypt."""
    rows = conn.execute(
        "SELECT kind, key_id, nonce, ciphertext, updated_at FROM credentials ORDER BY kind"
    ).fetchall()
    return [
        StoredCredential(
            kind=cast(CredentialKind, str(row[0])),
            key_id=str(row[1]),
            nonce=bytes(row[2]),
            ciphertext=bytes(row[3]),
            updated_at=str(row[4]),
        )
        for row in rows
    ]


def put_credential(
    conn: Connection,
    key: CredentialsKey,
    owner: str,
    kind: CredentialKind,
    fields: Mapping[str, str | None],
) -> str:
    """Encrypt `fields` for `owner` and store them as the session owner's
    `kind`, replacing what was there. Returns the time written.

    `owner` is the session's owner, which the row policy stores the row
    under; it is passed for the associated data only. Raises
    CredentialRefused, before anything is written, for a field that breaks
    the rule.
    """
    kept = checked_fields(kind, fields)
    nonce, ciphertext = encrypt(key, owner, kind, kept)
    written = store_now()
    update_or_insert(
        conn,
        "UPDATE credentials SET key_id = ?, nonce = ?, ciphertext = ?, updated_at = ? "
        "WHERE kind = ?",
        (key.key_id, nonce, ciphertext, written, kind),
        "INSERT INTO credentials (kind, key_id, nonce, ciphertext, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, key.key_id, nonce, ciphertext, written),
    )
    return written


def delete_credential(conn: Connection, kind: CredentialKind) -> None:
    """Remove the session owner's `kind`; nothing happens when none is set."""
    conn.execute("DELETE FROM credentials WHERE kind = ?", (kind,))
