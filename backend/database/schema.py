"""Schema migration and capability detection.

One Postgres-compatible database holds all three data shapes. On Tiger Cloud
that means TimescaleDB (hypertable + continuous aggregates), pgvector and
pgvectorscale (DiskANN). On a plain Postgres with only pgvector the same
migrations still apply: each file can declare ``-- requires: <extension>`` or
``-- requires: !<extension>`` and is skipped when the condition does not hold,
so a laptop gets ordinary views and an HNSW index instead.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.core.exceptions import ConfigurationError

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "migrations"
OPTIONAL_EXTENSIONS = ("timescaledb", "vectorscale")
_REQUIRES = re.compile(r"^--\s*requires:\s*(!?)([a-z_]+)\s*$", re.MULTILINE)


MIGRATION_LOCK_KEY = 7_316_042_001  # arbitrary, stable: "pr-review-agent migrations"
_PROVIDES = re.compile(r"^--\s*provides:\s*([a-z_]+)\s*$", re.MULTILINE)
_OPTIONAL = re.compile(r"^--\s*optional\s*$", re.MULTILINE)


@dataclass
class MigrationReport:
    extensions: dict[str, bool] = field(default_factory=dict)
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    # Optional (Tiger-specific) files that failed and were replaced by their fallback.
    degraded: dict[str, str] = field(default_factory=dict)

    @property
    def flavor(self) -> str:
        if self.extensions.get("timescaledb") and self.extensions.get("vectorscale"):
            base = "tiger"
        elif self.extensions.get("timescaledb"):
            base = "timescale"
        else:
            base = "plain-postgres"
        return f"{base} (degraded: {', '.join(sorted(self.degraded))})" if self.degraded else base


async def ensure_extensions(conn: Any) -> dict[str, bool]:
    """pgvector is mandatory; the Tiger extensions are used when present."""
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(
            "pgvector is required. Use Tiger Cloud, the timescale/timescaledb-ha image, "
            f"or install pgvector on your Postgres ({exc})."
        ) from exc
    for ext in OPTIONAL_EXTENSIONS:
        try:
            await conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext} CASCADE")
        except Exception as exc:  # noqa: BLE001 - optional by design
            log.info("extension %s not available: %s", ext, str(exc).splitlines()[0])
    rows = await conn.fetch("SELECT extname FROM pg_extension")
    installed = {r["extname"] for r in rows}
    return {name: name in installed for name in ("vector", *OPTIONAL_EXTENSIONS)}


def _applies(sql: str, features: dict[str, bool]) -> bool:
    """``-- requires: name`` / ``-- requires: !name`` against extensions and provided features."""
    for negate, name in _REQUIRES.findall(sql):
        present = features.get(name, False)
        if (negate and present) or (not negate and not present):
            return False
    return True


async def migrate(conn: Any, embedding_dim: int, migrations_dir: Path = MIGRATIONS_DIR) -> MigrationReport:
    """Apply pending migrations in file order.

    Directives (SQL comments at the top of a file):

    - ``-- requires: timescaledb`` runs the file only when that extension or
      feature is present; ``-- requires: !rollups`` only when it is absent.
    - ``-- provides: rollups`` marks a feature as satisfied once the file is applied.
    - ``-- optional`` lets the file fail without failing the migration: the
      error is recorded and the ``!feature`` fallback after it runs instead.
      This is how a Tiger-specific index or continuous aggregate degrades to
      its plain-Postgres equivalent rather than taking the service down.

    Each file is one implicit transaction, so a failed file leaves nothing behind.
    """
    # The API and the worker start together and both migrate. A session-level
    # advisory lock serialises them: the second one finds everything applied.
    await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
    try:
        return await _migrate_locked(conn, embedding_dim, migrations_dir)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)


async def _migrate_locked(conn: Any, embedding_dim: int, migrations_dir: Path) -> MigrationReport:
    report = MigrationReport(extensions=await ensure_extensions(conn))
    features = dict(report.extensions)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
    for path in sorted(migrations_dir.glob("*.sql")):
        sql = path.read_text()
        provides = _PROVIDES.findall(sql)
        if path.name in done:
            report.already.append(path.name)
            features.update(dict.fromkeys(provides, True))
            continue
        if not _applies(sql, features):
            report.skipped.append(path.name)
            continue
        sql = sql.replace("{{EMBEDDING_DIM}}", str(int(embedding_dim)))
        try:
            await conn.execute(sql)
        except Exception as exc:
            if _OPTIONAL.search(sql):
                reason = str(exc).splitlines()[0][:300]
                log.warning("optional migration %s failed, using its fallback: %s", path.name, reason)
                report.degraded[path.name] = reason
                continue
            raise ConfigurationError(f"migration {path.name} failed: {exc}") from exc
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", path.name)
        report.applied.append(path.name)
        features.update(dict.fromkeys(provides, True))
    await verify_embedding_dimension(conn, embedding_dim)
    return report


async def verify_embedding_dimension(conn: Any, expected: int) -> None:
    """Fail at startup, not at the first insert, if schema and embedder disagree."""
    actual = await conn.fetchval(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = 'code_chunks'::regclass AND attname = 'embedding'"
    )
    if actual is not None and int(actual) != int(expected):
        raise ConfigurationError(
            f"code_chunks.embedding is VECTOR({actual}) but EMBEDDING_DIMENSIONS={expected}. "
            "Set EMBEDDING_DIMENSIONS to match, or drop code_chunks and re-index."
        )


async def reset_schema(conn: Any) -> None:
    """Drop everything this project created. Test databases only."""
    dbname = await conn.fetchval("SELECT current_database()")
    if "test" not in dbname:
        raise ConfigurationError(f"refusing to reset non-test database '{dbname}'")
    for view in ("agent_health_1m", "pr_cost_hourly"):
        kind = await conn.fetchval("SELECT relkind FROM pg_class WHERE relname = $1", view)
        if kind == b"m" or kind == "m":
            await conn.execute(f"DROP MATERIALIZED VIEW IF EXISTS {view} CASCADE")
        elif kind is not None:
            await conn.execute(f"DROP VIEW IF EXISTS {view} CASCADE")
    for table in (
        "agent_events", "hitl_feedback", "hitl_reviews", "finding_records", "pr_review_records",
        "code_chunks", "repo_file_index", "repo_conventions", "schema_migrations",
    ):  # fmt: skip
        await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await conn.execute("DROP FUNCTION IF EXISTS reject_agent_events_mutation() CASCADE")
