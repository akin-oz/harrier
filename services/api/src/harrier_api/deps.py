"""Shared FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends

from harrier.db import connect
from harrier_api.demo import demo_db_path, is_demo_mode


def get_conn() -> Iterator[sqlite3.Connection]:
    # same_thread=False: FastAPI runs this dependency and the endpoint on
    # different threadpool threads, so sqlite3's same-thread check fires
    # intermittently under concurrent requests even though this connection
    # only ever serves one request and is closed below.
    conn = connect(demo_db_path() if is_demo_mode() else None, same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
