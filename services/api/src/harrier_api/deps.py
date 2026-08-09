"""Shared FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends

from harrier.db import connect
from harrier_api.demo import demo_db_path, is_demo_mode


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect(demo_db_path() if is_demo_mode() else None)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
