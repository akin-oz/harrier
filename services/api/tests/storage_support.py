"""A fake Supabase Storage server for the hosted file store (spec 107).

Storage is not in CI (spec 107, Honest limitations), so the hosted store is
tested against this fake behind an `httpx.MockTransport`. It answers the five
calls the seam makes, in the shapes Storage's published API gives them, read
2026-10-16 from Supabase's Storage server reference and `storage-js`:

- upload: `POST /storage/v1/object/{bucket}/{name}`, the bytes as the body,
  `x-upsert: true`, the content type and `cache-control` as headers;
- download: `GET /storage/v1/object/{bucket}/{name}`;
- info: `GET /storage/v1/object/info/{bucket}/{name}`, answering `size`,
  `etag`, `version`, `updated_at` and `last_modified`;
- remove: `DELETE /storage/v1/object/{bucket}` with `{"prefixes": [...]}`,
  answering the removed objects, an absent one simply not among them;
- list: `POST /storage/v1/object/list/{bucket}` with `prefix`, `limit`,
  `offset` and `sortBy`, answering the entries directly inside the prefix:
  a folder with `id` null, an object with its `id`, `updated_at` and
  `metadata` (`size`, `eTag`, `mimetype`).

An absent object is answered 404 with Storage's `not_found` body, or, with
`legacy_not_found`, 400 with `statusCode` "404", as older Storage versions
answered.

An upload without `x-upsert: true` to a name already held is answered 400
with `statusCode` "409", `error` "Duplicate" and the message "The resource
already exists", or, with `conflict_as_409`, the same body under HTTP 409
(spec 154). That is the shape read 2026-10-17 from `supabase/storage`:
`KeyAlreadyExists` in `src/internal/errors/codes.ts`, sent as HTTP 400 with
the status in the body by `src/http/error-handler.ts`. The object is left
unchanged. Every request is recorded. Every name is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

BUCKET = "harrier-files"
PREFIX = "/storage/v1/object"


@dataclass
class StoredObject:
    data: bytes
    content_type: str
    cache_control: str
    updated_at: str
    etag: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class FakeStorage:
    """Objects by full name, in memory. `failures` answers the next calls of
    an operation (`upload`, `download`, `info`, `remove`, `list`) with the
    statuses given, once each; `raising` raises an exception instead."""

    objects: dict[str, StoredObject] = field(default_factory=dict[str, StoredObject])
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])
    failures: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    raising: dict[str, Exception] = field(default_factory=dict[str, Exception])
    legacy_not_found: bool = False
    conflict_as_409: bool = False
    before: dict[str, Callable[[httpx.Request], None]] = field(
        default_factory=dict[str, Callable[[httpx.Request], None]]
    )
    _tick: int = 0

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), timeout=30.0)

    # --- what tests read ---

    def names(self) -> list[str]:
        return sorted(self.objects)

    def operations(self) -> list[str]:
        return [self.operation(request)[0] for request in self.requests]

    def put(self, name: str, data: bytes, content_type: str = "application/json") -> None:
        """Place an object as if it had been uploaded."""
        self._store(name, data, content_type, "no-cache")

    # --- the server ---

    def operation(self, request: httpx.Request) -> tuple[str, str]:
        """The operation a request asks for, and the object name or prefix
        it names (empty for a list or a remove, whose names are in the
        body)."""
        path = request.url.path
        if not path.startswith(PREFIX + "/"):
            return "unknown", ""
        rest = path[len(PREFIX) + 1 :]
        if rest.startswith(f"info/{BUCKET}/") and request.method == "GET":
            return "info", rest[len(f"info/{BUCKET}/") :]
        if rest == f"list/{BUCKET}" and request.method == "POST":
            return "list", ""
        if rest == BUCKET and request.method == "DELETE":
            return "remove", ""
        if rest.startswith(f"{BUCKET}/"):
            name = rest[len(f"{BUCKET}/") :]
            if request.method == "POST":
                return "upload", name
            if request.method == "GET":
                return "download", name
        return "unknown", ""

    def _not_found(self) -> httpx.Response:
        body = {"statusCode": "404", "error": "not_found", "message": "Object not found"}
        return httpx.Response(400 if self.legacy_not_found else 404, json=body)

    def _conflict(self) -> httpx.Response:
        body = {
            "statusCode": "409",
            "error": "Duplicate",
            "message": "The resource already exists",
        }
        return httpx.Response(409 if self.conflict_as_409 else 400, json=body)

    def _now(self) -> str:
        self._tick += 1
        moment = datetime(2026, 10, 16, 9, 0, tzinfo=UTC) + timedelta(seconds=self._tick)
        return moment.isoformat().replace("+00:00", "Z")

    def _store(self, name: str, data: bytes, content_type: str, cache_control: str) -> None:
        etag = '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'
        self.objects[name] = StoredObject(data, content_type, cache_control, self._now(), etag)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        operation, name = self.operation(request)
        if operation in self.before:
            self.before[operation](request)
        if operation in self.raising:
            raise self.raising[operation]
        pending = self.failures.get(operation)
        if pending:
            return httpx.Response(pending.pop(0), json={"statusCode": "500", "error": "failed"})
        if operation == "upload":
            if request.headers.get("x-upsert") != "true" and name in self.objects:
                return self._conflict()
            self._store(
                name,
                request.content,
                request.headers.get("content-type", ""),
                request.headers.get("cache-control", ""),
            )
            return httpx.Response(200, json={"Key": f"{BUCKET}/{name}", "Id": str(uuid.uuid4())})
        if operation == "download":
            found = self.objects.get(name)
            if found is None:
                return self._not_found()
            return httpx.Response(
                200, content=found.data, headers={"content-type": found.content_type}
            )
        if operation == "info":
            found = self.objects.get(name)
            if found is None:
                return self._not_found()
            return httpx.Response(
                200,
                json={
                    "id": found.id,
                    "name": name,
                    "version": found.id,
                    "bucket_id": BUCKET,
                    "size": len(found.data),
                    "content_type": found.content_type,
                    "cache_control": found.cache_control,
                    "etag": found.etag,
                    "last_modified": found.updated_at,
                    "created_at": found.updated_at,
                    "updated_at": found.updated_at,
                },
            )
        if operation == "remove":
            prefixes = json.loads(request.content)["prefixes"]
            removed = [
                {"name": item, "bucket_id": BUCKET}
                for item in prefixes
                if self.objects.pop(item, None) is not None
            ]
            return httpx.Response(200, json=removed)
        if operation == "list":
            return httpx.Response(200, json=self._list(json.loads(request.content)))
        return httpx.Response(400, json={"statusCode": "400", "error": "unknown route"})

    def _list(self, body: dict[str, object]) -> list[dict[str, object]]:
        prefix = str(body["prefix"]).rstrip("/")
        limit = int(str(body.get("limit", 100)))
        offset = int(str(body.get("offset", 0)))
        entries: dict[str, dict[str, object]] = {}
        for name, found in self.objects.items():
            if not name.startswith(prefix + "/"):
                continue
            inside = name[len(prefix) + 1 :]
            first, _, rest = inside.partition("/")
            if rest:
                entries.setdefault(
                    first,
                    {
                        "name": first,
                        "id": None,
                        "updated_at": None,
                        "created_at": None,
                        "metadata": None,
                    },
                )
            else:
                entries[first] = {
                    "name": first,
                    "id": found.id,
                    "updated_at": found.updated_at,
                    "created_at": found.updated_at,
                    "last_accessed_at": found.updated_at,
                    "metadata": {
                        "eTag": found.etag,
                        "size": len(found.data),
                        "mimetype": found.content_type,
                        "cacheControl": found.cache_control,
                    },
                }
        ordered = [entries[key] for key in sorted(entries)]
        return ordered[offset : offset + limit]
