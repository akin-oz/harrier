"""Hosted mode and the bearer token (spec 104).

The hosted app cannot start under spec 104 alone: `create_app` still
refuses a Postgres store with spec 103's text until spec 134. So these tests
build it with `create_hosted_app`, the assembly step `create_app` calls
after that gate, with a key fetcher that serves keys made here and a clock
the test moves. No test reaches the network or a database: `get_conn` is
replaced by a spy that records whether a request reached it.

Every token, key and secret here is made at run time.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from starlette.middleware.cors import SAFELISTED_HEADERS
from starlette.websockets import WebSocket, WebSocketDisconnect

from harrier.connection import POSTGRES_NOT_YET, Connection
from harrier.demo import repo_root
from harrier_api import app as app_module
from harrier_api.app import create_app, create_hosted_app
from harrier_api.deps import get_conn
from harrier_api.hostedauth import (
    ALLOWED_HOSTS_REFUSAL,
    CORS_ORIGINS_REFUSAL,
    DEMO_REFUSAL,
    GROUP_REFUSAL,
    SUPABASE_URL_REFUSAL,
    HostedAuthMiddleware,
    HostedConfig,
    hosted_config,
    needs_token,
)
from harrier_api.localauth import TOKEN_HEADER, require_token
from harrier_api.runmodels import get_manager

SUPABASE_URL = "https://project-ref.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"
API_HOST = "api.example.test"
SPA_ORIGIN = "https://app.example.test"
KID = "current-key"
CONTRACT = repo_root() / "packages" / "contract" / "openapi.json"

# The two texts spec 104 writes out under Failure modes, written out here
# too, so a changed constant fails. The other refusals' texts are the code's
# constants, and the spec asks only that each names its variable, which
# each start case checks.
AUTH_VALUE_TEXT = "HARRIER_AUTH must be empty or supabase"
NEEDS_POSTGRES_TEXT = "HARRIER_AUTH=supabase needs HARRIER_DATABASE_URL to name a Postgres store"


# --- keys, tokens and the app ---


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class KeyServer:
    """The project's key endpoint, served from memory."""

    published: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    calls: int = 0
    failing: bool = False
    # Served instead of the key set when set: a malformed answer.
    document: object = None
    # Entries served beside the keys, which no key reader can use.
    extra: list[object] = field(default_factory=list[object])

    def publish(self, kid: str, key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey) -> None:
        if isinstance(key, ec.EllipticCurvePrivateKey):
            jwk = ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
        else:
            jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        self.published[kid] = {**jwk, "kid": kid}

    def __call__(self) -> object:
        self.calls += 1
        if self.failing:
            raise OSError(f"the key endpoint is unreachable {UNREACHABLE_SENTINEL}")
        if self.document is not None:
            return self.document
        return {"keys": [*self.published.values(), *self.extra]}


# In the fetch error's own text, which the log must not carry.
UNREACHABLE_SENTINEL = f"sentinel-{uuid.uuid4().hex}"


def new_ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


DROP = object()


def claims_for(**overrides: object) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": "authenticated",
        "role": "authenticated",
        "sub": str(uuid.uuid4()),
        "exp": now + 3600,
        "iat": now,
        "session_id": str(uuid.uuid4()),
        "email": f"owner-{uuid.uuid4().hex[:8]}@example.test",
        "is_anonymous": False,
    }
    for name, value in overrides.items():
        if value is DROP:
            claims.pop(name, None)
        else:
            claims[name] = value
    return claims


def mint(key: object, claims: dict[str, Any], *, kid: str = KID, alg: str = "ES256") -> str:
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})  # pyright: ignore[reportArgumentType]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Hosted:
    app: FastAPI
    client: TestClient
    server: KeyServer
    clock: Clock
    key: ec.EllipticCurvePrivateKey
    entered: list[dict[str, Any]]

    def token(self, **overrides: object) -> str:
        return mint(self.key, claims_for(**overrides))


CONFIG = HostedConfig(
    supabase_url=SUPABASE_URL, allowed_hosts=(API_HOST,), cors_origins=(SPA_ORIGIN,)
)


def build(
    *,
    config: HostedConfig = CONFIG,
    failing_at_start: bool = False,
) -> Hosted:
    server = KeyServer()
    key = new_ec_key()
    server.publish(KID, key)
    server.failing = failing_at_start
    clock = Clock()
    app = create_hosted_app(config, fetch_keys=server, clock=clock)
    entered: list[dict[str, Any]] = []

    def spy(request: Request) -> Iterator[Connection]:
        # Records the request's verified claims, then opens the test's own
        # SQLite store, so a request that passes is served for real.
        entered.append(dict(request.state.claims))
        yield from get_conn()

    app.dependency_overrides[get_conn] = spy
    client = TestClient(app, base_url=f"http://{config.allowed_hosts[0]}")
    return Hosted(app, client, server, clock, key, entered)


@pytest.fixture
def hosted() -> Hosted:
    return build()


def assert_refused(response: Any, status: int, reason: str, challenge: str | None) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["reason"] == reason, body
    assert set(body) == {"detail", "reason"}, body
    assert response.headers.get("www-authenticate") == challenge


INVALID = 'Bearer error="invalid_token"'


def api_routes(routes: Sequence[object]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from api_routes(original.routes)


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


# --- local mode ---


def test_local_mode_has_no_hosted_middleware() -> None:
    app = create_app()
    installed = {middleware.cls for middleware in app.user_middleware}
    assert HostedAuthMiddleware not in installed
    assert not any(
        isinstance(cls, type) and cls.__name__.endswith("CORSMiddleware") for cls in installed
    ), installed
    # A cross-origin request gets no CORS headers, which `GET /session`
    # relies on (spec 035), and a read needs no bearer token.
    response = TestClient(app).get("/runs", headers={"Origin": SPA_ORIGIN})
    assert response.status_code == 200
    assert not [name for name in response.headers if name.lower().startswith("access-control-")]


def test_hosted_mode_is_read_from_the_process_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARRIER_AUTH", raising=False)
    (tmp_path / ".env").write_text(
        "HARRIER_AUTH=supabase\n"
        f"HARRIER_SUPABASE_URL={SUPABASE_URL}\n"
        f"HARRIER_ALLOWED_HOSTS={API_HOST}\n"
        f"HARRIER_CORS_ORIGINS={SPA_ORIGIN}\n",
        encoding="utf-8",
    )
    app = create_app()
    assert not getattr(app.state, "hosted", False)
    assert HostedAuthMiddleware not in {middleware.cls for middleware in app.user_middleware}


# --- start-up ---


def child_environment(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """The process environment for an API start, with no hosted variable
    or service credential the developer's shell may hold."""
    environ = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("HARRIER_AUTH", "HARRIER_SUPABASE", "HARRIER_ALLOWED"))
        and not name.startswith(("HARRIER_CORS", "SUPABASE_"))
        and not value.strip().startswith("sb_secret_")
        and name != "HARRIER_DEMO"
    }
    environ["HARRIER_DATA_DIR"] = str(tmp_path / "data")
    environ.update(overrides)
    return environ


def start_api(environ: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    # What uvicorn does with `harrier_api.app:app`: import the module, which
    # builds the app. A refusal raised there ends the process with status 1.
    return subprocess.run(
        [sys.executable, "-c", "import harrier_api.app"],
        env=environ,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@dataclass(frozen=True)
class StartCase:
    overrides: dict[str, str]
    text: str
    variable: str = ""
    shadow_jwt: bool = False


def start_cases(password: str, secret: str) -> dict[str, StartCase]:
    return {
        "auth has another value": StartCase({"HARRIER_AUTH": "Supabase"}, AUTH_VALUE_TEXT),
        "hosted on sqlite": StartCase({"HARRIER_DATABASE_URL": ""}, NEEDS_POSTGRES_TEXT),
        "no supabase url": StartCase(
            {"HARRIER_SUPABASE_URL": ""},
            "HARRIER_AUTH=supabase needs HARRIER_SUPABASE_URL",
            "HARRIER_SUPABASE_URL",
        ),
        "supabase url not https": StartCase(
            {"HARRIER_SUPABASE_URL": "http://project-ref.supabase.co"},
            SUPABASE_URL_REFUSAL,
            "HARRIER_SUPABASE_URL",
        ),
        "supabase url with a path": StartCase(
            {"HARRIER_SUPABASE_URL": f"{SUPABASE_URL}/auth/v1"},
            SUPABASE_URL_REFUSAL,
            "HARRIER_SUPABASE_URL",
        ),
        "supabase url with credentials": StartCase(
            {"HARRIER_SUPABASE_URL": f"https://someone:{password}@project-ref.supabase.co"},
            SUPABASE_URL_REFUSAL,
            "HARRIER_SUPABASE_URL",
        ),
        "no allowed hosts": StartCase(
            {"HARRIER_ALLOWED_HOSTS": " , "}, ALLOWED_HOSTS_REFUSAL, "HARRIER_ALLOWED_HOSTS"
        ),
        "a wildcard host": StartCase(
            {"HARRIER_ALLOWED_HOSTS": f"{API_HOST},*"},
            ALLOWED_HOSTS_REFUSAL,
            "HARRIER_ALLOWED_HOSTS",
        ),
        "a host with a port": StartCase(
            {"HARRIER_ALLOWED_HOSTS": f"{API_HOST}:443"},
            ALLOWED_HOSTS_REFUSAL,
            "HARRIER_ALLOWED_HOSTS",
        ),
        "no cors origins": StartCase(
            {"HARRIER_CORS_ORIGINS": ""}, CORS_ORIGINS_REFUSAL, "HARRIER_CORS_ORIGINS"
        ),
        "a wildcard origin": StartCase(
            {"HARRIER_CORS_ORIGINS": "*"}, CORS_ORIGINS_REFUSAL, "HARRIER_CORS_ORIGINS"
        ),
        "an origin pattern": StartCase(
            {"HARRIER_CORS_ORIGINS": f"{SPA_ORIGIN},https://*.vercel.app"},
            CORS_ORIGINS_REFUSAL,
            "HARRIER_CORS_ORIGINS",
        ),
        "an origin not https": StartCase(
            {"HARRIER_CORS_ORIGINS": "http://app.example.test"},
            CORS_ORIGINS_REFUSAL,
            "HARRIER_CORS_ORIGINS",
        ),
        "an origin with a path": StartCase(
            {"HARRIER_CORS_ORIGINS": f"{SPA_ORIGIN},{SPA_ORIGIN}/"},
            CORS_ORIGINS_REFUSAL,
            "HARRIER_CORS_ORIGINS",
        ),
        "demo mode": StartCase({"HARRIER_DEMO": "1"}, DEMO_REFUSAL, "HARRIER_DEMO"),
        "hosted group not installed": StartCase(
            {}, GROUP_REFUSAL, "uv sync --group hosted", shadow_jwt=True
        ),
        "the service role key": StartCase(
            {"SUPABASE_SERVICE_ROLE_KEY": secret},
            "the environment holds a service credential in SUPABASE_SERVICE_ROLE_KEY; "
            "the hosted API refuses to start with one",
        ),
        "a secret key value": StartCase(
            {"SOME_PLATFORM_KEY": f"sb_secret_{secret}"},
            "the environment holds a service credential in SOME_PLATFORM_KEY; "
            "the hosted API refuses to start with one",
        ),
    }


PASSWORD = f"synthetic{uuid.uuid4().hex}"
SECRET = uuid.uuid4().hex


def complete_hosted_environment() -> dict[str, str]:
    return {
        "HARRIER_AUTH": "supabase",
        "HARRIER_SUPABASE_URL": SUPABASE_URL,
        "HARRIER_ALLOWED_HOSTS": API_HOST,
        "HARRIER_CORS_ORIGINS": SPA_ORIGIN,
        "HARRIER_DATABASE_URL": f"postgresql://harrier_api:{PASSWORD}@127.0.0.1:1/postgres",
    }


@pytest.mark.parametrize("case", sorted(start_cases(PASSWORD, SECRET)))
def test_hosted_start_refuses_incomplete_configuration(case: str, tmp_path: Path) -> None:
    spec = start_cases(PASSWORD, SECRET)[case]
    environ = child_environment(tmp_path, **{**complete_hosted_environment(), **spec.overrides})
    if spec.shadow_jwt:
        shadow = tmp_path / "shadow" / "jwt"
        shadow.mkdir(parents=True)
        (shadow / "__init__.py").write_text("raise ImportError('not installed')\n")
        environ["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(shadow.parent), environ.get("PYTHONPATH", "")) if part
        )

    started = start_api(environ, tmp_path)

    assert started.returncode == 1, started.stderr
    last_line = started.stderr.strip().splitlines()[-1]
    assert last_line == f"harrier_api.hostedauth.HostedConfigError: {spec.text}", started.stderr
    assert spec.variable in spec.text
    printed = started.stdout + started.stderr
    for value in (PASSWORD, SECRET, SUPABASE_URL, "postgresql://", "project-ref"):
        assert value not in printed, (case, value)
    assert not (tmp_path / "data").exists()


def test_a_complete_hosted_configuration_still_meets_the_spec_103_gate(
    tmp_path: Path,
) -> None:
    environ = child_environment(tmp_path, **complete_hosted_environment())
    started = start_api(environ, tmp_path)
    assert started.returncode == 1, started.stderr
    assert started.stderr.strip().splitlines()[-1] == f"RuntimeError: {POSTGRES_NOT_YET}"
    assert PASSWORD not in started.stdout + started.stderr

    # The same configuration, assembled, serves a request with a valid token.
    config = hosted_config(complete_hosted_environment())
    assert config is not None
    hosted = build(config=config)
    response = hosted.client.get("/runs", headers=bearer(hosted.token()))
    assert response.status_code == 200, response.text
    assert response.json() == []


# --- tokens ---


def test_a_request_without_a_token_is_401_and_takes_no_connection(hosted: Hosted) -> None:
    response = hosted.client.get("/jobs")
    assert_refused(response, 401, "missing", "Bearer")
    assert hosted.entered == []

    # The same route with a token reaches `get_conn`, and the request's
    # claims are the verified payload exactly as signed.
    claims = claims_for()
    response = hosted.client.get("/jobs", headers=bearer(mint(hosted.key, claims)))
    assert response.status_code == 200, response.text
    assert hosted.entered == [claims]


@pytest.mark.parametrize(
    "header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer   ", "Token abc", "bearer"]
)
def test_a_header_that_is_not_a_bearer_token_is_missing(hosted: Hosted, header: str) -> None:
    response = hosted.client.get("/jobs", headers={"Authorization": header})
    assert_refused(response, 401, "missing", "Bearer")
    assert hosted.entered == []


def test_a_valid_token_is_accepted_in_either_algorithm(hosted: Hosted) -> None:
    # The scheme name is matched without regard to case, and `aud` may be a
    # list that holds `authenticated`.
    token = hosted.token(aud=["authenticated", "another-audience"])
    assert hosted.client.get("/runs", headers={"Authorization": f"bEaReR {token}"}).is_success

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hosted.server.publish("rsa-key", rsa_key)
    hosted.clock.now += 31
    token = mint(rsa_key, claims_for(), kid="rsa-key", alg="RS256")
    assert hosted.client.get("/runs", headers=bearer(token)).is_success


def test_expiry_honours_the_leeway(hosted: Hosted) -> None:
    now = int(time.time())
    just_expired = hosted.token(exp=now - 10, iat=now - 3600)
    assert hosted.client.get("/runs", headers=bearer(just_expired)).status_code == 200

    expired = hosted.token(exp=now - 120, iat=now - 3600)
    assert_refused(hosted.client.get("/runs", headers=bearer(expired)), 401, "expired", INVALID)

    # `iat` takes the same leeway the other way.
    issued_ahead = hosted.token(iat=now + 10)
    assert hosted.client.get("/runs", headers=bearer(issued_ahead)).status_code == 200


def forged_tokens(hosted: Hosted) -> dict[str, str]:
    now = int(time.time())
    return {
        "wrong signature": mint(new_ec_key(), claims_for()),
        "alg none": mint(None, claims_for(), alg="none"),
        "alg HS256": jwt.encode(
            claims_for(),
            uuid.uuid4().hex + uuid.uuid4().hex,
            algorithm="HS256",
            headers={"kid": KID},
        ),
        "unknown kid": mint(hosted.key, claims_for(), kid="never-published"),
        "no kid": jwt.encode(claims_for(), hosted.key, algorithm="ES256"),
        "foreign iss": hosted.token(iss="https://other-project.supabase.co/auth/v1"),
        "aud anon": hosted.token(aud="anon"),
        "role anon": hosted.token(role="anon"),
        "role service_role": hosted.token(role="service_role"),
        "non-uuid sub": hosted.token(sub="not-a-uuid"),
        "no sub": hosted.token(sub=DROP),
        "no exp": hosted.token(exp=DROP),
        "no iat": hosted.token(iat=DROP),
        "iat in the future": hosted.token(iat=now + 120),
        "is_anonymous not a boolean": hosted.token(is_anonymous="false"),
        "not a token": "not.a.token",
    }


FORGED_CASES = (
    "wrong signature",
    "alg none",
    "alg HS256",
    "unknown kid",
    "no kid",
    "foreign iss",
    "aud anon",
    "role anon",
    "role service_role",
    "non-uuid sub",
    "no sub",
    "no exp",
    "no iat",
    "iat in the future",
    "is_anonymous not a boolean",
    "not a token",
)


@pytest.mark.parametrize("case", FORGED_CASES)
def test_a_forged_or_foreign_token_is_401_invalid(hosted: Hosted, case: str) -> None:
    token = forged_tokens(hosted)[case]
    assert_refused(hosted.client.get("/jobs", headers=bearer(token)), 401, "invalid", INVALID)
    assert hosted.entered == []


def test_an_anonymous_session_is_refused(hosted: Hosted) -> None:
    response = hosted.client.get("/runs", headers=bearer(hosted.token(is_anonymous=True)))
    assert_refused(response, 403, "anonymous", None)
    # Absent, it is not anonymous.
    assert hosted.client.get("/runs", headers=bearer(hosted.token(is_anonymous=DROP))).is_success


def test_a_token_outside_the_header_is_not_read(hosted: Hosted) -> None:
    token = hosted.token()
    client = hosted.client
    for response in (
        client.get(f"/runs?access_token={token}"),
        client.get(f"/runs?token={token}"),
        client.get("/runs", headers={"Cookie": f"access_token={token}; sb-access-token={token}"}),
        client.get("/runs", headers={"X-Harrier-Token": token}),
        client.post("/capture/add", data={"token": token, "access_token": token}),
    ):
        assert_refused(response, 401, "missing", "Bearer")
    assert hosted.entered == []


def test_an_unknown_key_id_refreshes_the_key_set_once(hosted: Hosted) -> None:
    assert hosted.server.calls == 1, "the key set is fetched once at start"
    assert hosted.client.get("/runs", headers=bearer(hosted.token())).is_success
    assert hosted.server.calls == 1

    rotated = new_ec_key()
    hosted.server.publish("next-key", rotated)
    hosted.clock.now += 5
    token = mint(rotated, claims_for(), kid="next-key")
    assert hosted.client.get("/runs", headers=bearer(token)).is_success
    assert hosted.server.calls == 2

    hosted.clock.now += 5
    stranger = mint(new_ec_key(), claims_for(), kid="another-unknown-key")
    response = hosted.client.get("/runs", headers=bearer(stranger))
    assert_refused(response, 401, "invalid", INVALID)
    assert hosted.server.calls == 2, "a second unknown kid within 30 seconds fetched again"

    hosted.clock.now += 30
    assert_refused(hosted.client.get("/runs", headers=bearer(stranger)), 401, "invalid", INVALID)
    assert hosted.server.calls == 3


def test_the_key_set_is_cached_for_five_minutes(hosted: Hosted) -> None:
    token = hosted.token()
    hosted.clock.now += 299
    assert hosted.client.get("/runs", headers=bearer(token)).is_success
    assert hosted.server.calls == 1

    # A revoked key leaves the set; the cached copy expires at five minutes.
    del hosted.server.published[KID]
    hosted.clock.now += 1
    assert_refused(hosted.client.get("/runs", headers=bearer(token)), 401, "invalid", INVALID)
    assert hosted.server.calls == 2


def test_unreachable_keys_answer_503_not_401(hosted: Hosted) -> None:
    held = hosted.token()
    stranger = mint(new_ec_key(), claims_for(), kid="another-unknown-key")
    hosted.server.failing = True

    hosted.clock.now += 400
    assert hosted.client.get("/runs", headers=bearer(held)).is_success
    assert hosted.server.calls == 2, "the expired cache was not refreshed"
    assert_refused(
        hosted.client.get("/runs", headers=bearer(stranger)), 503, "keys_unavailable", None
    )
    # After a fetch that does not succeed, the next waits 30 seconds, so an
    # unknown kid right after one does not fetch again.
    assert hosted.server.calls == 2

    # Within the hour of the last good fetch, the held key still verifies.
    hosted.clock.now = 1_000_000.0 + 3600
    assert hosted.client.get("/runs", headers=bearer(held)).is_success
    assert_refused(
        hosted.client.get("/runs", headers=bearer(stranger)), 503, "keys_unavailable", None
    )

    hosted.clock.now += 1
    assert_refused(hosted.client.get("/runs", headers=bearer(held)), 503, "keys_unavailable", None)

    # `alg` is refused before any key is looked up: no fetch, and 401.
    calls = hosted.server.calls
    hosted.clock.now += 60
    hs256 = jwt.encode(
        claims_for(), uuid.uuid4().hex + uuid.uuid4().hex, algorithm="HS256", headers={"kid": KID}
    )
    assert_refused(hosted.client.get("/runs", headers=bearer(hs256)), 401, "invalid", INVALID)
    assert hosted.server.calls == calls

    hosted.server.failing = False
    assert hosted.client.get("/runs", headers=bearer(held)).is_success


def test_a_failed_first_fetch_still_starts(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="harrier_api.hostedauth")
    hosted = build(failing_at_start=True)

    assert "could not be fetched" in caplog.text
    # The exception's own text could quote a response; only its type is
    # logged.
    assert UNREACHABLE_SENTINEL not in caplog.text
    assert "OSError" in caplog.text
    assert hosted.client.get("/health").status_code == 200
    token = hosted.token()
    assert_refused(hosted.client.get("/runs", headers=bearer(token)), 503, "keys_unavailable", None)

    hosted.server.failing = False
    hosted.clock.now += 31
    assert hosted.client.get("/runs", headers=bearer(token)).is_success


def test_a_malformed_key_set_keeps_the_last_good_one(hosted: Hosted) -> None:
    # A key set that is not one is a failed fetch: the last good set keeps
    # verifying, and an unknown kid is 503, never 401.
    held = hosted.token()
    stranger = mint(new_ec_key(), claims_for(), kid="another-unknown-key")
    for document in ({}, {"keys": "not a list"}, ["not", "an", "object"]):
        hosted.server.document = document
        hosted.clock.now += 400
        assert hosted.client.get("/runs", headers=bearer(held)).is_success, document
        assert_refused(
            hosted.client.get("/runs", headers=bearer(stranger)), 503, "keys_unavailable", None
        )


def test_an_entry_no_key_reader_can_use_is_skipped(hosted: Hosted) -> None:
    rotated = new_ec_key()
    hosted.server.publish("next-key", rotated)
    hosted.server.extra = [
        {"kty": "oct", "kid": "shared-secret", "k": "c3ludGhldGlj"},
        {"kty": "no-such-type", "kid": "unreadable"},
        {"kty": "EC"},
        "not an object",
    ]
    # The new key is picked up although the set holds entries beside it
    # that no reader can use.
    token = mint(rotated, claims_for(), kid="next-key")
    assert hosted.client.get("/runs", headers=bearer(token)).is_success
    # A symmetric key in the set verifies nothing.
    naming_it = mint(hosted.key, claims_for(), kid="shared-secret")
    assert_refused(hosted.client.get("/runs", headers=bearer(naming_it)), 401, "invalid", INVALID)


def test_the_key_set_is_fetched_from_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    config = hosted_config(
        {**complete_hosted_environment(), "HARRIER_SUPABASE_URL": f"{SUPABASE_URL}/"}
    )
    assert config is not None
    assert config.issuer == ISSUER
    assert config.jwks_url == f"{ISSUER}/.well-known/jwks.json"

    server = KeyServer()
    key = new_ec_key()
    server.publish(KID, key)
    opened: list[tuple[str, float | None]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(server()).encode()

    def urlopen(url: str, timeout: float | None = None) -> Response:
        opened.append((url, timeout))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    client = TestClient(create_hosted_app(config), base_url=f"http://{API_HOST}")
    response = client.get("/runs", headers=bearer(mint(key, claims_for())))
    assert response.status_code == 200, response.text
    assert [url for url, _ in opened] == [config.jwks_url]
    assert all(timeout is not None and timeout > 0 for _, timeout in opened)


def test_a_websocket_is_refused_hosted(hosted: Hosted) -> None:
    # No route takes a websocket today. One added later cannot skip the
    # token: a browser cannot send the header on the handshake.
    async def opened(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("open")
        await websocket.close()

    hosted.app.add_api_websocket_route("/socket", opened)
    # The Host the trusted-host check admits, so the refusal is this one.
    headers = {**bearer(hosted.token()), "Host": API_HOST}
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        hosted.client.websocket_connect("/socket", headers=headers) as socket,
    ):
        socket.receive_text()
    assert closed.value.code == 1008


def test_a_token_never_reaches_a_body_or_a_log(
    hosted: Hosted, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    now = int(time.time())
    sent: list[tuple[str, dict[str, Any]]] = []

    def send(path: str, claims: dict[str, Any], *, key: object = None, kid: str = KID) -> Any:
        token = mint(key if key is not None else hosted.key, claims, kid=kid)
        sent.append((token, claims))
        return hosted.client.get(path, headers=bearer(token))

    responses = [
        send("/runs", claims_for()),
        send("/runs/a-run", claims_for(exp=now - 120, iat=now - 3600)),
        send("/jobs", claims_for(), key=new_ec_key()),
        send("/config", claims_for(role="service_role")),
        send("/tracks", claims_for(is_anonymous=True)),
        send("/runs", claims_for(), kid="never-published"),
    ]
    hosted.server.failing = True
    hosted.clock.now += 3601 + 30
    responses.append(send("/runs", claims_for()))
    assert [r.status_code for r in responses] == [200, 401, 401, 401, 403, 401, 503]

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for token, claims in sent:
        for text in (*[r.text for r in responses], logged):
            assert token not in text
            for name in ("sub", "session_id", "email"):
                assert claims[name] not in text, name

    refusals = [
        record.getMessage() for record in caplog.records if "refused" in record.getMessage()
    ]
    assert "refused GET /runs/a-run: expired" in refusals
    assert "refused GET /jobs: invalid" in refusals
    assert "refused GET /tracks: anonymous" in refusals
    assert "refused GET /runs: keys_unavailable" in refusals


def test_a_refusal_log_line_cannot_be_forged(
    hosted: Hosted, caplog: pytest.LogCaptureFixture
) -> None:
    # The path reaches the log percent-decoded, so an encoded newline would
    # have written a second, forged record line.
    caplog.set_level(logging.INFO, logger="harrier_api.hostedauth")
    hosted.client.get("/x%0Arefused GET /admin: forged")
    refusals = [r.getMessage() for r in caplog.records if r.getMessage().startswith("refused")]
    assert refusals == ["refused GET /x\\nrefused GET /admin: forged: missing"]


# --- routes and contract ---


def test_every_hosted_route_but_health_needs_a_token(hosted: Hosted) -> None:
    walked: list[tuple[str, str]] = []
    for route in api_routes(hosted.app.routes):
        for method in route.methods or ():
            path = concrete(route.path)
            response = hosted.client.request(method, path)
            walked.append((method, route.path))
            if (method, route.path) == ("GET", "/health"):
                assert response.status_code == 200
            elif route.path in {"/session", "/capture/add-form"} or (
                (method, route.path) == ("GET", "/capture/add")
            ):
                assert response.status_code == 404, (method, route.path)
            else:
                assert_refused(response, 401, "missing", "Bearer")
    assert len(walked) > 40, "the walk is not finding the routes"
    for path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
        assert hosted.client.get(path).status_code == 404, path
    # Not served is not served with a token either, and HEAD, which
    # Starlette answers on every GET route, reaches no local-only page.
    token = bearer(hosted.token())
    for method, path in (
        ("GET", "/session"),
        ("GET", "/capture/add"),
        ("HEAD", "/capture/add"),
        ("HEAD", "/session"),
        ("POST", "/capture/add-form"),
        ("GET", "/openapi.json"),
    ):
        assert hosted.client.request(method, path, headers=token).status_code == 404, path
    # The /api prefix the SPA uses reaches the same check.
    assert_refused(hosted.client.get("/api/runs"), 401, "missing", "Bearer")
    assert hosted.client.get("/api/health").status_code == 200
    assert hosted.entered == []


def test_the_contract_declares_every_auth_refusal(hosted: Hosted) -> None:
    refusal = {"$ref": "#/components/schemas/AuthRefusalOut"}

    # What the hosted app answers without a token, route by route.
    answers_401 = {
        (method.lower(), route.path)
        for route in api_routes(hosted.app.routes)
        for method in route.methods or ()
        if hosted.client.request(method, concrete(route.path)).status_code == 401
    }
    assert len(answers_401) > 40, "the walk is not finding the routes"

    # The committed contract, and the document the route class builds now,
    # so a declaration the code stopped making fails here and not only in
    # the drift check.
    committed = json.loads(CONTRACT.read_text(encoding="utf-8"))
    for document in (committed, hosted.app.openapi()):
        assert document["components"]["securitySchemes"]["bearer"] == {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
        declared: set[tuple[str, str]] = set()
        for path, operations in document["paths"].items():
            for method, operation in operations.items():
                responses = operation["responses"]
                if "401" not in responses:
                    assert "security" not in operation, (method, path)
                    continue
                declared.add((method, path))
                schema = responses["401"]["content"]["application/json"]["schema"]
                assert schema == refusal, (method, path)
                assert operation.get("security") == [{"bearer": []}], (method, path)
                body = responses["503"]["content"]["application/json"]["schema"]
                assert refusal in body.get("anyOf", [body]), (method, path)
        assert declared == answers_401


def dependency_calls(dependant: Any) -> list[object]:
    calls: list[object] = [dependant.call]
    for child in dependant.dependencies:
        calls.extend(dependency_calls(child))
    return calls


def test_the_local_token_is_not_read_hosted(hosted: Hosted) -> None:
    """Spec 035's token is not read hosted: the bearer token serves its
    purpose. Every route that needs the local token locally is reached with a
    valid bearer token and no `X-Harrier-Token`. The store and the run
    manager answer 418 when reached, so no route does real work here."""

    def reached() -> object:
        raise HTTPException(status_code=418, detail="reached")

    hosted.app.dependency_overrides[get_conn] = reached
    hosted.app.dependency_overrides[get_manager] = reached
    token = bearer(hosted.token())
    walked: list[tuple[str, str]] = []
    for route in api_routes(hosted.app.routes):
        if require_token not in dependency_calls(route.dependant):
            continue
        for method in route.methods or ():
            assert needs_token(method, route.path), (method, route.path)
            response = hosted.client.request(method, concrete(route.path), headers=token)
            walked.append((method, route.path))
            assert response.status_code != 403, (method, route.path, response.text)
    assert len(walked) > 40, "the walk is not finding the routes that need the local token"
    # A wrong local token is not read either.
    response = hosted.client.post(
        "/runs/x/cancel", headers={**token, TOKEN_HEADER: "not-the-token"}
    )
    assert response.status_code == 418, response.text


def test_the_contract_is_the_same_in_both_modes(hosted: Hosted) -> None:
    local = create_app().openapi()
    assert json.dumps(hosted.app.openapi(), sort_keys=True) == json.dumps(local, sort_keys=True)


def test_hosted_health_reveals_no_store_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    def refuse(*_args: object, **_kwargs: object) -> object:
        opened.append("opened")
        raise AssertionError("hosted health opened the store or read a lease")

    monkeypatch.setattr(app_module, "connect", refuse)
    monkeypatch.setattr(app_module, "oldest_hold", refuse)
    hosted = build(failing_at_start=True)

    response = hosted.client.get("/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["database"] == "postgres"
    assert body["job_count"] is None
    assert body["database_hold"] is None
    assert body["demo"] is False
    assert opened == [] and hosted.entered == []


# --- cross-origin and hosts ---


def test_cors_allows_only_the_configured_origins(hosted: Hosted) -> None:
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization, content-type, last-event-id",
    }
    allowed = hosted.client.options("/runs", headers={"Origin": SPA_ORIGIN, **preflight})
    assert allowed.status_code == 200, allowed.text
    assert allowed.headers["access-control-allow-origin"] == SPA_ORIGIN
    methods = {m.strip() for m in allowed.headers["access-control-allow-methods"].split(",")}
    assert methods == {"GET", "POST", "PUT", "DELETE"}
    headers = {
        h.strip().lower() for h in allowed.headers["access-control-allow-headers"].split(",")
    }
    # Exactly the three, beside the headers CORS always admits.
    safelisted = {header.lower() for header in SAFELISTED_HEADERS}
    assert headers == safelisted | {"authorization", "content-type", "last-event-id"}
    assert "access-control-allow-credentials" not in allowed.headers

    # CORS is outermost: even the trusted-host refusal of a foreign Host
    # reaches an allowed origin's page readable.
    foreign = hosted.client.get("/runs", headers={"Origin": SPA_ORIGIN, "Host": "evil.test"})
    assert foreign.status_code == 400
    assert foreign.headers["access-control-allow-origin"] == SPA_ORIGIN

    for stranger in ("https://someone-else.vercel.app", "https://app.example.test.evil.test"):
        refused = hosted.client.options("/runs", headers={"Origin": stranger, **preflight})
        cors = [name for name in refused.headers if name.lower().startswith("access-control-")]
        assert cors == [], (stranger, cors)
        unread = hosted.client.get("/runs", headers={"Origin": stranger})
        assert not [n for n in unread.headers if n.lower().startswith("access-control-")]

    # A 401 to an allowed origin carries the CORS headers, so the SPA can
    # read why it was refused.
    response = hosted.client.get("/runs", headers={"Origin": SPA_ORIGIN})
    assert_refused(response, 401, "missing", "Bearer")
    assert response.headers["access-control-allow-origin"] == SPA_ORIGIN


def test_hosted_trusted_hosts_are_the_configured_names() -> None:
    names = (API_HOST, "api-staging.example.test")
    # Read the way a start reads them, from comma-separated variables.
    config = hosted_config(
        {
            **complete_hosted_environment(),
            "HARRIER_ALLOWED_HOSTS": f" {names[0]} , {names[1]} ",
            "HARRIER_CORS_ORIGINS": f"{SPA_ORIGIN},https://app-staging.example.test:8443",
        }
    )
    assert config is not None
    assert config.allowed_hosts == names
    assert config.cors_origins == (SPA_ORIGIN, "https://app-staging.example.test:8443")
    hosted = build(config=config)
    for name in names:
        for port in ("", ":443"):
            response = hosted.client.get("/health", headers={"Host": name + port})
            assert response.status_code == 200, f"{name}{port} is listed but refused"
    for foreign in ("localhost", "127.0.0.1", "0.0.0.0", "testserver", "evil.example.com"):
        assert hosted.client.get("/health", headers={"Host": foreign}).status_code == 400, foreign
