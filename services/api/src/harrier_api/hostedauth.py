"""Hosted mode and the bearer token (spec 104).

The local API knows no caller: there is one, the operator, and spec 035's
guards (`localauth.py`) prove a request came from their own browser. The
hosted API serves many owners from the internet, so every request but
`GET /health` carries a Supabase access token in its `Authorization`
header, and this module decides whether it is good.

It holds four things: the configuration reader, which turns
`HARRIER_AUTH=supabase` and its variables into a `HostedConfig` or a refusal
to start; the signing key cache; the token verifier; and the ASGI middleware
that refuses a request before routing, so a route cannot forget the check
and a refused request never reaches a dependency that opens a connection.

PyJWT is imported where a token is verified, never at module level. The
configuration reader runs on every start, local ones included, and the
image installs no `hosted` group.

The verified claims are where this module stops. Each request's database
identity, the pool and the start checks are `harrier_api.hostedstore`'s
(spec 134).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from harrier.connection import URL_VARIABLE, StoreUrlError, store_target
from harrier.credentials import KEY_SHAPE, CredentialsKey, parse_key
from harrier.demo import DEMO_ENV
from harrier.llm.config import DEBUG_VARIABLES, debug_value_on

if TYPE_CHECKING:
    from jwt import PyJWK

    from harrier.filestore import FileStore

logger = logging.getLogger(__name__)

# --- configuration ---

AUTH_VARIABLE = "HARRIER_AUTH"
SUPABASE_URL_VARIABLE = "HARRIER_SUPABASE_URL"
ALLOWED_HOSTS_VARIABLE = "HARRIER_ALLOWED_HOSTS"
CORS_ORIGINS_VARIABLE = "HARRIER_CORS_ORIGINS"
# The key that encrypts every owner's credentials, a Fly secret (spec 116).
CREDENTIALS_KEY_VARIABLE = "HARRIER_CREDENTIALS_KEY"
# The run login's URL: a second login role, which a hosted run receives as
# its `HARRIER_DATABASE_URL` (spec 106). The name is `harrier_api.runenv`'s.
RUN_DATABASE_URL_VARIABLE = "HARRIER_RUN_DATABASE_URL"
# The `apikey` header of every Storage request (spec 107, Configuration).
PUBLISHABLE_KEY_VARIABLE = "HARRIER_SUPABASE_PUBLISHABLE_KEY"
PUBLISHABLE_KEY_PREFIX = "sb_publishable_"
HOSTED_AUTH = "supabase"

# A service credential on a request path is the service role on a request
# path: run subprocesses inherit the environment (spec 035).
SERVICE_ROLE_KEY_VARIABLE = "SUPABASE_SERVICE_ROLE_KEY"
SECRET_KEY_PREFIX = "sb_secret_"

# The operator's own notification settings. `send_telegram_message` falls
# back to the first two, so a run that inherited them would send every
# owner's notifications to the operator's chat; Gmail is not offered hosted
# at all. Each owner's Telegram settings are their own (spec 109).
OPERATOR_NOTIFICATION_VARIABLES = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
GMAIL_VARIABLE_PREFIX = "GMAIL_"

AUTH_VALUE_REFUSAL = f"{AUTH_VARIABLE} must be empty or {HOSTED_AUTH}"
NEEDS_POSTGRES_REFUSAL = (
    f"{AUTH_VARIABLE}={HOSTED_AUTH} needs {URL_VARIABLE} to name a Postgres store"
)
# The other half of the same rule: the API serves Postgres only hosted, where
# each request runs as its user (spec 134, The gate).
POSTGRES_NEEDS_HOSTED_REFUSAL = (
    f"the API serves a Postgres store only with {AUTH_VARIABLE}={HOSTED_AUTH} (spec 104)"
)
DEMO_REFUSAL = (
    f"{DEMO_ENV} cannot run with {AUTH_VARIABLE}={HOSTED_AUTH}: demo mode seeds a local SQLite file"
)
GROUP_REFUSAL = (
    f"{AUTH_VARIABLE}={HOSTED_AUTH} needs the hosted dependencies: run uv sync --group hosted"
)
SUPABASE_URL_REFUSAL = (
    f"{SUPABASE_URL_VARIABLE} must be an https:// URL that names a host and nothing else"
)
ALLOWED_HOSTS_REFUSAL = (
    f"{ALLOWED_HOSTS_VARIABLE} must list one or more host names, separated by commas, "
    "with no port and no wildcard"
)
CORS_ORIGINS_REFUSAL = (
    f"{CORS_ORIGINS_VARIABLE} must list one or more exact https:// origins, separated by "
    "commas, with no wildcard and no path"
)
# One text for a key that is absent, of another form, or not 32 bytes: it
# names the variable and the form, never the value (spec 116).
CREDENTIALS_KEY_REFUSAL = f"{CREDENTIALS_KEY_VARIABLE} must be {KEY_SHAPE}"
# One text for a run URL that is absent, empty or not a Postgres URL: it
# names the variable, never the value (spec 106).
RUN_DATABASE_URL_REFUSAL = (
    f"{AUTH_VARIABLE}={HOSTED_AUTH} needs {RUN_DATABASE_URL_VARIABLE}, "
    "a postgresql:// URL for the run login role"
)
# One text for a key that is absent, empty, a legacy JWT or anything else not
# beginning `sb_publishable_`: it names the variable and the form, never the
# value (spec 107, Failure modes). A legacy service_role key has the shape of
# a legacy anon key, so no legacy key is accepted.
PUBLISHABLE_KEY_REFUSAL = (
    f"hosted mode needs {PUBLISHABLE_KEY_VARIABLE}, a key beginning {PUBLISHABLE_KEY_PREFIX}"
)

# A host name as the trusted-host middleware compares it: the Host header's
# text before the first colon. A port or a wildcard would make an entry that
# never matches or matches everything (spec 084's lesson).
_HOST_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


class HostedConfigError(RuntimeError):
    """The hosted configuration is refused. The text names a variable, never
    its value: a value may be a URL, a key or a secret (spec 104)."""


@dataclass(frozen=True)
class HostedConfig:
    supabase_url: str
    allowed_hosts: tuple[str, ...]
    cors_origins: tuple[str, ...]
    # Its text form names the key id only; kept out of the config's repr
    # all the same (spec 116).
    credentials_key: CredentialsKey = field(repr=False)
    # The run login's URL, password included: kept out of the repr (spec
    # 106). Its role is checked when the store opens.
    run_database_url: str = field(repr=False)
    # Public by design (the SPA's bundle holds it), and still kept out of
    # the repr, as every key here is (spec 107).
    publishable_key: str = field(repr=False, default="")

    @property
    def issuer(self) -> str:
        return f"{self.supabase_url}/auth/v1"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"


# The hosted app and the run manager read hosted mode from two places: the
# app from how it was built, the run manager from the process environment,
# because `write_run_input` has no request. Building the hosted app refuses
# unless the two agree (spec 132).
HOSTED_APP_REFUSAL = (
    f"the hosted app is built only with {AUTH_VARIABLE}={HOSTED_AUTH} in the process environment"
)


def hosted_mode(environ: Mapping[str, str]) -> bool:
    """Whether this process serves hosted: `HARRIER_AUTH=supabase`, nothing
    inferred. Pass the process environment. A process with any other
    non-empty value never serves, because `hosted_config` refuses it at
    start. The run manager reads this where it has no request (spec 132)."""
    return environ.get(AUTH_VARIABLE, "") == HOSTED_AUTH


def hosted_config(environ: Mapping[str, str]) -> HostedConfig | None:
    """The hosted configuration, or None in local mode.

    Pass the process environment. Hosted mode is never read from a `.env`
    file: on Fly there is none, and a file in the working directory must not
    switch an API into hosted mode (spec 104, Configuration). Local mode
    reads none of the hosted variables, so a developer's shell cannot leave
    the local API half hosted.
    """
    mode = environ.get(AUTH_VARIABLE, "")
    if mode == "":
        return None
    if mode != HOSTED_AUTH:
        raise HostedConfigError(AUTH_VALUE_REFUSAL)
    if environ.get(DEMO_ENV, "") == "1":
        raise HostedConfigError(DEMO_REFUSAL)
    if not _hosted_group_installed():
        raise HostedConfigError(GROUP_REFUSAL)
    # A malformed URL raises StoreUrlError with its own text, as locally.
    if not store_target(environ).is_postgres:
        raise HostedConfigError(NEEDS_POSTGRES_REFUSAL)
    config = HostedConfig(
        supabase_url=_supabase_url(environ.get(SUPABASE_URL_VARIABLE, "")),
        allowed_hosts=_allowed_hosts(environ.get(ALLOWED_HOSTS_VARIABLE, "")),
        cors_origins=_cors_origins(environ.get(CORS_ORIGINS_VARIABLE, "")),
        credentials_key=_credentials_key(environ.get(CREDENTIALS_KEY_VARIABLE, "")),
        run_database_url=_run_database_url(environ.get(RUN_DATABASE_URL_VARIABLE, "")),
        publishable_key=_publishable_key(environ.get(PUBLISHABLE_KEY_VARIABLE, "")),
    )
    _refuse_a_service_credential(environ)
    _refuse_prompt_logging(environ)
    _refuse_operator_notification_settings(environ)
    return config


def _run_database_url(raw: str) -> str:
    """`raw` when it is a Postgres URL `store_target` accepts. Refused, with
    one text naming the variable, when it is anything else: the URL's own
    refusal names `HARRIER_DATABASE_URL`, and the value is never quoted."""
    target = None
    try:
        target = store_target({URL_VARIABLE: raw})
    except StoreUrlError:
        target = None
    if target is None or not target.is_postgres:
        raise HostedConfigError(RUN_DATABASE_URL_REFUSAL)
    return raw


def _publishable_key(raw: str) -> str:
    """The key when it begins `sb_publishable_` and has more after it;
    otherwise refused with one text that never quotes it."""
    key = raw.strip()
    if not key.startswith(PUBLISHABLE_KEY_PREFIX) or key == PUBLISHABLE_KEY_PREFIX:
        raise HostedConfigError(PUBLISHABLE_KEY_REFUSAL)
    return key


def _credentials_key(raw: str) -> CredentialsKey:
    key = parse_key(raw)
    if key is None:
        raise HostedConfigError(CREDENTIALS_KEY_REFUSAL)
    return key


def _hosted_group_installed() -> bool:
    try:
        import cryptography  # noqa: F401  # pyright: ignore[reportUnusedImport]
        import httpx  # noqa: F401  # pyright: ignore[reportUnusedImport]
        import jwt  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        return False
    return True


def _supabase_url(raw: str) -> str:
    if not raw:
        raise HostedConfigError(f"{AUTH_VARIABLE}={HOSTED_AUTH} needs {SUPABASE_URL_VARIABLE}")
    # The issuer is this URL plus /auth/v1, compared exactly, so anything
    # but a host (credentials, a path, a query) would make an issuer no
    # token carries.
    origin = _https_origin(raw.removesuffix("/"))
    if origin is None:
        raise HostedConfigError(SUPABASE_URL_REFUSAL)
    return origin


def _listed(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _allowed_hosts(raw: str) -> tuple[str, ...]:
    hosts = _listed(raw)
    if not hosts or not all(_HOST_NAME.match(host) for host in hosts):
        raise HostedConfigError(ALLOWED_HOSTS_REFUSAL)
    return tuple(hosts)


def _cors_origins(raw: str) -> tuple[str, ...]:
    origins = _listed(raw)
    if not origins or not all(_is_exact_origin(origin) for origin in origins):
        raise HostedConfigError(CORS_ORIGINS_REFUSAL)
    return tuple(origins)


def _is_exact_origin(origin: str) -> bool:
    """`https://host` or `https://host:port`, as a browser sends `Origin`.
    The comparison is exact, so a path or a pattern could never match, and
    a wildcard is refused rather than read as a host (spec 104, Open
    decision 7)."""
    return "*" not in origin and _https_origin(origin) == origin


def _https_origin(text: str) -> str | None:
    """The text as `https://host[:port]`, or None when it is anything else."""
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    # Rebuilt, then compared with the text: any other scheme, credentials,
    # a path, a query or a fragment makes the two differ.
    netloc = parts.hostname if port is None else f"{parts.hostname}:{port}"
    origin = f"https://{netloc}"
    return origin if text == origin else None


def _refuse_a_service_credential(environ: Mapping[str, str]) -> None:
    if SERVICE_ROLE_KEY_VARIABLE in environ:
        raise HostedConfigError(_service_credential_refusal(SERVICE_ROLE_KEY_VARIABLE))
    for name in sorted(environ):
        if environ[name].strip().startswith(SECRET_KEY_PREFIX):
            raise HostedConfigError(_service_credential_refusal(name))


def _refuse_operator_notification_settings(environ: Mapping[str, str]) -> None:
    """Refuse a start whose environment holds the operator's Telegram or
    any Gmail setting, set or empty, naming it and never its value (spec
    109). Spec 106 builds a run's environment from an allowlist; this makes
    the value absent in the first place."""
    for name in sorted(environ):
        if name in OPERATOR_NOTIFICATION_VARIABLES or name.startswith(GMAIL_VARIABLE_PREFIX):
            raise HostedConfigError(operator_notification_refusal(name))


def operator_notification_refusal(name: str) -> str:
    return f"the hosted API must not hold {name}; notification settings are per owner (spec 109)"


def _service_credential_refusal(name: str) -> str:
    return (
        f"the environment holds a service credential in {name}; "
        "the hosted API refuses to start with one"
    )


def prompt_logging_refusal(name: str) -> str:
    return f"prompt logging ({name}) is local only; unset it for the hosted API"


def _refuse_prompt_logging(environ: Mapping[str, str]) -> None:
    """Prompt logging writes every prompt and output under the data
    directory: hosted, tenants' application content on a shared machine's
    disk. The first variable that is on is named, never its value (spec
    135, Amendment (2026-10-13): prompt logging and query strings)."""
    for name in DEBUG_VARIABLES:
        if debug_value_on(environ.get(name, "")):
            raise HostedConfigError(prompt_logging_refusal(name))


# --- routes: what the hosted API serves, and what needs a token ---

# The one route a caller reaches without a token. It opens nothing hosted.
PUBLIC_ROUTES = frozenset({("GET", "/health")})
# Not served hosted (spec 104, What the hosted API serves): the local token
# and the capture confirmation page have no hosted meaning, and the contract
# is in the repository. Any method on these paths is answered 404.
NOT_SERVED_PATHS = frozenset(
    {"/session", "/capture/add-form", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
)
# `POST /capture/add` is served, with a bearer token; its GET is not.
NOT_SERVED_ROUTES = frozenset({("GET", "/capture/add")})


def _as_get(method: str) -> str:
    # Starlette answers HEAD on every GET route.
    return "GET" if method.upper() == "HEAD" else method.upper()


def not_served(method: str, path: str) -> bool:
    return path in NOT_SERVED_PATHS or (_as_get(method), path) in NOT_SERVED_ROUTES


def needs_token(method: str, path: str) -> bool:
    """Whether the hosted API answers this route only with a verified token:
    every route it serves but the public one."""
    return not not_served(method, path) and (method.upper(), path) not in PUBLIC_ROUTES


# --- refusals ---

AuthReason = Literal["missing", "invalid", "expired", "anonymous", "keys_unavailable"]


class AuthRefusalOut(BaseModel):
    """Why the hosted API refused a request's token (spec 104). `detail` is
    a fixed sentence per reason, and never quotes the token, a claim, or the
    verifier's words. The local API never sends it."""

    detail: str
    reason: AuthReason


@dataclass(frozen=True)
class _RefusalShape:
    status: int
    detail: str
    challenge: str | None


_INVALID_TOKEN = 'Bearer error="invalid_token"'

REFUSALS: dict[AuthReason, _RefusalShape] = {
    "missing": _RefusalShape(401, "This request carries no bearer token.", "Bearer"),
    "invalid": _RefusalShape(401, "The bearer token could not be verified.", _INVALID_TOKEN),
    "expired": _RefusalShape(401, "The bearer token has expired.", _INVALID_TOKEN),
    "anonymous": _RefusalShape(403, "An anonymous session cannot use the hosted API.", None),
    "keys_unavailable": _RefusalShape(
        503, "The signing keys cannot be fetched now. Try again shortly.", None
    ),
}

AUTH_401_DESCRIPTION = "the bearer token is missing, invalid or expired (hosted, spec 104)"
KEYS_UNAVAILABLE_DESCRIPTION = "the hosted API cannot fetch its signing keys (spec 104)"
ANONYMOUS_403_DESCRIPTION = "the session is anonymous (hosted, spec 104)"
BEARER_SCHEME_NAME = "bearer"
BEARER_SECURITY_SCHEME: dict[str, str] = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "JWT",
}


class TokenRefused(Exception):
    def __init__(self, reason: AuthReason) -> None:
        super().__init__(reason)
        self.reason: AuthReason = reason


def refusal_response(reason: AuthReason) -> JSONResponse:
    shape = REFUSALS[reason]
    headers = {} if shape.challenge is None else {"WWW-Authenticate": shape.challenge}
    body = AuthRefusalOut(detail=shape.detail, reason=reason)
    return JSONResponse(status_code=shape.status, content=body.model_dump(), headers=headers)


# --- signing keys ---

# Chosen values, not measured ones (spec 104, Honest limitations). Supabase
# caches the key set at its edge for 10 minutes and advises the application
# not to cache longer; 5 here keeps the two together inside that.
CACHE_SECONDS = 300.0
# A `kid` the cache does not hold forces at most one fetch per this long, so
# a stream of forged key ids cannot make the API a load generator against
# the key endpoint. A failed fetch waits as long before the next.
REFRESH_COOLDOWN_SECONDS = 30.0
# How long the last good set keeps verifying while the endpoint is down:
# Supabase's default access token lifetime.
STALE_LIMIT_SECONDS = 3600.0
FETCH_TIMEOUT_SECONDS = 5.0
# Clock skew allowed on `exp` and `iat`, either way.
LEEWAY_SECONDS = 30

ALGORITHMS = ("ES256", "RS256")

KeyFetcher = Callable[[], object]
Clock = Callable[[], float]


class KeysUnavailable(Exception):
    """No key set good enough to judge this token: answer 503, never 401,
    because the token may be valid and a 401 signs the person out."""


class UnknownKey(Exception):
    """A fresh key set holds no key with this id."""


def fetch_from(url: str) -> KeyFetcher:
    def fetch() -> object:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
            return json.loads(response.read())

    return fetch


def _parse_key_set(document: object) -> dict[str, PyJWK]:
    from jwt import PyJWK
    from jwt.exceptions import PyJWTError

    if not isinstance(document, dict):
        raise ValueError("the key set is not a JSON object")
    entries = cast(dict[str, object], document).get("keys")
    if not isinstance(entries, list):
        raise ValueError("the key set has no keys list")
    keys: dict[str, PyJWK] = {}
    for entry in cast(list[object], entries):
        if not isinstance(entry, dict):
            continue
        data = cast(dict[str, Any], entry)
        kid = data.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        # An entry PyJWT cannot read is skipped, not a failed fetch. A key of
        # another algorithm can verify nothing: the header `alg` must be
        # ES256 or RS256, and PyJWT refuses an `alg` that differs from the
        # key's own.
        try:
            keys[kid] = PyJWK(data)
        except PyJWTError:
            continue
    return keys


class SigningKeys:
    """The project's published signing keys, cached (spec 104, Keys).

    The fetch runs outside the lock, by one thread at a time. While it runs,
    a token whose key the cache holds is answered from the cache, within the
    hour the last good set is trusted, so a slow or unreachable endpoint
    holds no request that does not need it. A token whose key the cache
    lacks waits for the fetch in flight, which may be bringing that key, at
    most a little longer than the fetch's own timeout.
    """

    def __init__(self, fetch: KeyFetcher, clock: Clock, *, source: str = "") -> None:
        self._fetch = fetch
        self._clock = clock
        self._source = source
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._forced_at: float | None = None
        self._last_failed = False
        self._refreshing = False

    def prime(self) -> None:
        """The fetch at start. A failure is logged and the API starts anyway:
        authenticated routes answer 503 until a fetch succeeds."""
        with self._lock:
            now = self._clock()
            self._claim(now)
        self._refresh(now)

    def key_for(self, kid: str) -> PyJWK:
        with self._lock:
            now = self._clock()
            fetched_at = self._fetched_at
            stale = fetched_at is None or now - fetched_at >= CACHE_SECONDS
            unknown = kid not in self._keys
            forced = unknown and not stale
            fetch = (
                (stale or unknown) and not self._refreshing and self._may_fetch(now, forced=forced)
            )
            if fetch:
                if forced:
                    self._forced_at = now
                self._claim(now)
        if fetch:
            self._refresh(now)
        with self._lock:
            if kid not in self._keys:
                # The fetch in flight may be bringing this key.
                self._settled.wait_for(
                    lambda: not self._refreshing, timeout=FETCH_TIMEOUT_SECONDS + 1
                )
            return self._answer(kid, now)

    def _answer(self, kid: str, now: float) -> PyJWK:
        fetched_at = self._fetched_at
        if fetched_at is None or now - fetched_at > STALE_LIMIT_SECONDS:
            raise KeysUnavailable
        key = self._keys.get(kid)
        if key is not None:
            return key
        if self._last_failed:
            # The endpoint is down, so this may be a new key.
            raise KeysUnavailable
        raise UnknownKey

    def _may_fetch(self, now: float, *, forced: bool) -> bool:
        attempted = self._attempted_at
        if self._last_failed and attempted is not None:
            return now - attempted >= REFRESH_COOLDOWN_SECONDS
        if forced:
            return self._forced_at is None or now - self._forced_at >= REFRESH_COOLDOWN_SECONDS
        return True

    def _claim(self, now: float) -> None:
        """Make this thread the one that fetches. Called holding the lock."""
        self._refreshing = True
        self._attempted_at = now

    def _refresh(self, now: float) -> None:
        """The fetch, without the lock; its result is applied under it."""
        keys: dict[str, PyJWK] | None = None
        failure = ""
        try:
            keys = _parse_key_set(self._fetch())
        except Exception as error:
            failure = type(error).__name__
        finally:
            with self._lock:
                if keys is None:
                    self._last_failed = True
                else:
                    self._keys = keys
                    self._fetched_at = now
                    self._last_failed = False
                self._refreshing = False
                self._settled.notify_all()
        if keys is None:
            # The source names the host and path, never a query; the
            # exception's own text could quote a response, so only its type.
            logger.warning(
                "the signing key set could not be fetched from %s: %s",
                self._source or "the configured key endpoint",
                failure,
            )


# --- the token ---


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise TokenRefused("missing")
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise TokenRefused("missing")
    return token


class TokenVerifier:
    """Every check under spec 104's "The token", in its order."""

    def __init__(self, config: HostedConfig, keys: SigningKeys) -> None:
        self._issuer = config.issuer
        self._keys = keys

    def verify(self, authorization: str | None) -> dict[str, Any]:
        import jwt

        token = _bearer_token(authorization)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenRefused("invalid") from None
        algorithm = header.get("alg")
        kid = header.get("kid")
        # `HS256` and `none` are refused here, before any key is looked up.
        if algorithm not in ALGORITHMS or not isinstance(kid, str) or not kid:
            raise TokenRefused("invalid")
        try:
            key = self._keys.key_for(kid)
        except KeysUnavailable:
            raise TokenRefused("keys_unavailable") from None
        except UnknownKey:
            raise TokenRefused("invalid") from None
        # PyJWT verifies with the key's own algorithm and refuses a header
        # `alg` that differs from it, so a key cannot be used as another.
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience="authenticated",
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise TokenRefused("expired") from None
        except jwt.PyJWTError:
            raise TokenRefused("invalid") from None
        if claims.get("role") != "authenticated":
            raise TokenRefused("invalid")
        if not _is_uuid(claims.get("sub")):
            raise TokenRefused("invalid")
        anonymous = claims.get("is_anonymous", False)
        if anonymous is True:
            raise TokenRefused("anonymous")
        if anonymous is not False:
            raise TokenRefused("invalid")
        return claims


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


# --- the middleware ---


# What binds a verified request's file store: its `sub` and its bearer
# token in, the store out (spec 107, "Binding a request").
StoreBinder = Callable[[str, str], "FileStore"]


def storage_binder(config: HostedConfig) -> StoreBinder:
    """The request's `SupabaseFileStore`: its owner, its own token, the
    project's URL and the publishable key."""
    from harrier.filestore import SupabaseFileStore

    def bind(owner: str, token: str) -> FileStore:
        return SupabaseFileStore.for_request(
            owner, token, origin=config.supabase_url, publishable_key=config.publishable_key
        )

    return bind


class HostedAuthMiddleware:
    """Refuses before routing (spec 104, Scope item 4).

    A route not served hosted is 404 whoever asks. `GET /health` passes.
    Every other request needs a verified token in its `Authorization`
    header; no other place a token could travel is read. A refused request
    is logged with its reason, method and path, never its header or claims.
    The verified payload, as signed, is the request's `claims` state.

    Once the token verifies, the request's file store is bound for the rest
    of the request and reset when its response ends (spec 107). Bound here,
    before routing, so a synchronous route that runs in the thread pool
    inherits it.
    """

    def __init__(
        self, app: ASGIApp, verifier: TokenVerifier, binder: StoreBinder | None = None
    ) -> None:
        self.app = app
        self.verifier = verifier
        self.binder = binder

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # A browser cannot send an `Authorization` header on a websocket
            # handshake, so none is served hosted: a route added later
            # cannot skip the token this way.
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = cast(str, scope["method"])
        path = cast(str, scope["path"])
        if not_served(method, path):
            await JSONResponse(status_code=404, content={"detail": "Not Found"})(
                scope, receive, send
            )
            return
        if not needs_token(method, path):
            await self.app(scope, receive, send)
            return
        authorization = Headers(scope=scope).get("authorization")
        try:
            claims = await run_in_threadpool(self.verifier.verify, authorization)
        except TokenRefused as refused:
            # The path is percent-decoded, so a control character in it is
            # escaped rather than written into the record as a new line.
            shown = path.encode("unicode_escape").decode("ascii")
            logger.info("refused %s %s: %s", method, shown, refused.reason)
            await refusal_response(refused.reason)(scope, receive, send)
            return
        state = cast(dict[str, Any], scope.get("state") or {})
        forwarded: Scope = {**scope, "state": {**state, "claims": claims}}
        if self.binder is None:
            # Only spec 104's and spec 132's tests build the middleware
            # without a binder: files() then refuses, as unbound.
            await self.app(forwarded, receive, send)
            return
        from harrier.filestore import bind_files

        # The verifier proved both: a string `sub` that is a UUID, and the
        # token it came in.
        store = self.binder(str(claims["sub"]), _bearer_token(authorization))
        with bind_files(store):
            await self.app(forwarded, receive, send)
