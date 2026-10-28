"""Expected Alembic revision for readiness checks.

Bump EXPECTED_SCHEMA_REVISION whenever a new migration becomes required for
the running app. /readyz compares this to alembic_version in the database.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Head revision the application code is built against.
# Stay on 0002 until all Expense/QuickSplit/personal_expense read paths
# are gone — 0003 drops those tables and residual queries become 500s.
EXPECTED_SCHEMA_REVISION = "0002_unified_bills"


def get_database_schema_revision(engine: Engine) -> Optional[str]:
    """Return current alembic_version.version_num, or None if unstamped."""
    try:
        with engine.connect() as conn:
            # Table may not exist yet on brand-new DBs.
            dialect = engine.dialect.name
            if dialect == "sqlite":
                exists = conn.execute(
                    text(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
                    )
                ).fetchone()
            else:
                exists = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_name = 'alembic_version'"
                    )
                ).fetchone()
            if not exists:
                return None
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            return str(row[0]) if row else None
    except Exception:
        return None


def schema_is_ready(engine: Engine) -> tuple[bool, str]:
    """True when DB revision matches what this app build expects."""
    current = get_database_schema_revision(engine)
    if current is None:
        return False, (
            f"Database has no alembic_version (expected {EXPECTED_SCHEMA_REVISION}). "
            "Run: alembic upgrade head  (or alembic stamp 0001_baseline on a legacy DB)."
        )
    if current != EXPECTED_SCHEMA_REVISION:
        return False, (
            f"Schema revision mismatch: database={current} "
            f"app_expects={EXPECTED_SCHEMA_REVISION}"
        )
    return True, f"schema={current}"
