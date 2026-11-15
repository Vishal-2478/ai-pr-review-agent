"""asyncpg connection pool for the single Tiger Cloud / Postgres store."""

from __future__ import annotations

import json
from typing import Any

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(database_url: str, min_size: int = 1, max_size: int = 8) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=database_url,
        min_size=min_size,
        max_size=max_size,
        command_timeout=30,
        init=_init_connection,
    )


def vector_literal(embedding: list[float]) -> str:
    """pgvector's text input format; cast with ``$n::vector`` in SQL."""
    return "[" + ",".join(f"{x:.7f}" for x in embedding) + "]"


def normalize_row(row: Any) -> dict[str, Any]:
    """asyncpg Record -> plain JSON-friendly dict."""
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif value.__class__.__name__ == "UUID":
            out[key] = str(value)
        elif value.__class__.__name__ == "Decimal":
            out[key] = float(value)
        else:
            out[key] = value
    return out
